"""Token storage. On Windows tokens are encrypted with DPAPI (bound to the
signed-in Windows user); elsewhere they are written with 0600 permissions."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _UI_FORBIDDEN = 0x01

    def _call(fn, data: bytes, *extra) -> bytes:
        buf = ctypes.create_string_buffer(data, len(data))
        inp = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        out = _Blob()
        if not fn(ctypes.byref(inp), *extra, None, None, None, _UI_FORBIDDEN, ctypes.byref(out)):
            raise ctypes.WinError()
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            _kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))

    def protect(data: bytes) -> bytes:
        return _call(_crypt32.CryptProtectData, data, ctypes.c_wchar_p("drivedex"))

    def unprotect(data: bytes) -> bytes:
        return _call(_crypt32.CryptUnprotectData, data, None)

    ENCRYPTED = True
else:  # pragma: no cover - exercised on non-Windows only
    def protect(data: bytes) -> bytes:
        return data

    def unprotect(data: bytes) -> bytes:
        return data

    ENCRYPTED = False


class TokenStore:
    def __init__(self, home: Path):
        self.dir = Path(home) / "tokens"

    def _path(self, account: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in account)
        return self.dir / f"{safe}.bin"

    def load(self, account: str) -> dict | None:
        p = self._path(account)
        if not p.exists():
            return None
        return json.loads(unprotect(p.read_bytes()))

    def save(self, account: str, token: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self._path(account)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(protect(json.dumps(token).encode()))
        if not ENCRYPTED:
            os.chmod(tmp, 0o600)
        os.replace(tmp, p)

    def delete(self, account: str) -> bool:
        p = self._path(account)
        if p.exists():
            p.unlink()
            return True
        return False
