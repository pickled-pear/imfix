"""Finding input files, working out where results go, and keeping file dates."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Images exiftool can write metadata into.
METADATA_EXTS = {
    ".jpg", ".jpeg", ".jpe", ".png", ".tif", ".tiff", ".webp", ".heic", ".heif", ".avif", ".gif",
    ".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".raf",
}

# What `compress` handles.
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".aiff", ".aif"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".wmv", ".webm", ".flv", ".3gp"}

# Everything copied into the working copy.
MEDIA_EXTS = METADATA_EXTS | IMAGE_EXTS | AUDIO_EXTS | VIDEO_EXTS

# Files imfix is part-way through writing. They are never treated as input.
TEMP_PREFIX = ".imfix-"


def collect(directory: Path, recurse: bool, exts: set[str], exclude: Path | None) -> list[Path]:
    """List the matching files in `directory`.

    Folders whose names start with "." (such as .thumbnails or .git) are always skipped, as is
    the `exclude` folder. `directory` itself is used whatever its name, so "." works.
    """
    found = []
    walker = os.walk(directory) if recurse else [(str(directory), [], os.listdir(directory))]
    for root, dirs, names in walker:
        root_path = Path(root)
        dirs[:] = [d for d in dirs if not d.startswith(".")
                   and not (exclude is not None and _same(root_path / d, exclude))]
        for name in names:
            path = root_path / name
            if path.suffix.lower() in exts and not name.startswith(TEMP_PREFIX) and path.is_file():
                found.append(path)
    return sorted(found, key=lambda p: str(p).casefold())


def _same(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


@dataclass
class FileTimes:
    """A file's dates, so they can be put back after the file is copied or rewritten."""

    accessed: float
    modified: float
    created: float | None

    @classmethod
    def of(cls, path: Path) -> "FileTimes":
        st = path.stat()
        return cls(st.st_atime, st.st_mtime, creation_time(st))

    def apply(self, path: Path) -> None:
        os.utime(path, (self.accessed, self.modified))
        if sys.platform == "win32" and self.created is not None:
            _set_windows_creation_time(path, self.created)


def creation_time(st: os.stat_result) -> float | None:
    """The file's creation time, or None where the system doesn't record it (most of Linux)."""
    birth = getattr(st, "st_birthtime", None)
    if birth is not None:
        return birth
    if sys.platform == "win32":
        return st.st_ctime  # On Windows before Python 3.12, st_ctime is the creation time.
    return None


def best_file_date(path: Path) -> datetime:
    """The creation date if the system records one, otherwise the modified date."""
    st = path.stat()
    created = creation_time(st)
    return datetime.fromtimestamp(created if created is not None else st.st_mtime)


def _set_windows_creation_time(path: Path, timestamp: float) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.SetFileTime.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.SetFileTime.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    FILE_WRITE_ATTRIBUTES = 0x100
    SHARE_ALL = 0x1 | 0x2 | 0x4
    OPEN_EXISTING = 3
    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

    handle = kernel32.CreateFileW(str(path), FILE_WRITE_ATTRIBUTES, SHARE_ALL, None, OPEN_EXISTING, 0, None)
    if handle == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        # FILETIME counts 100-nanosecond steps since 1601-01-01.
        ticks = int((timestamp + 11644473600) * 10_000_000)
        filetime = wintypes.FILETIME(ticks & 0xFFFFFFFF, ticks >> 32)
        if not kernel32.SetFileTime(handle, ctypes.byref(filetime), None, None):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(handle)


def copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    times = FileTimes.of(src)
    shutil.copy2(src, dst)
    times.apply(dst)


def unique_stem(stem: str, taken: set[str], suffix: str = "") -> str:
    """Return `stem`, or `stem_1`, `stem_2`... if `stem + suffix` is already in `taken`.

    `taken` holds case-folded names (Windows names aren't case-sensitive). The result is added to it.
    """
    candidate, counter = stem, 0
    while (candidate + suffix).casefold() in taken:
        counter += 1
        candidate = f"{stem}_{counter}"
    taken.add((candidate + suffix).casefold())
    return candidate
