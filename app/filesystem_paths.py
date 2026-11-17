"""Windows path adapters; physical data stays at the caller's chosen location."""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path


def filesystem_path(path: str | os.PathLike) -> Path:
    """Use the extended Win32 form only when a native path approaches MAX_PATH."""
    value = os.fspath(path)
    if os.name != "nt" or value.startswith("\\\\?\\"):
        return Path(value)
    absolute = os.path.abspath(value)
    if len(absolute.encode("utf-16-le")) // 2 < 240:
        return Path(value)
    if absolute.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + absolute[2:])
    return Path("\\\\?\\" + absolute)


@contextmanager
def native_render_root(work: Path):
    """Lease a short drive alias for a deep local renderer directory, then remove it.

    The alias contains no data: it points to the same physical temporary directory.
    Allocation is serialized across MMF processes. Existing drives are skipped and
    cleanup matches the exact target, so another mapping cannot be removed.
    """
    if os.name != "nt" or len(str(work)) < 90:
        yield work
        return
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    define = kernel.DefineDosDeviceW
    define.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR]
    define.restype = wintypes.BOOL
    query = kernel.QueryDosDeviceW
    query.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    query.restype = wintypes.DWORD
    mutex_create = kernel.CreateMutexW
    mutex_create.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    mutex_create.restype = wintypes.HANDLE
    wait = kernel.WaitForSingleObject
    wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    wait.restype = wintypes.DWORD
    release = kernel.ReleaseMutex
    release.argtypes = [wintypes.HANDLE]
    release.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    logical = kernel.GetLogicalDrives
    logical.restype = wintypes.DWORD

    @contextmanager
    def allocation_lock():
        handle = mutex_create(None, False, "Local\\MMF_Native_Render_Path_v1")
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        acquired = False
        try:
            state = wait(handle, 10000)
            acquired = state in (0, 0x80)  # acquired / abandoned owner
            if not acquired:
                raise OSError("Native renderer alias allocation lock unavailable")
            yield
        finally:
            if acquired:
                release(handle)
            close(handle)

    absolute = str(work.resolve())
    if absolute.startswith("\\\\?\\UNC\\"):
        raise OSError("Short native renderer aliases require a local temporary directory")
    if absolute.startswith("\\\\?\\"):
        absolute = absolute[4:]
    if absolute.startswith("\\\\"):
        raise OSError("Short native renderer aliases require a local temporary directory")
    target = "\\??\\" + absolute
    device = None
    with allocation_lock():
        drives = logical()
        for letter in "ZYXWVUTSRQPONMLKJIHGFE":
            if drives & (1 << (ord(letter) - ord("A"))):
                continue
            buffer = ctypes.create_unicode_buffer(32768)
            ctypes.set_last_error(0)
            if query(letter + ":", buffer, len(buffer)):
                continue
            if ctypes.get_last_error() != 2:
                continue
            if define(0x9, letter + ":", target):  # raw target; no shell broadcast
                device = letter + ":"
                break
    if device is None:
        raise OSError("No unused drive alias available for the native renderer")
    try:
        alias = Path(device + "\\")
        resolved_alias = str(alias.resolve())
        if resolved_alias.startswith("\\\\?\\"):
            resolved_alias = resolved_alias[4:]
        if resolved_alias.casefold() != absolute.casefold():
            raise OSError("Native renderer alias does not resolve to its assigned directory")
        yield alias
    finally:
        # Exact removal is deliberate: never delete by drive letter alone.
        if not define(0xF, device, target):
            raise ctypes.WinError(ctypes.get_last_error())
