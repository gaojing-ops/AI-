"""Workspace-scoped, current-Windows-user DPAPI vault outside the source tree."""
import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import tempfile


def key_path():
    base = os.environ.get("LOCALAPPDATA")
    if os.name != "nt" or not base:
        raise RuntimeError("本机加密凭据需要Windows当前用户环境")
    identity = hashlib.sha256(str(Path(__file__).resolve().parent).casefold().encode()).hexdigest()[:16]
    return Path(base) / "NovelTool" / identity / "deepseek.dpapi"


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data, protect):
    if os.name != "nt":
        raise RuntimeError("DPAPI仅适用于Windows")
    buffer = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = _Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise RuntimeError("Windows凭据加解密失败")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel32.LocalFree(target.data)


def save_key(key, *, path=None):
    key = str(key).strip()
    if not key or key == "YOUR_DEEPSEEK_API_KEY_HERE":
        raise ValueError("密钥为空或占位值")
    target = Path(path) if path is not None else key_path()
    encrypted = _crypt(key.encode("utf-8"), True)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".credential-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encrypted)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    if load_key(path=target) != key:
        raise RuntimeError("凭据保存读回不一致")


def load_key(*, path=None):
    target = Path(path) if path is not None else key_path()
    if not target.exists():
        return ""
    try:
        return _crypt(target.read_bytes(), False).decode("utf-8")
    except Exception:
        raise RuntimeError("本机加密凭据不可读，请重新配置；不会回退到其他账户") from None


def clear_key():
    key_path().unlink(missing_ok=True)


if __name__ == "__main__":
    import getpass
    import sys
    import warnings
    if not sys.stdin.isatty():
        raise SystemExit("必须在不回显终端输入凭据")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        secret = getpass.getpass("DeepSeek key (hidden; Windows DPAPI): ")
    save_key(secret)
    secret = ""
    print("CREDENTIAL_SAVED_AND_READBACK_OK; current-user encrypted; no plaintext config")
