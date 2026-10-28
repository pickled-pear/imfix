from datetime import datetime

import pytest

from imfix.dates import date_from_filename, parse_exif_date


@pytest.mark.parametrize(
    "name, expected",
    [
        ("IMG_20230514_142301.jpg", datetime(2023, 5, 14, 14, 23, 1)),
        ("PXL_20230514_142301123.jpg", datetime(2023, 5, 14, 14, 23, 1)),
        ("ABC_20250101_0935790135.jpg", datetime(2025, 1, 1)),
        ("ABC_20250101_1423011234.jpg", datetime(2025, 1, 1)),  # 10 digits: an id, even if it starts like a time
        ("XYZ_20250101_093579.jpg", datetime(2025, 1, 1)),  # 79 seconds isn't a time
        ("VID_20250101_235959.jpg", datetime(2025, 1, 1, 23, 59, 59)),
        ("20230514_142301.jpg", datetime(2023, 5, 14, 14, 23, 1)),
        ("20230514142301.jpg", datetime(2023, 5, 14, 14, 23, 1)),
        ("2023-05-14 14.23.01.png", datetime(2023, 5, 14, 14, 23, 1)),
        ("2023-05-14_14-23-01.png", datetime(2023, 5, 14, 14, 23, 1)),
        ("Screenshot_2023-05-14-14-23-01.png", datetime(2023, 5, 14, 14, 23, 1)),
        ("Screenshot 2023-05-14 at 14.23.01.png", datetime(2023, 5, 14, 14, 23, 1)),
        ("IMG-20230514-WA0001.jpg", datetime(2023, 5, 14)),
        ("holiday 2023.05.14.jpg", datetime(2023, 5, 14)),
        ("Zoë_20230514_142301.jpg", datetime(2023, 5, 14, 14, 23, 1)),
        ("IMG_20231399_142301.jpg", None),  # not a real date
        ("IMG_1234.jpg", None),
        ("photo.jpg", None),
        ("123420230514.jpg", None),  # date glued to the end of a longer number
    ],
)
def test_date_from_filename(name, expected):
    assert date_from_filename(name) == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2023:05:14 14:23:01", datetime(2023, 5, 14, 14, 23, 1)),
        ("2023:05:14 14:23:01.123+01:00", datetime(2023, 5, 14, 14, 23, 1)),
        ("2023:05:14", datetime(2023, 5, 14)),
        ("0000:00:00 00:00:00", None),
        ("", None),
        (None, None),
        (12345, None),
    ],
)
def test_parse_exif_date(value, expected):
    assert parse_exif_date(value) == expected
