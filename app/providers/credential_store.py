from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Any

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2

class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

class CREDENTIALW(ctypes.Structure):
    _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR), ("LastWritten", FILETIME), ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]

class CredentialStore:
    def __init__(self, namespace: str = "property_ai"):
        self.namespace = namespace
        self._session: dict[str, str] = {}
        self._advapi32 = None
        if os.name == "nt":
            try:
                api = ctypes.WinDLL("Advapi32.dll")
                api.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
                api.CredWriteW.restype = wintypes.BOOL
                api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
                api.CredReadW.restype = wintypes.BOOL
                api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
                api.CredDeleteW.restype = wintypes.BOOL
                api.CredFree.argtypes = [ctypes.c_void_p]
                self._advapi32 = api
            except OSError:
                pass

    def credential_ref(self, provider_name: str) -> str:
        return f"{self.namespace}/{provider_name}"

    def capability(self) -> dict[str, Any]:
        return {"secure_store": "windows_credential_manager" if self._advapi32 else "session_only", "persistent_available": bool(self._advapi32), "session_fallback": True}

    def _read_windows(self, target: str) -> str:
        if not self._advapi32:
            return ""
        pointer = ctypes.POINTER(CREDENTIALW)()
        if not self._advapi32.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            return ""
        try:
            cred = pointer.contents
            raw = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize) if cred.CredentialBlob and cred.CredentialBlobSize else b""
            return raw.decode("utf-16-le") if raw else ""
        finally:
            self._advapi32.CredFree(pointer)

    def get(self, provider_name: str, env_name: str = "") -> tuple[str, str]:
        target = self.credential_ref(provider_name)
        value = self._read_windows(target)
        if value:
            return value, "windows_credential_manager"
        if env_name and os.environ.get(env_name, "").strip():
            return os.environ[env_name].strip(), "environment"
        if self._session.get(target):
            return self._session[target], "session_only"
        return "", "none"

    def set(self, provider_name: str, secret: str) -> dict[str, Any]:
        value = str(secret or "").strip()
        if not value:
            raise ValueError("API Key不能为空。")
        target = self.credential_ref(provider_name)
        if self._advapi32:
            raw = value.encode("utf-16-le")
            blob = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
            cred = CREDENTIALW(0, CRED_TYPE_GENERIC, target, "AI物业方案智能体Provider凭证", FILETIME(), len(raw), ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte)), CRED_PERSIST_LOCAL_MACHINE, 0, None, None, provider_name)
            if self._advapi32.CredWriteW(ctypes.byref(cred), 0):
                self._session.pop(target, None)
                return {"credential_ref": target, "storage": "windows_credential_manager", "persistent": True}
        self._session[target] = value
        return {"credential_ref": target, "storage": "session_only", "persistent": False}

    def delete(self, provider_name: str) -> dict[str, Any]:
        target = self.credential_ref(provider_name)
        self._session.pop(target, None)
        deleted = bool(self._advapi32 and self._advapi32.CredDeleteW(target, CRED_TYPE_GENERIC, 0))
        return {"credential_ref": target, "deleted": deleted, "session_deleted": True}
