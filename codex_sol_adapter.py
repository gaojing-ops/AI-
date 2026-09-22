# -*- coding: utf-8 -*-
"""Fail-closed adapter for non-interactive Codex ``exec`` calls.

The adapter deliberately has no knowledge of Codex authentication files.  It
passes the prompt over stdin, runs without a shell, and only exposes hashes of
stderr and the bound inputs.  Formal evidence is command-bound to an embedded,
reviewed official CLI release and the exact ``gpt-5.6-sol`` command policy.
Current Codex JSONL does not report a model; if a trusted lifecycle event
reports one in the future, it is additional evidence and must exactly match
the command.

The receipt is local operational evidence.  It is not a cryptographic remote
attestation and does not defend against an administrator who can modify this
Python source, the running process, or its memory.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from contextlib import contextmanager
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence


DEFAULT_MODEL = "gpt-5.6-sol"  # 默认兜底模型，支持由配置动态覆盖
RECEIPT_VERSION = 6
ADAPTER_VERSION = 7
PROTOCOL_VERSION = 5
FORMAL_BUNDLE_VERSION = 5
FORMAL_PROVENANCE_STATUS = "command_bound_official_cli"
FORMAL_EVENT_REPORTED_STATUS = "event_reported_official_cli"
LOCAL_EVIDENCE_SCOPE = "local_operational_evidence_not_remote_attestation"
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_TRUST_ROOT_ID = "embedded_openai_codex_release_allowlist_v2"
_OFFICIAL_RELEASES = MappingProxyType({
    "83751f15cb6a0a7b97df67752c001e3fe1c20e18ffbfec3ff63567296205eb6c":
        MappingProxyType({
            "release_id": "@openai/codex@0.145.0/windows-x64",
            "platform": "windows",
            "architecture": "x64",
            "root_package_directory": "codex",
            "root_package_name": "@openai/codex",
            "root_package_version": "0.145.0",
            "native_package_alias": "@openai/codex-win32-x64",
            "native_package_directory": "codex-win32-x64",
            "native_package_name": "@openai/codex",
            "native_package_version": "0.145.0-win32-x64",
            "native_dependency_spec": "npm:@openai/codex@0.145.0-win32-x64",
            "native_executable_relative_path": (
                "vendor/x86_64-pc-windows-msvc/bin/codex.exe"
            ),
            "cli_version": "codex-cli 0.145.0",
            "cli_version_stdout": "codex-cli 0.145.0\n",
        }),
})

_FORMAL_PROVENANCE_BASIS = {
    "transport": "production_subprocess",
    "cli_identity": "embedded_release_allowlist_live_identity_and_lock",
    "model_selection": "fixed_exact_command_argument",
    "event_protocol": "complete_codex_exec_jsonl_lifecycle",
    "output_contract": "event_message_output_file_and_schema_replay",
}

_MODEL_PROVENANCE_FIELDS = {
    "thread.started": ("model", "model_name"),
    "turn.started": ("model", "model_name"),
    "turn.completed": ("model", "model_name"),
}

_SCHEMA_KEYWORDS = {
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "const",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "anyOf",
    "oneOf",
}
_SCHEMA_TYPES = {
    "object",
    "array",
    "string",
    "integer",
    "number",
    "boolean",
    "null",
}
_RUNTIME_ENV_NAMES = {
    "APPDATA",
    "CODEX_HOME",
    "COMSPEC",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "LOCALAPPDATA",
    "NO_COLOR",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TERM",
    "TMP",
    "USERPROFILE",
    "WINDIR",
}
_SENSITIVE_ENV_PARTS = (
    "_KEY",
    "API_KEY",
    "APIKEY",
    "AUTH",
    "CREDENTIAL",
    "PASSWORD",
    "PASSWD",
    "PRIVATE_KEY",
    "SECRET",
    "TOKEN",
)


class CodexSolError(RuntimeError):
    """Base error for a call that cannot be trusted."""


class CodexSolExecutableError(CodexSolError):
    """The configured Codex executable is absent or unusable."""


class CodexSolAccessError(CodexSolError):
    """Windows or the sandbox refused to execute Codex."""


class CodexSolTimeoutError(CodexSolError):
    """Codex did not finish within the explicit timeout."""


class CodexSolProtocolError(CodexSolError):
    """Codex returned incomplete, malformed, or contradictory evidence."""


Runner = Callable[..., Any]
Which = Callable[[str], str | None]


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _file_sha256(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except PermissionError as exc:
        raise CodexSolAccessError(
            "Windows or the sandbox denied access to the Codex executable"
        ) from exc
    except OSError as exc:
        raise CodexSolExecutableError(
            "The Codex executable could not be hashed"
        ) from exc
    return digest.hexdigest()


def _validate_formal_executable_path(path: str | os.PathLike[str]) -> str:
    if os.name != "nt":
        raise CodexSolProtocolError(
            "The embedded formal trust root supports Windows x64 only"
        )
    raw = str(path or "")
    candidate = Path(raw)
    if not candidate.is_absolute() or raw.startswith(("\\\\", "//")):
        raise CodexSolProtocolError(
            "Formal Codex executable must be an absolute local path"
        )
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise CodexSolExecutableError(
            "Formal Codex executable path cannot be resolved"
        ) from None
    if resolved.suffix.lower() != ".exe" or not resolved.is_file():
        raise CodexSolProtocolError("Formal Codex executable must be a native .exe")
    return str(resolved)


def _open_windows_executable_lock(path: str) -> tuple[int, dict[str, str]]:
    """Open a Windows handle that denies write and delete sharing."""

    import ctypes
    from ctypes import wintypes

    class _FileTime(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", _FileTime),
            ("ftLastAccessTime", _FileTime),
            ("ftLastWriteTime", _FileTime),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    get_info = kernel32.GetFileInformationByHandle
    get_info.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ByHandleFileInformation))
    get_info.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    generic_read = 0x80000000
    file_share_read = 0x00000001
    open_existing = 3
    file_attribute_normal = 0x00000080
    invalid_handle = ctypes.c_void_p(-1).value
    handle = create_file(
        path,
        generic_read,
        file_share_read,
        None,
        open_existing,
        file_attribute_normal,
        None,
    )
    handle_value = int(handle or 0)
    if handle_value == invalid_handle or not handle_value:
        error = ctypes.get_last_error()
        raise OSError(error, "CreateFileW integrity lock failed")
    info = _ByHandleFileInformation()
    if not get_info(handle, ctypes.byref(info)):
        error = ctypes.get_last_error()
        close_handle(handle)
        raise OSError(error, "GetFileInformationByHandle failed")
    if int(info.dwFileAttributes) & 0x00000400:
        close_handle(handle)
        raise OSError("Formal executable must not be a reparse point")
    identity = {
        "volume_serial": f"{int(info.dwVolumeSerialNumber):08x}",
        "file_id": (
            f"{int(info.nFileIndexHigh):08x}{int(info.nFileIndexLow):08x}"
        ),
    }
    return handle_value, identity


def _windows_identity_from_handle(handle_value: int) -> dict[str, str]:
    import ctypes
    from ctypes import wintypes

    class _FileTime(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", _FileTime),
            ("ftLastAccessTime", _FileTime),
            ("ftLastWriteTime", _FileTime),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_info = kernel32.GetFileInformationByHandle
    get_info.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ByHandleFileInformation))
    get_info.restype = wintypes.BOOL
    info = _ByHandleFileInformation()
    if not get_info(wintypes.HANDLE(handle_value), ctypes.byref(info)):
        raise OSError(ctypes.get_last_error(), "GetFileInformationByHandle failed")
    return {
        "volume_serial": f"{int(info.dwVolumeSerialNumber):08x}",
        "file_id": f"{int(info.nFileIndexHigh):08x}{int(info.nFileIndexLow):08x}",
    }


def _close_windows_handle(handle_value: int) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    close_handle(wintypes.HANDLE(handle_value))


@contextmanager
def _hold_formal_executable_lock(path: str):
    """Hold the executable immutable across hash, probe, and main execution."""

    try:
        handle, identity = _open_windows_executable_lock(path)
    except (OSError, PermissionError):
        raise CodexSolAccessError(
            "Formal mode could not acquire the Windows executable integrity lock"
        ) from None
    try:
        yield dict(identity)
    except BaseException:
        raise
    else:
        try:
            final_identity = _windows_identity_from_handle(handle)
        except OSError:
            raise CodexSolAccessError(
                "Formal executable integrity lock could not be revalidated"
            ) from None
        if final_identity != identity:
            raise CodexSolProtocolError(
                "Formal executable file identity changed while locked"
            )
    finally:
        _close_windows_handle(handle)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _reject_json_constant(_value: str) -> Any:
    raise ValueError("Non-standard JSON numeric constant")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _strict_json_loads(value: str) -> Any:
    return json.loads(
        value,
        parse_constant=_reject_json_constant,
        object_pairs_hook=_unique_json_object,
    )


def _trusted_release_for_hash(executable_sha256: str) -> Mapping[str, str]:
    digest = str(executable_sha256 or "").lower()
    release = _OFFICIAL_RELEASES.get(digest)
    if release is None:
        raise CodexSolProtocolError(
            "Codex executable SHA-256 is not in the embedded official release allowlist"
        )
    return release


def _official_package_layout(
    executable_path: str,
    release: Mapping[str, str],
) -> dict[str, Path | str]:
    """Derive the one approved npm root/native package layout."""

    executable = Path(executable_path)
    relative_text = release["native_executable_relative_path"]
    relative_parts = tuple(relative_text.split("/"))
    if (
        not executable.is_absolute()
        or not relative_parts
        or any(not part or part in {".", ".."} for part in relative_parts)
        or len(executable.parts) < len(relative_parts) + 3
        or tuple(executable.parts[-len(relative_parts):]) != relative_parts
    ):
        raise CodexSolProtocolError(
            "Official Codex executable is not at the approved native vendor path"
        )

    native_root = executable.parents[len(relative_parts) - 1]
    scope_root = native_root.parent
    node_modules_root = scope_root.parent
    if (
        native_root.name != release["native_package_directory"]
        or scope_root.name != "@openai"
        or node_modules_root.name != "node_modules"
    ):
        raise CodexSolProtocolError(
            "Official Codex native package path does not match the trust root"
        )

    root_package_root = scope_root / release["root_package_directory"]
    return {
        "native_package_root": native_root,
        "native_package_json_path": native_root / "package.json",
        "root_package_root": root_package_root,
        "root_package_json_path": root_package_root / "package.json",
        "native_executable_relative_path": relative_text,
    }


def _read_strict_package_json(package_path: Path, *, layer: str) -> Mapping[str, Any]:
    try:
        resolved = package_path.resolve(strict=True)
        if resolved != package_path:
            raise OSError("package.json path escaped the approved package root")
        raw = resolved.read_text(encoding="utf-8")
        package = _strict_json_loads(raw)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        raise CodexSolProtocolError(
            f"Official Codex {layer} package.json identity is unreadable"
        ) from None
    if not isinstance(package, Mapping):
        raise CodexSolProtocolError(
            f"Official Codex {layer} package.json must be an object"
        )
    return package


def _load_adjacent_package_identity(
    executable_path: str,
    release: Mapping[str, str],
) -> dict[str, str]:
    """Load the exact native package and its sibling root package identity."""

    layout = _official_package_layout(executable_path, release)
    native_path = layout["native_package_json_path"]
    root_path = layout["root_package_json_path"]
    if not isinstance(native_path, Path) or not isinstance(root_path, Path):
        raise CodexSolProtocolError("Official Codex package layout is invalid")
    native_package = _read_strict_package_json(native_path, layer="native")
    root_package = _read_strict_package_json(root_path, layer="root")

    native_name = native_package.get("name")
    native_version = native_package.get("version")
    if (
        native_name != release["native_package_name"]
        or native_version != release["native_package_version"]
    ):
        raise CodexSolProtocolError(
            "Official Codex native package name or version does not match the trust root"
        )

    root_name = root_package.get("name")
    root_version = root_package.get("version")
    if (
        root_name != release["root_package_name"]
        or root_version != release["root_package_version"]
    ):
        raise CodexSolProtocolError(
            "Official Codex root package name or version does not match the trust root"
        )
    optional_dependencies = root_package.get("optionalDependencies")
    if (
        not isinstance(optional_dependencies, Mapping)
        or optional_dependencies.get(release["native_package_alias"])
        != release["native_dependency_spec"]
    ):
        raise CodexSolProtocolError(
            "Official Codex root package does not bind the approved native package"
        )

    return {
        "native_package_alias": release["native_package_alias"],
        "native_package_directory": release["native_package_directory"],
        "native_package_json_path": str(native_path),
        "native_package_json_sha256": _file_sha256(native_path),
        "native_package_name": str(native_name),
        "native_package_version": str(native_version),
        "native_executable_relative_path": release[
            "native_executable_relative_path"
        ],
        "root_package_directory": release["root_package_directory"],
        "root_package_json_path": str(root_path),
        "root_package_json_sha256": _file_sha256(root_path),
        "root_package_name": str(root_name),
        "root_package_version": str(root_version),
        "native_dependency_spec": release["native_dependency_spec"],
    }


def _inspect_official_release(
    executable_path: str,
    executable_sha256: str,
    file_identity: Mapping[str, str],
) -> dict[str, Any]:
    release = _trusted_release_for_hash(executable_sha256)
    package = _load_adjacent_package_identity(executable_path, release)
    return {
        "trust_root": _TRUST_ROOT_ID,
        "evidence_scope": LOCAL_EVIDENCE_SCOPE,
        "release_id": release["release_id"],
        "platform": release["platform"],
        "architecture": release["architecture"],
        "executable_path": executable_path,
        "executable_sha256": executable_sha256,
        "windows_file_identity": dict(file_identity),
        **package,
        "cli_version": release["cli_version"],
    }


def _process_failure_hint(stdout: str) -> str:
    """Return a fixed diagnostic label, never raw provider text or a verdict.

    Only the last JSON error event is considered. Earlier retry diagnostics and
    agent prose must not turn an unknown terminal failure into a confident cause.
    This hint never authorizes retries, model changes, or successful receipts.
    """
    payload: Mapping[str, Any] = {}
    for line in stdout[-262144:].splitlines()[-256:]:
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(event, dict) or event.get("type") not in ("error", "turn.failed"):
            continue
        error = event.get("error", event)
        payload = error if isinstance(error, dict) else {}
    code = payload.get("code")
    code_hints = {
        "usage_limit_reached": "usage_limit",
        "insufficient_quota": "usage_limit",
        "rate_limit_exceeded": "rate_limit",
        "invalid_api_key": "authentication",
        "authentication_error": "authentication",
        "context_length_exceeded": "context_limit",
    }
    if isinstance(code, str) and code in code_hints:
        return code_hints[code]
    message = payload.get("message")
    if not isinstance(message, str):
        return "unknown"
    message = message.lower()
    patterns = (
        ("service_capacity", ("selected model is at capacity",)),
        ("usage_limit", ("you've hit your usage limit", "usage limit reached", "insufficient quota")),
        ("rate_limit", ("rate limit exceeded", "rate limit reached")),
        ("authentication", ("authentication failed", "invalid api key", "please log in again")),
        ("context_limit", ("context length exceeded", "maximum context length")),
        ("connection", ("stream disconnected", "error sending request", "connection reset", "request timed out")),
    )
    for hint, markers in patterns:
        if any(marker in message for marker in markers):
            return hint
    return "unknown"


def _validate_clean_process_result(
    completed: Any,
    *,
    stage: str,
    expected_stdout: str | None = None,
    allow_stderr: bool = False,
) -> dict[str, Any]:
    returncode = getattr(completed, "returncode", None)
    stdout = str(getattr(completed, "stdout", "") or "")
    stderr = str(getattr(completed, "stderr", "") or "")
    stderr_sha256 = _sha256_text(stderr)
    if isinstance(returncode, bool) or returncode != 0:
        raise CodexSolProtocolError(
            f"{stage} failed with exit code {returncode}; "
            f"failure_hint={_process_failure_hint(stdout)}; "
            f"stdout_sha256={_sha256_text(stdout)}; "
            f"stderr_sha256={stderr_sha256}"
        )
    if stderr and not allow_stderr:
        raise CodexSolProtocolError(
            f"{stage} returned non-empty stderr; stderr_sha256={stderr_sha256}"
        )
    normalized_stdout = stdout.strip()
    if expected_stdout is not None and stdout != expected_stdout:
        raise CodexSolProtocolError(f"{stage} returned an unexpected version")
    return {
        "returncode": 0,
        "stdout": stdout,
        "stdout_normalized": normalized_stdout,
        "stdout_sha256": _sha256_text(stdout),
        "stderr_sha256": stderr_sha256,
        "stderr_empty": not bool(stderr),
    }


def _json_equal(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is right
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _json_equal(a, b) for a, b in zip(left, right)
        )
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _json_equal(left[key], right[key]) for key in left
        )
    return left == right


def _receipt_digest(receipt: Mapping[str, Any]) -> str:
    material = {
        key: value for key, value in dict(receipt).items()
        if key != "receipt_sha256"
    }
    return _sha256_text(_canonical_json(material))


def build_subprocess_env(
    source_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a minimal runtime environment without credential variables.

    ``CODEX_HOME`` is retained so the official executable can use its normal
    authentication store.  The adapter never opens that directory or reads an
    authentication file itself.
    """

    source = os.environ if source_env is None else source_env
    clean: dict[str, str] = {}
    for key, value in source.items():
        name = str(key)
        upper = name.upper()
        if any(part in upper for part in _SENSITIVE_ENV_PARTS):
            continue
        if upper in _RUNTIME_ENV_NAMES or upper.startswith("LC_"):
            clean[name] = str(value)
    return clean


def _probe_official_cli_version(
    executable_path: str,
    *,
    expected_version: str,
    child_env: Mapping[str, str],
    timeout_seconds: float,
) -> dict[str, Any]:
    command = [executable_path, "--version"]
    kwargs: dict[str, Any] = {
        "cwd": str(Path(executable_path).parent),
        "shell": False,
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "strict",
        "env": dict(child_env),
        "timeout": min(float(timeout_seconds), 30.0),
        "check": False,
    }
    if os.name == "nt" and getattr(subprocess, "CREATE_NO_WINDOW", 0):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        completed = subprocess.run(command, **kwargs)
    except PermissionError:
        raise CodexSolAccessError(
            "Windows denied the official Codex version probe"
        ) from None
    except FileNotFoundError:
        raise CodexSolExecutableError(
            "The official Codex executable disappeared before version probe"
        ) from None
    except subprocess.TimeoutExpired:
        raise CodexSolTimeoutError("Official Codex version probe timed out") from None
    except UnicodeError:
        raise CodexSolProtocolError(
            "Official Codex version output is not valid UTF-8"
        ) from None
    except OSError:
        raise CodexSolExecutableError(
            "The official Codex version probe could not be launched"
        ) from None
    evidence = _validate_clean_process_result(
        completed,
        stage="Official Codex version probe",
        expected_stdout=expected_version,
    )
    return {
        "command": command,
        "command_sha256": _sha256_text(_canonical_json(command)),
        "returncode": evidence["returncode"],
        "stdout_normalized": evidence["stdout_normalized"],
        "stdout_sha256": evidence["stdout_sha256"],
        "stderr_sha256": evidence["stderr_sha256"],
        "stderr_empty": evidence["stderr_empty"],
    }


def _check_nonnegative_integer(value: Any, keyword: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CodexSolProtocolError(
            f"Output schema keyword {keyword} must be a non-negative integer"
        )
    return value


def _check_schema(schema: Mapping[str, Any], path: str = "$") -> None:
    unsupported = sorted(set(schema) - _SCHEMA_KEYWORDS)
    if unsupported:
        raise CodexSolProtocolError(
            f"Unsupported output schema keyword at {path}: {unsupported[0]}"
        )
    schema_type = schema.get("type")
    if schema_type is not None and schema_type not in _SCHEMA_TYPES:
        raise CodexSolProtocolError(f"Unsupported output schema type at {path}")

    if "properties" in schema:
        properties = schema["properties"]
        if not isinstance(properties, Mapping):
            raise CodexSolProtocolError(f"properties must be an object at {path}")
        for name, child in properties.items():
            if not isinstance(name, str) or not isinstance(child, Mapping):
                raise CodexSolProtocolError(
                    f"properties contains an invalid schema at {path}"
                )
            _check_schema(child, f"{path}.properties.{name}")
    if "required" in schema:
        required = schema["required"]
        if (
            not isinstance(required, list)
            or any(not isinstance(name, str) for name in required)
            or len(set(required)) != len(required)
        ):
            raise CodexSolProtocolError(f"required must contain unique strings at {path}")
    if "additionalProperties" in schema and not isinstance(
        schema["additionalProperties"], bool
    ):
        raise CodexSolProtocolError(
            f"additionalProperties must be boolean at {path}"
        )
    if "items" in schema:
        if not isinstance(schema["items"], Mapping):
            raise CodexSolProtocolError(f"items must be a schema object at {path}")
        _check_schema(schema["items"], f"{path}.items")
    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, list) or not enum:
            raise CodexSolProtocolError(f"enum must be a non-empty array at {path}")
    for keyword in ("minLength", "maxLength", "minItems", "maxItems"):
        if keyword in schema:
            _check_nonnegative_integer(schema[keyword], keyword)
    if (
        "minLength" in schema
        and "maxLength" in schema
        and schema["minLength"] > schema["maxLength"]
    ):
        raise CodexSolProtocolError(f"minLength exceeds maxLength at {path}")
    if (
        "minItems" in schema
        and "maxItems" in schema
        and schema["minItems"] > schema["maxItems"]
    ):
        raise CodexSolProtocolError(f"minItems exceeds maxItems at {path}")
    for keyword in ("anyOf", "oneOf"):
        if keyword not in schema:
            continue
        branches = schema[keyword]
        if (
            not isinstance(branches, list)
            or not branches
            or any(not isinstance(branch, Mapping) for branch in branches)
        ):
            raise CodexSolProtocolError(
                f"{keyword} must contain schema objects at {path}"
            )
        for index, branch in enumerate(branches):
            _check_schema(branch, f"{path}.{keyword}[{index}]")


def _value_matches_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and not (isinstance(value, float) and not math.isfinite(value))
        )
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return False


def _instance_errors(
    schema: Mapping[str, Any],
    value: Any,
    path: str = "$",
) -> list[str]:
    errors: list[str] = []
    expected_type = schema.get("type")
    if expected_type is not None and not _value_matches_type(value, expected_type):
        return [f"{path} has the wrong type"]
    if "enum" in schema and not any(
        _json_equal(value, candidate) for candidate in schema["enum"]
    ):
        errors.append(f"{path} is not an allowed enum value")
    if "const" in schema and not _json_equal(value, schema["const"]):
        errors.append(f"{path} does not match const")

    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        required = schema.get("required") or []
        for name in required:
            if name not in value:
                errors.append(f"{path} is missing required property {name}")
        for name, child_value in value.items():
            child_schema = properties.get(name)
            if child_schema is not None:
                errors.extend(
                    _instance_errors(child_schema, child_value, f"{path}.{name}")
                )
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path} has an additional property {name}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path} has too few items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path} has too many items")
        if "items" in schema:
            for index, child_value in enumerate(value):
                errors.extend(
                    _instance_errors(schema["items"], child_value, f"{path}[{index}]")
                )
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path} is shorter than minLength")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path} is longer than maxLength")

    if "anyOf" in schema:
        matches = sum(
            not _instance_errors(branch, value, path)
            for branch in schema["anyOf"]
        )
        if matches < 1:
            errors.append(f"{path} does not match anyOf")
    if "oneOf" in schema:
        matches = sum(
            not _instance_errors(branch, value, path)
            for branch in schema["oneOf"]
        )
        if matches != 1:
            errors.append(f"{path} does not match exactly one oneOf branch")
    return errors


def validate_structured_output(
    output_schema: Mapping[str, Any],
    value: Any,
) -> None:
    """Validate the supported JSON Schema subset without trusting the CLI."""

    _check_schema(output_schema)
    errors = _instance_errors(output_schema, value)
    if errors:
        raise CodexSolProtocolError(
            f"Codex structured output failed local schema validation: {errors[0]}"
        )


def resolve_executable(
    executable: str | os.PathLike[str] | None = None,
    *,
    which: Which = shutil.which,
) -> str:
    """Resolve an explicit path or find ``codex`` on PATH.

    An explicit value is treated as a path, not another command lookup.  This
    prevents a typo in a configured executable from silently selecting a
    different installation.
    """

    if executable is not None:
        raw = str(executable).strip()
        if not raw:
            raise CodexSolExecutableError("Codex executable path is empty")
        path = Path(raw).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise CodexSolExecutableError(
                "Configured Codex executable path does not exist"
            )
        return str(path.resolve())

    candidate = which("codex")
    if not candidate:
        raise CodexSolExecutableError("Codex executable was not found on PATH")
    path = Path(candidate)
    if not path.is_file():
        raise CodexSolExecutableError("Codex PATH entry is not a file")
    return str(path.resolve())


def build_command(
    executable: str,
    *,
    model: str,
    schema_path: str | os.PathLike[str],
    output_path: str | os.PathLike[str],
) -> list[str]:
    """Build the fixed, non-interactive command policy."""

    target = str(model or "").strip()
    if not target:
        raise ValueError("A target model is required; no fallback is allowed")
    return [
        str(executable),
        "exec",
        "--model",
        target,
        "--sandbox",
        "read-only",
        "--ephemeral",
        "--json",
        "--ignore-user-config",
        "--ignore-rules",
        "--output-schema",
        str(schema_path),
        "-o",
        str(output_path),
        "--skip-git-repo-check",
        "-",
    ]


def _parse_jsonl(stdout: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(str(stdout or "").splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            event = _strict_json_loads(raw_line)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CodexSolProtocolError(
                f"Codex JSONL is malformed at line {line_number}"
            ) from None
        if not isinstance(event, dict) or not str(event.get("type") or ""):
            raise CodexSolProtocolError(
                f"Codex JSONL event {line_number} is not a typed object"
            )
        events.append(event)
    if not events:
        raise CodexSolProtocolError("Codex returned no JSONL events")
    return events


def _reported_models(events: Sequence[Mapping[str, Any]]) -> set[str]:
    """Read only trusted event-envelope model fields.

    Agent message content is intentionally not searched: model-generated JSON
    must never be able to assert its own provenance.
    """

    found: set[str] = set()
    for event in events:
        event_type = str(event.get("type") or "")
        for key in _MODEL_PROVENANCE_FIELDS.get(event_type, ()):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                found.add(value.strip())
    return found


def _agent_message_text(event: Mapping[str, Any]) -> str | None:
    if event.get("type") == "item.completed":
        item = event.get("item")
        if isinstance(item, Mapping) and item.get("type") == "agent_message":
            text = item.get("text")
            return text if isinstance(text, str) else None
    return None


def _validate_events(
    events: Sequence[Mapping[str, Any]],
    *,
    target_model: str,
) -> dict[str, Any]:
    types = [str(event.get("type") or "") for event in events]
    if "turn.failed" in types:
        raise CodexSolProtocolError("Codex reported turn.failed")
    # Codex may emit an ``error`` diagnostic while its own transport retries,
    # then still finish with a complete turn, output file, schema-valid final
    # message and exit code 0.  The completed lifecycle is authoritative; an
    # unrecovered failure has no valid turn.completed/output and still fails
    # closed below.
    diagnostic_error_event_count = types.count("error")

    started = [event for event in events if event.get("type") == "thread.started"]
    turn_started = [event for event in events if event.get("type") == "turn.started"]
    completed = [event for event in events if event.get("type") == "turn.completed"]
    if len(started) != 1:
        raise CodexSolProtocolError("Exactly one thread.started event is required")
    if len(turn_started) != 1:
        raise CodexSolProtocolError("Exactly one turn.started event is required")
    if len(completed) != 1:
        raise CodexSolProtocolError("Exactly one turn.completed event is required")
    thread_id = str(started[0].get("thread_id") or "").strip()
    if not thread_id:
        raise CodexSolProtocolError("thread.started is missing thread_id")
    thread_index = events.index(started[0])
    turn_index = events.index(turn_started[0])
    completed_index = events.index(completed[0])
    if not thread_index < turn_index < completed_index:
        raise CodexSolProtocolError("Codex lifecycle events are out of order")

    message_rows = [
        (index, text)
        for index, event in enumerate(events)
        if (text := _agent_message_text(event)) is not None and text.strip()
    ]
    messages = [text for _index, text in message_rows]
    if not messages:
        raise CodexSolProtocolError("Codex returned no final agent_message")
    if turn_index >= message_rows[0][0] or message_rows[-1][0] >= completed_index:
        raise CodexSolProtocolError("Codex agent_message lifecycle is out of order")
    if completed_index != len(events) - 1:
        raise CodexSolProtocolError("turn.completed must be the final JSONL event")
    final_message = messages[-1]

    usage = completed[0].get("usage")
    if not isinstance(usage, Mapping) or not usage:
        raise CodexSolProtocolError("turn.completed is missing usage")
    for required_usage in ("input_tokens", "output_tokens"):
        if required_usage not in usage:
            raise CodexSolProtocolError(
                f"turn.completed usage is missing {required_usage}"
            )
    normalized_usage: dict[str, int] = {}
    for key, value in usage.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise CodexSolProtocolError("turn.completed usage is invalid")
        normalized_usage[str(key)] = value

    models = _reported_models(events)
    if len(models) > 1:
        raise CodexSolProtocolError("Codex reported contradictory model provenance")
    reported_model = next(iter(models), None)
    if reported_model is not None and reported_model != target_model:
        raise CodexSolProtocolError("Codex returned a different model than requested")

    return {
        "thread_id": thread_id,
        "final_message": final_message,
        "usage": normalized_usage,
        "reported_model": reported_model,
        "reported_model_source": (
            "trusted_cli_event" if reported_model is not None else None
        ),
        "diagnostic_error_event_count": diagnostic_error_event_count,
    }


def _formal_model_provenance(reported_model: str | None) -> dict[str, Any]:
    if reported_model is not None and reported_model != DEFAULT_MODEL:
        raise CodexSolProtocolError("Codex returned a different model than requested")
    return {
        "status": (
            FORMAL_EVENT_REPORTED_STATUS
            if reported_model is not None
            else FORMAL_PROVENANCE_STATUS
        ),
        "basis": dict(_FORMAL_PROVENANCE_BASIS),
        "requested_model": DEFAULT_MODEL,
        "command_bound_model": DEFAULT_MODEL,
        "reported_model": reported_model,
        "reported_model_source": (
            "trusted_cli_event" if reported_model is not None else None
        ),
    }


def _nonformal_model_provenance(
    *,
    target_model: str,
    reported_model: str | None,
    custom_runner: bool,
) -> dict[str, Any]:
    return {
        "status": (
            "custom_runner_nonformal"
            if custom_runner
            else "untrusted_subprocess_nonformal"
        ),
        "basis": (
            "custom_runner_event_observation"
            if custom_runner
            else "untrusted_command_event_observation"
        ),
        "requested_model": target_model,
        "command_bound_model": None if custom_runner else target_model,
        "reported_model": reported_model,
        "reported_model_source": (
            "trusted_cli_event" if reported_model is not None else None
        ),
    }


def _validate_official_release_evidence(
    evidence: Any,
    *,
    executable_path: str,
    executable_sha256: str,
) -> dict[str, Any]:
    if not isinstance(evidence, Mapping):
        raise CodexSolProtocolError("Formal official release evidence is missing")
    release = _trusted_release_for_hash(executable_sha256)
    expected_static = {
        "trust_root": _TRUST_ROOT_ID,
        "evidence_scope": LOCAL_EVIDENCE_SCOPE,
        "release_id": release["release_id"],
        "platform": release["platform"],
        "architecture": release["architecture"],
        "executable_path": executable_path,
        "executable_sha256": executable_sha256,
        "native_package_alias": release["native_package_alias"],
        "native_package_directory": release["native_package_directory"],
        "native_package_name": release["native_package_name"],
        "native_package_version": release["native_package_version"],
        "native_executable_relative_path": release[
            "native_executable_relative_path"
        ],
        "root_package_directory": release["root_package_directory"],
        "root_package_name": release["root_package_name"],
        "root_package_version": release["root_package_version"],
        "native_dependency_spec": release["native_dependency_spec"],
        "cli_version": release["cli_version"],
    }
    for key, value in expected_static.items():
        if evidence.get(key) != value:
            raise CodexSolProtocolError(
                f"Formal official release evidence is inconsistent: {key}"
            )
    layout = _official_package_layout(executable_path, release)
    for layer in ("native", "root"):
        path_field = f"{layer}_package_json_path"
        hash_field = f"{layer}_package_json_sha256"
        expected_path = layout[path_field]
        package_path = evidence.get(path_field)
        if (
            not isinstance(expected_path, Path)
            or not isinstance(package_path, str)
            or not Path(package_path).is_absolute()
            or Path(package_path) != expected_path
        ):
            raise CodexSolProtocolError(
                f"Formal {layer} package.json path is invalid"
            )
        if not _is_sha256(evidence.get(hash_field)):
            raise CodexSolProtocolError(
                f"Formal {layer} package.json SHA-256 is invalid"
            )
    file_identity = evidence.get("windows_file_identity")
    if not isinstance(file_identity, Mapping):
        raise CodexSolProtocolError("Formal Windows file identity is missing")
    if (
        len(str(file_identity.get("volume_serial") or "")) != 8
        or len(str(file_identity.get("file_id") or "")) != 16
        or any(
            char not in "0123456789abcdef"
            for char in (
                str(file_identity.get("volume_serial") or "")
                + str(file_identity.get("file_id") or "")
            )
        )
    ):
        raise CodexSolProtocolError("Formal Windows file identity is invalid")
    probe = evidence.get("version_probe")
    if not isinstance(probe, Mapping):
        raise CodexSolProtocolError("Formal Codex version probe evidence is missing")
    expected_probe_command = [executable_path, "--version"]
    if (
        probe.get("command") != expected_probe_command
        or probe.get("command_sha256")
        != _sha256_text(_canonical_json(expected_probe_command))
        or isinstance(probe.get("returncode"), bool)
        or probe.get("returncode") != 0
        or probe.get("stdout_normalized") != release["cli_version"]
        or probe.get("stdout_sha256")
        != _sha256_text(release["cli_version_stdout"])
        or probe.get("stderr_empty") is not True
        or probe.get("stderr_sha256") != _EMPTY_SHA256
    ):
        raise CodexSolProtocolError("Formal Codex version probe evidence is invalid")
    return dict(evidence)


def _build_formal_receipt(result: Mapping[str, Any]) -> dict[str, Any]:
    """Build a receipt from an internally verified formal execution result."""

    provenance = result.get("model_provenance")
    if not isinstance(provenance, Mapping):
        raise CodexSolProtocolError("Model provenance is missing")
    reported_value = provenance.get("reported_model")
    if reported_value is not None and (
        not isinstance(reported_value, str) or not reported_value.strip()
    ):
        raise CodexSolProtocolError("Reported model evidence is invalid")
    reported = reported_value.strip() if isinstance(reported_value, str) else None
    expected_provenance = _formal_model_provenance(reported)
    if not _json_equal(dict(provenance), expected_provenance):
        raise CodexSolProtocolError(
            "Formal provenance basis is missing, stale, or inconsistent"
        )
    if (
        result.get("target_model") != DEFAULT_MODEL
        or result.get("requested_model") != DEFAULT_MODEL
        or result.get("command_bound_model") != DEFAULT_MODEL
        or result.get("reported_model") != reported
        or result.get("provenance_basis") != expected_provenance["status"]
    ):
        raise CodexSolProtocolError("Formal model bindings are inconsistent")
    if result.get("adapter_version") != ADAPTER_VERSION:
        raise CodexSolProtocolError("Adapter version evidence is missing or stale")
    if result.get("protocol_version") != PROTOCOL_VERSION:
        raise CodexSolProtocolError("Protocol version evidence is missing or stale")
    required_hashes = (
        "prompt_sha256",
        "output_schema_sha256",
        "response_sha256",
        "event_stream_sha256",
        "executable_sha256",
        "executable_sha256_before",
        "executable_sha256_after",
        "command_policy_sha256",
    )
    for field in required_hashes:
        value = str(result.get(field) or "")
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise CodexSolProtocolError(f"Formal receipt field is invalid: {field}")
    if not (
        result["executable_sha256"]
        == result["executable_sha256_before"]
        == result["executable_sha256_after"]
    ):
        raise CodexSolProtocolError("Formal executable SHA-256 binding is inconsistent")
    executable_path = str(result.get("executable_path") or "")
    if (
        not executable_path
        or not Path(executable_path).is_absolute()
        or result.get("evidence_scope") != LOCAL_EVIDENCE_SCOPE
    ):
        raise CodexSolProtocolError("Formal executable path or evidence scope is invalid")
    official_release = _validate_official_release_evidence(
        result.get("official_release"),
        executable_path=executable_path,
        executable_sha256=result["executable_sha256"],
    )
    success_stderr_empty = result.get("success_stderr_empty")
    success_stderr_sha256 = str(result.get("success_stderr_sha256") or "")
    diagnostic_error_event_count = result.get(
        "diagnostic_error_event_count", 0
    )
    if (
        isinstance(result.get("process_returncode"), bool)
        or result.get("process_returncode") != 0
        or not isinstance(success_stderr_empty, bool)
        or not _is_sha256(success_stderr_sha256)
        or (
            success_stderr_empty
            and success_stderr_sha256 != _EMPTY_SHA256
        )
        or (
            not success_stderr_empty
            and success_stderr_sha256 == _EMPTY_SHA256
        )
        or isinstance(diagnostic_error_event_count, bool)
        or not isinstance(diagnostic_error_event_count, int)
        or diagnostic_error_event_count < 0
    ):
        raise CodexSolProtocolError("Formal main process success evidence is invalid")

    receipt: dict[str, Any] = {
        "version": RECEIPT_VERSION,
        "adapter_version": ADAPTER_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "origin": "codex_exec_official_cli_noninteractive",
        "evidence_scope": LOCAL_EVIDENCE_SCOPE,
        "formal_receipt_eligible": True,
        "provenance_basis": expected_provenance["status"],
        "thread_id": str(result.get("thread_id") or ""),
        "target_model": DEFAULT_MODEL,
        "requested_model": DEFAULT_MODEL,
        "command_bound_model": DEFAULT_MODEL,
        "reported_model": reported,
        "reported_model_source": expected_provenance["reported_model_source"],
        "model_provenance": expected_provenance,
        "prompt_sha256": result["prompt_sha256"],
        "output_schema_sha256": result["output_schema_sha256"],
        "response_sha256": result["response_sha256"],
        "event_stream_sha256": result["event_stream_sha256"],
        "executable_path": executable_path,
        "executable_sha256": result["executable_sha256"],
        "executable_sha256_before": result["executable_sha256_before"],
        "executable_sha256_after": result["executable_sha256_after"],
        "command_policy_sha256": result["command_policy_sha256"],
        "official_release": official_release,
        "process_returncode": 0,
        "success_stderr_sha256": success_stderr_sha256,
        "success_stderr_empty": success_stderr_empty,
        "diagnostic_error_event_count": diagnostic_error_event_count,
        "usage": dict(result.get("usage") or {}),
        "execution_policy": {
            "sandbox": "read-only",
            "ephemeral": True,
            "jsonl": True,
            "output_schema": True,
            "ignore_user_config": True,
            "ignore_rules": True,
            "sanitized_environment": True,
            "shell": False,
            "windows_executable_lock": "deny_write_and_delete_sharing",
            "success_stderr_required_empty": False,
            "success_stderr_policy": (
                "hash_diagnostic_and_require_complete_validated_output"
            ),
        },
    }
    if not receipt["thread_id"] or not receipt["usage"]:
        raise CodexSolProtocolError("Formal receipt lifecycle evidence is incomplete")
    receipt["receipt_sha256"] = _receipt_digest(receipt)
    return receipt


def _is_sha256(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _bundle_digest(bundle: Mapping[str, Any]) -> str:
    material = {
        key: value for key, value in dict(bundle).items()
        if key != "bundle_sha256"
    }
    return _sha256_text(_canonical_json(material))


def _build_formal_bundle(
    *,
    prompt_sha256: str,
    output_schema: Mapping[str, Any],
    output_text: str,
    events: Sequence[Mapping[str, Any]],
    command: Sequence[str],
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    bundle: dict[str, Any] = {
        "version": FORMAL_BUNDLE_VERSION,
        "adapter_version": ADAPTER_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        # Deliberately retain only the prompt digest; callers must provide the
        # expected digest during verification so prompt contents are not copied
        # into logs or durable evidence bundles.
        "prompt_sha256": prompt_sha256,
        "output_schema": dict(output_schema),
        "output_text": output_text,
        "events": [dict(event) for event in events],
        "command": [str(item) for item in command],
        "receipt": dict(receipt),
    }
    bundle["bundle_sha256"] = _bundle_digest(bundle)
    return bundle


def verify_formal_bundle(
    bundle: Mapping[str, Any],
    *,
    pinned_executable_sha256: str,
    expected_prompt_sha256: str,
) -> bool:
    """Independently replay every durable formal-evidence binding.

    This protects local operations against accidental wiring, stale releases,
    and evidence edits.  It is not a cryptographic remote attestation against
    an administrator who can replace this module or alter process memory.
    """

    if not isinstance(bundle, Mapping):
        raise CodexSolProtocolError("Formal bundle must be an object")
    pinned = str(pinned_executable_sha256 or "").lower()
    expected_prompt = str(expected_prompt_sha256 or "").lower()
    if not _is_sha256(pinned):
        raise CodexSolProtocolError("Pinned executable SHA-256 is invalid")
    _trusted_release_for_hash(pinned)
    if not _is_sha256(expected_prompt):
        raise CodexSolProtocolError("Expected prompt SHA-256 is invalid")
    if bundle.get("version") != FORMAL_BUNDLE_VERSION:
        raise CodexSolProtocolError("Formal bundle version is missing or stale")
    if bundle.get("adapter_version") != ADAPTER_VERSION:
        raise CodexSolProtocolError("Formal bundle adapter version is missing or stale")
    if bundle.get("protocol_version") != PROTOCOL_VERSION:
        raise CodexSolProtocolError("Formal bundle protocol version is missing or stale")
    if not _is_sha256(bundle.get("bundle_sha256")):
        raise CodexSolProtocolError("Formal bundle digest is invalid")
    if _bundle_digest(bundle) != bundle.get("bundle_sha256"):
        raise CodexSolProtocolError("Formal bundle digest mismatch")
    if str(bundle.get("prompt_sha256") or "").lower() != expected_prompt:
        raise CodexSolProtocolError("Formal bundle prompt binding mismatch")

    schema = bundle.get("output_schema")
    events = bundle.get("events")
    command = bundle.get("command")
    receipt = bundle.get("receipt")
    output_text = bundle.get("output_text")
    if not isinstance(schema, Mapping) or not schema:
        raise CodexSolProtocolError("Formal bundle output schema is invalid")
    if (
        not isinstance(events, list)
        or not events
        or any(not isinstance(event, Mapping) for event in events)
    ):
        raise CodexSolProtocolError("Formal bundle event stream is invalid")
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(item, str) for item in command)
    ):
        raise CodexSolProtocolError("Formal bundle command is invalid")
    if not isinstance(receipt, Mapping):
        raise CodexSolProtocolError("Formal bundle receipt is invalid")
    if not isinstance(output_text, str):
        raise CodexSolProtocolError("Formal bundle output text is invalid")

    receipt_executable = str(receipt.get("executable_path") or "")
    if not command or command[0] != receipt_executable:
        raise CodexSolProtocolError("Formal bundle executable path binding mismatch")
    resolved_executable = _validate_formal_executable_path(command[0])
    if resolved_executable != command[0]:
        raise CodexSolProtocolError("Formal bundle executable path is not canonical")

    child_env = build_subprocess_env()
    with _hold_formal_executable_lock(resolved_executable) as file_identity:
        current_sha256 = _file_sha256(resolved_executable)
        if current_sha256 != pinned:
            raise CodexSolProtocolError(
                "Formal bundle executable does not match the pinned official release"
            )
        current_release = _inspect_official_release(
            resolved_executable,
            current_sha256,
            file_identity,
        )
        trusted_release = _trusted_release_for_hash(current_sha256)
        current_release["version_probe"] = _probe_official_cli_version(
            resolved_executable,
            expected_version=trusted_release["cli_version_stdout"],
            child_env=child_env,
            timeout_seconds=30.0,
        )
        final_sha256 = _file_sha256(resolved_executable)
        final_release = _inspect_official_release(
            resolved_executable,
            final_sha256,
            file_identity,
        )
        if final_sha256 != pinned or final_release != {
            key: value
            for key, value in current_release.items()
            if key != "version_probe"
        }:
            raise CodexSolProtocolError(
                "Formal bundle official release identity changed during replay"
            )

    for model_field in ("target_model", "requested_model", "command_bound_model"):
        if receipt.get(model_field) != DEFAULT_MODEL:
            raise CodexSolProtocolError(
                f"Formal bundle {model_field} must be exactly {DEFAULT_MODEL}"
            )
    evidence = _validate_events(events, target_model=DEFAULT_MODEL)
    if output_text.strip() != evidence["final_message"].strip():
        raise CodexSolProtocolError("Formal bundle output and event message differ")
    try:
        structured_output = _strict_json_loads(output_text)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise CodexSolProtocolError("Formal bundle output is not valid JSON") from None
    validate_structured_output(schema, structured_output)

    if command.count("--output-schema") != 1 or command.count("-o") != 1:
        raise CodexSolProtocolError("Formal bundle command paths are ambiguous")
    try:
        schema_path = command[command.index("--output-schema") + 1]
        output_path = command[command.index("-o") + 1]
    except IndexError:
        raise CodexSolProtocolError("Formal bundle command paths are incomplete") from None
    expected_command = build_command(
        command[0],
        model=DEFAULT_MODEL,
        schema_path=schema_path,
        output_path=output_path,
    )
    if command != expected_command:
        raise CodexSolProtocolError("Formal bundle command policy mismatch")

    schema_text = _canonical_json(dict(schema))
    event_stream = "\n".join(_canonical_json(event) for event in events)
    formal_provenance = _formal_model_provenance(evidence["reported_model"])
    replay: dict[str, Any] = {
        "thread_id": evidence["thread_id"],
        "target_model": DEFAULT_MODEL,
        "requested_model": DEFAULT_MODEL,
        "command_bound_model": DEFAULT_MODEL,
        "reported_model": evidence["reported_model"],
        "provenance_basis": formal_provenance["status"],
        "model_provenance": formal_provenance,
        "prompt_sha256": expected_prompt,
        "output_schema_sha256": _sha256_text(schema_text),
        "response_sha256": _sha256_text(output_text),
        "event_stream_sha256": _sha256_text(event_stream),
        "evidence_scope": LOCAL_EVIDENCE_SCOPE,
        "executable_path": resolved_executable,
        "executable_sha256": pinned,
        "executable_sha256_before": pinned,
        "executable_sha256_after": pinned,
        "command_policy_sha256": _sha256_text(_canonical_json(command)),
        "official_release": current_release,
        "process_returncode": 0,
        "success_stderr_sha256": receipt.get("success_stderr_sha256"),
        "success_stderr_empty": receipt.get("success_stderr_empty"),
        "diagnostic_error_event_count": evidence[
            "diagnostic_error_event_count"
        ],
        "usage": evidence["usage"],
        "adapter_version": ADAPTER_VERSION,
        "protocol_version": PROTOCOL_VERSION,
    }
    expected_receipt = _build_formal_receipt(replay)
    if not _json_equal(dict(receipt), expected_receipt):
        raise CodexSolProtocolError("Formal bundle receipt replay mismatch")
    return True


def run_codex_sol(
    prompt: str,
    output_schema: Mapping[str, Any],
    *,
    executable: str | os.PathLike[str] | None = None,
    model: str = DEFAULT_MODEL,
    timeout_seconds: float = 900.0,
    runner: Runner | None = None,
    which: Which = shutil.which,
    temp_parent: str | os.PathLike[str] | None = None,
    formal: bool = False,
    pinned_executable_sha256: str | None = None,
) -> dict[str, Any]:
    """Run one schema-bound Codex turn and return verified evidence.

    The function never retries with another model.  Custom runners are useful
    for parser tests but are always non-formal.  Formal mode calls the standard
    library subprocess transport directly, requires the embedded official
    release identity, and holds a Windows deny-write/delete handle from the
    first hash through the version probe, main execution, and final hash.
    """

    prompt_text = str(prompt or "")
    if not prompt_text.strip():
        raise ValueError("Prompt must not be empty")
    target_model = str(model or "").strip()
    if not target_model:
        raise ValueError("A target model is required; no fallback is allowed")
    formal_requested = formal is True
    if formal_requested and (
        not isinstance(model, str) or model != DEFAULT_MODEL
    ):
        raise CodexSolProtocolError(
            f"Formal mode requires exact model {DEFAULT_MODEL}"
        )
    if not isinstance(output_schema, Mapping) or not output_schema:
        raise ValueError("A non-empty JSON output schema is required")
    _check_schema(output_schema)
    try:
        timeout_value = float(timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout_seconds must be positive") from exc
    if timeout_value <= 0:
        raise ValueError("timeout_seconds must be positive")

    schema_text = _canonical_json(dict(output_schema))
    executable_path = resolve_executable(executable, which=which)
    if formal_requested and runner is not None:
        raise CodexSolProtocolError("Formal mode does not allow a custom runner")
    pinned_sha256 = str(pinned_executable_sha256 or "").strip().lower()
    if formal_requested:
        executable_path = _validate_formal_executable_path(executable_path)
        if not _is_sha256(pinned_sha256):
            raise CodexSolProtocolError(
                "Formal mode requires pinned_executable_sha256"
            )
        _trusted_release_for_hash(pinned_sha256)

    executable_sha256_before: str | None = None
    executable_sha256_after: str | None = None
    official_release: dict[str, Any] | None = None

    with tempfile.TemporaryDirectory(
        prefix="codex-sol-",
        dir=str(temp_parent) if temp_parent is not None else None,
    ) as temp_dir:
        schema_path = Path(temp_dir) / "output.schema.json"
        output_path = Path(temp_dir) / "final-output.json"
        schema_path.write_text(schema_text, encoding="utf-8", newline="\n")
        command = build_command(
            executable_path,
            model=target_model,
            schema_path=schema_path,
            output_path=output_path,
        )
        command_policy_sha256 = _sha256_text(_canonical_json(command))
        child_env = build_subprocess_env()
        run_kwargs: dict[str, Any] = {
            "input": prompt_text,
            "cwd": temp_dir,
            "shell": False,
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "strict",
            "env": child_env,
            "timeout": timeout_value,
            "check": False,
        }
        if os.name == "nt" and getattr(subprocess, "CREATE_NO_WINDOW", 0):
            run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        def invoke_main_process() -> Any:
            try:
                if formal_requested:
                    return subprocess.run(command, **run_kwargs)
                if runner is not None:
                    return runner(command, **run_kwargs)
                return subprocess.run(command, **run_kwargs)
            except PermissionError:
                raise CodexSolAccessError(
                    "Windows or the sandbox denied access to the Codex executable"
                ) from None
            except FileNotFoundError:
                raise CodexSolExecutableError(
                    "The resolved Codex executable disappeared before launch"
                ) from None
            except OSError as exc:
                if getattr(exc, "winerror", None) == 5:
                    raise CodexSolAccessError(
                        "Windows or the sandbox denied access to the Codex executable"
                    ) from None
                raise CodexSolExecutableError(
                    "The Codex executable could not be launched"
                ) from None
            except subprocess.TimeoutExpired:
                raise CodexSolTimeoutError("Codex exec timed out") from None
            except UnicodeError:
                raise CodexSolProtocolError("Codex output is not valid UTF-8") from None

        if formal_requested:
            with _hold_formal_executable_lock(executable_path) as file_identity:
                executable_sha256_before = _file_sha256(executable_path)
                if executable_sha256_before != pinned_sha256:
                    raise CodexSolProtocolError(
                        "Codex executable does not match the pinned official release"
                    )
                official_release = _inspect_official_release(
                    executable_path,
                    executable_sha256_before,
                    file_identity,
                )
                trusted_release = _trusted_release_for_hash(
                    executable_sha256_before
                )
                official_release["version_probe"] = _probe_official_cli_version(
                    executable_path,
                    expected_version=trusted_release["cli_version_stdout"],
                    child_env=child_env,
                    timeout_seconds=timeout_value,
                )
                completed = invoke_main_process()
                executable_sha256_after = _file_sha256(executable_path)
                final_release = _inspect_official_release(
                    executable_path,
                    executable_sha256_after,
                    file_identity,
                )
                if (
                    executable_sha256_after != executable_sha256_before
                    or executable_sha256_after != pinned_sha256
                    or final_release
                    != {
                        key: value
                        for key, value in official_release.items()
                        if key != "version_probe"
                    }
                ):
                    raise CodexSolProtocolError(
                        "Codex official release identity changed during formal execution"
                    )
                process_evidence = _validate_clean_process_result(
                    completed,
                    stage="Codex exec",
                    allow_stderr=True,
                )
        else:
            executable_sha256_before = _file_sha256(executable_path)
            completed = invoke_main_process()
            process_evidence = _validate_clean_process_result(
                completed,
                stage="Codex exec",
            ) if getattr(completed, "returncode", None) != 0 else {
                "returncode": 0,
                "stdout": str(getattr(completed, "stdout", "") or ""),
                "stderr_sha256": _sha256_text(
                    str(getattr(completed, "stderr", "") or "")
                ),
                "stderr_empty": not bool(
                    str(getattr(completed, "stderr", "") or "")
                ),
            }

        if formal_requested and official_release is None:
            raise CodexSolProtocolError("Formal official release evidence is missing")

        stdout = str(getattr(completed, "stdout", "") or "")
        stderr = str(getattr(completed, "stderr", "") or "")
        returncode = getattr(completed, "returncode", None)
        if isinstance(returncode, bool) or returncode != 0:
            stderr_digest = _sha256_text(stderr)
            raise CodexSolProtocolError(
                f"Codex exec failed with exit code {returncode}; "
                f"stderr_sha256={stderr_digest}"
            )

        events = _parse_jsonl(stdout)
        evidence = _validate_events(events, target_model=target_model)
        if not output_path.is_file():
            raise CodexSolProtocolError("Codex did not write the final output file")
        try:
            output_text = output_path.read_text(encoding="utf-8")
        except UnicodeError:
            raise CodexSolProtocolError("Codex final output is not valid UTF-8") from None
        if output_text.strip() != evidence["final_message"].strip():
            raise CodexSolProtocolError(
                "Codex final output file does not match the final agent_message"
            )
        try:
            structured_output = _strict_json_loads(output_text)
        except (ValueError, json.JSONDecodeError):
            raise CodexSolProtocolError(
                "Codex final agent_message is not valid JSON"
            ) from None
        validate_structured_output(output_schema, structured_output)

        event_stream = "\n".join(
            _canonical_json(event) for event in events
        )
        model_provenance = (
            _formal_model_provenance(evidence["reported_model"])
            if formal_requested
            else _nonformal_model_provenance(
                target_model=target_model,
                reported_model=evidence["reported_model"],
                custom_runner=runner is not None,
            )
        )
        result: dict[str, Any] = {
            "status": "completed",
            "thread_id": evidence["thread_id"],
            "target_model": target_model,
            "requested_model": target_model,
            "command_bound_model": (
                DEFAULT_MODEL
                if formal_requested
                else model_provenance["command_bound_model"]
            ),
            "reported_model": evidence["reported_model"],
            "provenance_basis": model_provenance["status"],
            "model_provenance": model_provenance,
            "formal_receipt_eligible": formal_requested,
            "evidence_scope": (
                LOCAL_EVIDENCE_SCOPE if formal_requested else "nonformal_execution"
            ),
            "prompt_sha256": _sha256_text(prompt_text),
            "output_schema_sha256": _sha256_text(schema_text),
            "response_sha256": _sha256_text(output_text),
            "event_stream_sha256": _sha256_text(event_stream),
            "executable_path": executable_path,
            "executable_sha256": executable_sha256_before,
            "executable_sha256_before": executable_sha256_before,
            "executable_sha256_after": executable_sha256_after,
            "command_policy_sha256": command_policy_sha256,
            "official_release": official_release,
            "process_returncode": process_evidence["returncode"],
            "success_stderr_sha256": process_evidence["stderr_sha256"],
            "success_stderr_empty": process_evidence["stderr_empty"],
            "diagnostic_error_event_count": evidence[
                "diagnostic_error_event_count"
            ],
            "adapter_version": ADAPTER_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "stderr_sha256": _sha256_text(stderr),
            "usage": evidence["usage"],
            "output": structured_output,
        }
        result["formal_receipt"] = (
            _build_formal_receipt(result)
            if result["formal_receipt_eligible"]
            else None
        )
        result["formal_bundle"] = (
            _build_formal_bundle(
                prompt_sha256=result["prompt_sha256"],
                output_schema=output_schema,
                output_text=output_text,
                events=events,
                command=command,
                receipt=result["formal_receipt"],
            )
            if result["formal_receipt"] is not None
            else None
        )
        return result


__all__ = [
    "DEFAULT_MODEL",
    "ADAPTER_VERSION",
    "CodexSolError",
    "CodexSolExecutableError",
    "CodexSolAccessError",
    "CodexSolTimeoutError",
    "CodexSolProtocolError",
    "resolve_executable",
    "build_command",
    "build_subprocess_env",
    "validate_structured_output",
    "verify_formal_bundle",
    "run_codex_sol",
]
