"""End-to-end tests that run the real exiftool and ffmpeg."""

import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from imfix.cli import cli

pytestmark = pytest.mark.skipif(
    not (shutil.which("exiftool") and shutil.which("ffmpeg")), reason="needs exiftool and ffmpeg"
)


def make_image(path: Path, size: str = "320x240") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}", "-frames:v", "1", str(path)],
        check=True,
    )
    return path


def read(path: Path, *tags: str) -> dict:
    args = ["exiftool", "-json", "-charset", "filename=utf8", *(f"-{t}" for t in tags), str(path)]
    return json.loads(subprocess.run(args, capture_output=True, check=True).stdout)[0]


def write(path: Path, *args: str) -> None:
    subprocess.run(["exiftool", "-charset", "filename=utf8", "-overwrite_original", *args, str(path)],
                   capture_output=True, check=True)


def imfix(*args: str):
    result = CliRunner().invoke(cli, list(map(str, args)), prog_name="imfix", catch_exceptions=False)
    print(result.output)
    return result


@pytest.fixture
def photos(tmp_path):
    folder = tmp_path / "Fotos été"
    make_image(folder / "Zoë's café.jpg")
    make_image(folder / "IMG_20230514_142301.jpg")
    make_image(folder / "ABC_20250101_0935790135.png")
    make_image(folder / "sub" / "PXL_20240202_101010123.jpg")
    return folder


def test_no_arguments_shows_help():
    result = imfix()
    assert result.exit_code == 0
    assert "Usage: imfix" in result.output


def test_subcommand_without_arguments_shows_help():
    assert "Usage: imfix tag" in imfix("tag").output


def test_tag_writes_copies_and_leaves_originals(photos):
    result = imfix("tag", photos, "holiday", "Côte d'Azur")
    assert result.exit_code == 0

    out = photos / "imfix"
    assert sorted(p.name for p in out.iterdir()) == sorted(p.name for p in photos.iterdir() if p.is_file())

    tags = read(out / "Zoë's café.jpg", "Subject", "Keywords")
    assert tags["Subject"] == ["holiday", "Côte d'Azur"]
    assert tags["Keywords"] == ["holiday", "Côte d'Azur"]
    assert "Subject" not in read(photos / "Zoë's café.jpg", "Subject")
    assert not (out / "sub").exists()  # no --recurse


def test_tag_twice_in_place_does_not_duplicate(photos):
    imfix("tag", photos, "a", "--in-place")
    imfix("tag", photos, "a", "b", "--in-place")
    assert read(photos / "Zoë's café.jpg", "Subject")["Subject"] == ["a", "b"]
    assert not (photos / "imfix").exists()


def test_recurse_keeps_layout_and_skips_output_folder(photos):
    imfix("tag", photos, "x", "-r")
    assert (photos / "imfix" / "sub" / "PXL_20240202_101010123.jpg").exists()
    result = imfix("tag", photos, "y", "-r")  # second run must not descend into imfix/
    assert not (photos / "imfix" / "imfix").exists()
    assert "already in the output folder" in result.output


def test_dry_run_changes_nothing(photos):
    before = {p: p.stat().st_mtime for p in photos.rglob("*")}
    for command in (["tag", "x"], ["date"], ["strip-copyright"], ["author", "Me"], ["rename"], ["compress"]):
        result = imfix(command[0], photos, *command[1:], "--dry-run", "-r")
        assert result.exit_code == 0
        assert "Dry run" in result.output
    assert {p: p.stat().st_mtime for p in photos.rglob("*")} == before


def test_date_only_fills_missing_dates(photos):
    write(photos / "IMG_20230514_142301.jpg", "-DateTimeOriginal=2001:01:01 01:01:01")
    imfix("date", photos, "-r", "-v")
    out = photos / "imfix"
    assert read(out / "IMG_20230514_142301.jpg", "DateTimeOriginal")["DateTimeOriginal"] == "2001:01:01 01:01:01"
    assert read(out / "ABC_20250101_0935790135.png", "DateTimeOriginal")["DateTimeOriginal"] == "2025:01:01 00:00:00"
    assert read(out / "sub" / "PXL_20240202_101010123.jpg", "DateTimeOriginal")["DateTimeOriginal"] == "2024:02:02 10:10:10"
    assert "DateTimeOriginal" not in read(out / "Zoë's café.jpg", "DateTimeOriginal")  # copied, unchanged


def test_strip_copyright(photos):
    f = photos / "Zoë's café.jpg"
    write(f, "-Copyright=© Zoë", "-XMP-dc:Rights=All rights", "-IPTC:CopyrightNotice=Zoë", "-Artist=Keep me")
    imfix("strip-copyright", photos, "--in-place")
    tags = read(f, "Copyright", "Rights", "CopyrightNotice", "Artist")
    assert set(tags) == {"SourceFile", "Artist"}


def test_author_set_and_clear(photos):
    f = photos / "Zoë's café.jpg"
    imfix("author", photos, "Zoë Ångström", "-i")
    tags = read(f, "Artist", "Creator", "By-line", "XPAuthor")
    assert tags["Artist"] == tags["Creator"] == tags["By-line"] == tags["XPAuthor"] == "Zoë Ångström"

    imfix("author", photos, "--none", "-i")
    assert set(read(f, "Artist", "Creator", "By-line", "XPAuthor")) == {"SourceFile"}


def test_author_needs_name_or_none(photos):
    assert imfix("author", photos).exit_code == 2


def test_rename_uses_date_taken_then_file_date_with_counters(photos):
    for name in ("Zoë's café.jpg", "IMG_20230514_142301.jpg", "ABC_20250101_0935790135.png"):
        write(photos / name, "-DateTimeOriginal=2022:03:04 05:06:07")
    no_date = make_image(photos / "nodate.jpg")
    os.utime(no_date, (1_600_000_000, 1_600_000_000))

    imfix("rename", photos, "--in-place")
    names = sorted(p.name for p in photos.iterdir() if p.is_file())

    # Three images share a time: the first in name order keeps the plain name, the others count up.
    assert {"2022-03-04_05-06-07.png", "2022-03-04_05-06-07_1.jpg", "2022-03-04_05-06-07_2.jpg"} <= set(names)
    if not hasattr(os.stat(photos), "st_birthtime"):  # no creation date here, so the modified date is used
        assert datetime.fromtimestamp(1_600_000_000).strftime("%Y-%m-%d_%H-%M-%S.jpg") in names

    # Running again changes nothing.
    result = imfix("rename", photos, "--in-place")
    assert sorted(p.name for p in photos.iterdir() if p.is_file()) == names
    assert "4 already named" in result.output


def test_compress_keeps_metadata_and_dates(photos):
    src = photos / "Zoë's café.jpg"
    make_image(src, "1920x1080")
    write(src, "-DateTimeOriginal=2022:03:04 05:06:07", "-XMP-dc:Subject=kept")
    os.utime(src, (1_600_000_000, 1_600_000_000))

    result = imfix("compress", photos, "--format", "webp")
    assert result.exit_code == 0
    out = photos / "imfix" / "Zoë's café.webp"
    tags = read(out, "DateTimeOriginal", "Subject")
    assert tags["DateTimeOriginal"] == "2022:03:04 05:06:07"
    assert tags["Subject"] == "kept"
    assert out.stat().st_mtime == 1_600_000_000

    stripped = photos / "stripped"
    imfix("compress", photos, "-o", stripped, "--strip-metadata")
    assert "DateTimeOriginal" not in read(stripped / "Zoë's café.jpg", "DateTimeOriginal")


def test_compress_name_clash_gets_counter(tmp_path):
    make_image(tmp_path / "a.jpg")
    make_image(tmp_path / "a.png")
    imfix("compress", tmp_path)
    assert sorted(p.name for p in (tmp_path / "imfix").iterdir()) == ["a.jpg", "a_1.jpg"]


def test_bad_file_is_reported_and_not_left_in_output(tmp_path):
    make_image(tmp_path / "good.jpg")
    (tmp_path / "bad.jpg").write_text("not an image")
    result = imfix("tag", tmp_path, "x")
    assert "Failed: bad.jpg" in result.output
    assert sorted(p.name for p in (tmp_path / "imfix").iterdir()) == ["good.jpg"]
    assert "1 tagged" in result.output and "1 failed" in result.output
