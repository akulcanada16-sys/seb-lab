"""Read existing files without blocking atomic replacement by their owner."""
from contextlib import contextmanager
import os
from pathlib import Path


def read_shared_text(path: Path, *, encoding: str = "utf-8") -> str:
    """Decode one newly opened complete file without blocking its replacement."""
    with open_shared_read(path) as stream:
        return stream.read().decode(encoding)


@contextmanager
def open_shared_read(path: Path):
    """Yield a binary read-only stream; Windows writers may replace its old file."""
    if os.name != "nt":
        with path.open("rb") as stream:
            yield stream
        return

    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    # GENERIC_READ; FILE_SHARE_READ | WRITE | DELETE; OPEN_EXISTING.
    handle = create(str(path), 0x80000000, 7, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close(handle)
        raise
    try:
        stream = os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise
    with stream:
        yield stream
