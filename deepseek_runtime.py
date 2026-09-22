# -*- coding: utf-8 -*-
"""DeepSeek V4 request routing, preflight checks, and usage accounting."""

from __future__ import annotations

import json
import ipaddress
import socket
import threading
import time
import urllib.request
from urllib.parse import quote, urlparse
from typing import Any, Callable


SUPPORTED_MODELS = {"deepseek-v4-flash", "deepseek-v4-pro"}

# CNY per one million tokens.  These are only used for a clearly labelled
# estimate; the durable token counters remain authoritative if prices change.
DEFAULT_PRICING_CNY = {
    "deepseek-v4-flash": {"cache_hit": 0.02, "cache_miss": 1.0, "output": 2.0},
    "deepseek-v4-pro": {"cache_hit": 0.025, "cache_miss": 3.0, "output": 6.0},
}


class DeepSeekPreflightError(RuntimeError):
    pass


_ORIGINAL_GETADDRINFO = socket.getaddrinfo
_DNS_OVERRIDES: dict[str, str] = {}
_DNS_OVERRIDE_LOCK = threading.Lock()


def _targeted_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
    normalized = str(host or "").lower().rstrip(".")
    with _DNS_OVERRIDE_LOCK:
        replacement = _DNS_OVERRIDES.get(normalized)
    return _ORIGINAL_GETADDRINFO(replacement or host, port, *args, **kwargs)


def install_dns_override(host: str, ipv4: str) -> None:
    """Install a process-local DNS override for one HTTPS host only."""
    normalized = str(host or "").lower().rstrip(".")
    address = ipaddress.ip_address(str(ipv4 or "").strip())
    if (
        not normalized
        or address.version != 4
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
    ):
        raise ValueError("无效的 DeepSeek 公网 IPv4 地址")
    with _DNS_OVERRIDE_LOCK:
        _DNS_OVERRIDES[normalized] = str(address)
        if socket.getaddrinfo is not _targeted_getaddrinfo:
            socket.getaddrinfo = _targeted_getaddrinfo


def resolve_public_ipv4(
    host: str,
    *,
    urlopen: Callable[..., Any] = urllib.request.urlopen,
    timeout: int = 10,
) -> str:
    """Resolve one public IPv4 through HTTPS DNS, avoiding local Fake-IP DNS."""
    normalized = str(host or "").lower().rstrip(".")
    if not normalized:
        raise DeepSeekPreflightError("DeepSeek 域名为空，无法启用直连 DNS")
    endpoints = (
        f"https://dns.google/resolve?name={quote(normalized)}&type=A",
        f"https://cloudflare-dns.com/dns-query?name={quote(normalized)}&type=A",
    )
    errors = []
    for endpoint in endpoints:
        request = urllib.request.Request(
            endpoint,
            headers={"Accept": "application/dns-json"},
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            for answer in payload.get("Answer", []) if isinstance(payload, dict) else []:
                if int(answer.get("type") or 0) != 1:
                    continue
                address = ipaddress.ip_address(str(answer.get("data") or "").strip())
                if (
                    address.version == 4
                    and not address.is_private
                    and not address.is_loopback
                    and not address.is_link_local
                    and not address.is_reserved
                ):
                    return str(address)
        except Exception as exc:
            errors.append(str(exc))
    detail = errors[-1] if errors else "没有返回可用公网 IPv4"
    raise DeepSeekPreflightError(f"DeepSeek 公网 DNS 解析失败：{detail}")


def enable_direct_dns(
    base_url: str,
    *,
    urlopen: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, str]:
    """Enable a scoped direct route when a local proxy serves unusable Fake-IP DNS."""
    host = (urlparse(str(base_url or "")).hostname or "").lower().rstrip(".")
    if host != "api.deepseek.com":
        raise DeepSeekPreflightError("仅允许为 api.deepseek.com 启用定向 DNS")
    address = resolve_public_ipv4(host, urlopen=urlopen)
    install_dns_override(host, address)
    return {"host": host, "ipv4": address}


def is_transient_connection_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    return any(
        token in text
        for token in (
            "connection error",
            "connecterror",
            "urlopen error",
            "unexpected_eof",
            "ssl",
            "timed out",
            "timeout",
            "connection reset",
            "network",
        )
    )


def build_chat_kwargs(
    *,
    model: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    thinking: bool,
    temperature: float | None = None,
    reasoning_effort: str = "high",
    stream: bool = False,
) -> dict[str, Any]:
    """Build parameters without sending temperature to thinking-mode calls."""
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": int(max_tokens),
        "extra_body": {
            "thinking": {"type": "enabled" if thinking else "disabled"}
        },
    }
    if thinking:
        payload["reasoning_effort"] = (
            reasoning_effort if reasoning_effort in {"high", "max"} else "high"
        )
    elif temperature is not None:
        payload["temperature"] = float(temperature)
    if stream:
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
    return payload


def _field(value: Any, name: str, default: Any = 0) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def normalize_usage(usage: Any) -> dict[str, int]:
    if not usage:
        return {
            "prompt_tokens": 0,
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": 0,
            "completion_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 0,
        }
    prompt = int(_field(usage, "prompt_tokens", 0) or 0)
    hit = int(_field(usage, "prompt_cache_hit_tokens", 0) or 0)
    miss = int(_field(usage, "prompt_cache_miss_tokens", 0) or 0)
    completion = int(_field(usage, "completion_tokens", 0) or 0)
    details = _field(usage, "completion_tokens_details", None)
    reasoning = int(_field(details, "reasoning_tokens", 0) or 0)
    if not hit and not miss and prompt:
        # Conservative estimate when an OpenAI-compatible proxy omits the
        # cache breakdown.
        miss = prompt
    return {
        "prompt_tokens": prompt,
        "prompt_cache_hit_tokens": hit,
        "prompt_cache_miss_tokens": miss,
        "completion_tokens": completion,
        "reasoning_tokens": reasoning,
        "total_tokens": int(_field(usage, "total_tokens", prompt + completion) or 0),
    }


def estimate_cost_cny(model: str, usage: dict[str, int]) -> float:
    rates = DEFAULT_PRICING_CNY.get(model)
    if not rates:
        return 0.0
    return (
        usage.get("prompt_cache_hit_tokens", 0) * rates["cache_hit"]
        + usage.get("prompt_cache_miss_tokens", 0) * rates["cache_miss"]
        + usage.get("completion_tokens", 0) * rates["output"]
    ) / 1_000_000


def _available_model_ids(response: Any) -> set[str]:
    data = _field(response, "data", []) or []
    return {
        str(_field(item, "id", "") or "").strip()
        for item in data
        if str(_field(item, "id", "") or "").strip()
    }


def preflight(
    *,
    client: Any,
    api_key: str,
    base_url: str,
    required_models: list[str],
    urlopen: Callable[..., Any] = urllib.request.urlopen,
    timeout: int = 20,
    retry_attempts: int = 3,
    retry_delay_seconds: float = 1.0,
    sleep: Callable[[float], Any] = time.sleep,
) -> dict[str, Any]:
    """Validate authentication, model availability, and usable balance.

    Both endpoints are read-only and do not generate model tokens.
    """
    attempts = max(1, int(retry_attempts or 1))
    model_response = None
    model_error = None
    for attempt in range(1, attempts + 1):
        try:
            model_response = client.models.list()
            model_error = None
            break
        except Exception as exc:
            model_error = exc
            if attempt < attempts:
                sleep(max(0.0, float(retry_delay_seconds)) * attempt)
    if model_error is not None:
        raise DeepSeekPreflightError(f"模型列表读取失败：{model_error}") from model_error
    available = _available_model_ids(model_response)
    missing = sorted({str(item) for item in required_models if item} - available)
    if missing:
        raise DeepSeekPreflightError("当前账号不可用模型：" + "、".join(missing))

    endpoint = str(base_url or "https://api.deepseek.com").rstrip("/")
    if endpoint.endswith("/v1"):
        endpoint = endpoint[:-3]
    request = urllib.request.Request(
        endpoint + "/user/balance",
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
    )
    balance = None
    balance_error = None
    for attempt in range(1, attempts + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                balance = json.loads(response.read().decode("utf-8"))
            balance_error = None
            break
        except Exception as exc:
            balance_error = exc
            if attempt < attempts:
                sleep(max(0.0, float(retry_delay_seconds)) * attempt)
    if balance_error is not None:
        raise DeepSeekPreflightError(f"余额读取失败：{balance_error}") from balance_error
    if not isinstance(balance, dict) or not balance.get("is_available"):
        raise DeepSeekPreflightError("DeepSeek 余额不足或账户当前不可调用")
    return {
        "status": "PASS",
        "available_models": sorted(available),
        "required_models": sorted(set(required_models)),
        "balance_infos": balance.get("balance_infos") or [],
    }
