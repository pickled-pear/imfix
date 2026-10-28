"""Reading dates from filenames and from exiftool values."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

# A date such as 20230514, 2023-05-14, 2023_05_14 or 2023.05.14 that isn't the tail of a longer number.
# The second separator must match the first, so "2023-0514" is not treated as a date.
_DATE = re.compile(r"(?<!\d)((?:19|20)\d{2})([-_.]?)(\d{2})\2(\d{2})")

# What may sit between the date and the time: "_", "-", ".", "T", a space, or macOS's " at ".
_GAP = r"(?:\s+at\s+|[ _T.-])?"

# A time written with separators: 14-23-01, 14.23.01, 14_23_01, 14:23:01.
_SEPARATED_TIME = re.compile(_GAP + r"(\d{2})[-_.:](\d{2})[-_.:](\d{2})(?!\d)")

# A run of digits after the date. This is either a compact time (142301, or 142301123 with
# milliseconds as Pixel phones write it) or a random id that only looks like a number.
_DIGIT_RUN = re.compile(_GAP + r"(\d+)")

# Compact times are HHMMSS plus up to three digits of milliseconds. Anything longer is an id.
_COMPACT_TIME_LENGTHS = range(6, 10)

# exiftool dates look like "2023:05:14 14:23:01", optionally with sub-seconds and a time zone.
# IPTC dates may be just "2023:05:14".
_EXIF_DATE = re.compile(r"^(\d{4})[:-](\d{2})[:-](\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?")


def _make(year, month, day, hour=0, minute=0, second=0) -> datetime | None:
    try:
        return datetime(int(year), int(month), int(day), int(hour), int(minute), int(second))
    except ValueError:
        return None


def _time_after(rest: str) -> tuple[int, int, int] | None:
    """Return the time at the start of `rest` (the text after a date), if there is one."""
    m = _SEPARATED_TIME.match(rest)
    if m:
        return int(m[1]), int(m[2]), int(m[3])

    m = _DIGIT_RUN.match(rest)
    if m and len(m[1]) in _COMPACT_TIME_LENGTHS:
        digits = m[1]
        return int(digits[0:2]), int(digits[2:4]), int(digits[4:6])

    return None


def date_from_filename(name: str) -> datetime | None:
    """Pull a date (and time, where there is one) out of a filename.

    Handles names such as:
        IMG_20230514_142301.jpg          -> 2023-05-14 14:23:01
        PXL_20230514_142301123.jpg       -> 2023-05-14 14:23:01
        ABC_20250101_0935790135.jpg      -> 2025-01-01 00:00:00  (random id, not a time)
        2023-05-14 14.23.01.png          -> 2023-05-14 14:23:01
        Screenshot_2023-05-14-14-23-01   -> 2023-05-14 14:23:01
        IMG-20230514-WA0001.jpg          -> 2023-05-14 00:00:00
    """
    stem = Path(name).stem

    for m in _DATE.finditer(stem):
        date = _make(m[1], m[3], m[4])
        if date is None:
            continue

        time = _time_after(stem[m.end():])
        if time is not None:
            with_time = _make(date.year, date.month, date.day, *time)
            # A time that doesn't exist (e.g. 09:35:79) means the digits were an id, not a time.
            if with_time is not None:
                return with_time

        return date

    return None


def parse_exif_date(value) -> datetime | None:
    """Parse a date value as exiftool reports it. Returns None for blank or zeroed dates."""
    if not isinstance(value, str):
        return None
    m = _EXIF_DATE.match(value.strip())
    if not m:
        return None
    return _make(m[1], m[2], m[3], m[4] or 0, m[5] or 0, m[6] or 0)


def to_exif(date: datetime) -> str:
    return date.strftime("%Y:%m:%d %H:%M:%S")
