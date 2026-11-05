"""Local OAuth credential storage; Windows uses current-user DPAPI encryption."""

import base64
import ctypes
import json
import os
import re
import tempfile
from pathlib import Path


class CredentialStore:
    def __init__(self, root: Path):
        self.root = root

    def _path(self, id_: str) -> Path:
        if not re.fullmatch(r"ag-[a-f0-9]{24}", id_):
            raise ValueError("Invalid credential reference")
        return self.root / (id_ + ".json")

    @staticmethod
    def _dpapi(data: bytes, *, decrypt=False) -> bytes:
        from ctypes import wintypes
        class Blob(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
        buffer = ctypes.create_string_buffer(data)
        source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        result = Blob()
        crypto = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        operation = crypto.CryptUnprotectData if decrypt else crypto.CryptProtectData
        operation.restype = wintypes.BOOL
        # CRYPTPROTECT_UI_FORBIDDEN: never show an OS credential dialog.
        if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
            raise OSError("Windows credential protection failed")
        try:
            return ctypes.string_at(result.data, result.size)
        finally:
            kernel.LocalFree(ctypes.cast(result.data, ctypes.c_void_p))

    def save(self, id_: str, value: dict) -> None:
        path = self._path(id_)
        data = json.dumps(value, ensure_ascii=True, allow_nan=False).encode()
        protection = "windows-dpapi-user" if os.name == "nt" else "posix-private-file"
        if os.name == "nt":
            data = self._dpapi(data)
        envelope = json.dumps({"version": 1, "protection": protection,
                               "payload": base64.b64encode(data).decode()}).encode()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise OSError("Credential directory must not be a symbolic link")
        if os.name != "nt":
            self.root.chmod(0o700)
        name = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.root, prefix=".credential-", delete=False) as file:
                name = file.name
                if os.name != "nt":
                    os.chmod(name, 0o600)
                file.write(envelope)
                file.flush()
                os.fsync(file.fileno())
            os.replace(name, path)
            name = None
        finally:
            if name is not None:
                Path(name).unlink(missing_ok=True)

    def load(self, id_: str) -> dict:
        data = self._path(id_).read_bytes()
        if len(data) > 8 * 1024 * 1024:
            raise OSError("Credential record too large")
        envelope = json.loads(data)
        payload = base64.b64decode(envelope["payload"], validate=True)
        if envelope["protection"] == "windows-dpapi-user":
            if os.name != "nt":
                raise OSError("Windows credentials require the original Windows user")
            payload = self._dpapi(payload, decrypt=True)
        elif envelope["protection"] != "posix-private-file" or os.name == "nt":
            raise OSError("Unexpected credential protection")
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise OSError("Invalid credential record")
        return value

    def delete(self, id_: str) -> None:
        self._path(id_).unlink(missing_ok=True)
