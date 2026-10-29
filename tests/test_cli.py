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


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *args], check=True)


def make_image(path: Path, size: str = "320x240", *extra: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # PNGs get photo-like noise: a flat test pattern is genuinely smaller as PNG than as JPEG.
    source = f"testsrc=size={size}"
    if path.suffix == ".png":
        source = f"nullsrc=size={size},geq=lum='random(1)*255':cb=128:cr=128"
    ffmpeg("-f", "lavfi", "-i", source, "-frames:v", "1", *extra, str(path))
    return path


def make_audio(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-metadata", "title=Zoë's song", str(path))
    return path


def make_video(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=2", "-f", "lavfi", "-i", "sine=duration=2",
           "-c:v", "mpeg4", "-q:v", "2", "-c:a", "aac", "-shortest",
           "-metadata", "creation_time=2021-06-07T08:09:10Z", str(path))
    return path


def read(path: Path, *tags: str) -> dict:
    args = ["exiftool", "-json", "-charset", "filename=utf8", *(f"-{t}" for t in tags), str(path)]
    return json.loads(subprocess.run(args, capture_output=True, check=True).stdout)[0]


def write(path: Path, *args: str) -> None:
    subprocess.run(["exiftool", "-charset", "filename=utf8", "-overwrite_original", *args, str(path)],
                   capture_output=True, check=True)


def names(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir() if p.is_file() and p.name != ".imfix.json")


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


# --- help -----------------------------------------------------------------------------------

def test_no_arguments_shows_help():
    result = imfix()
    assert result.exit_code == 0
    assert "Usage: imfix" in result.output


def test_subcommand_without_arguments_shows_help():
    assert "Usage: imfix tag" in imfix("tag").output


# --- the working copy -----------------------------------------------------------------------

def test_first_command_copies_all_media_and_leaves_originals(photos):
    make_audio(photos / "song.wav")
    (photos / "notes.txt").write_text("not media")

    result = imfix("tag", photos, "holiday", "Côte d'Azur")
    assert result.exit_code == 0

    out = photos / "imfix"
    assert names(out) == sorted(["Zoë's café.jpg", "IMG_20230514_142301.jpg", "ABC_20250101_0935790135.png", "song.wav"])
    assert (out / ".imfix.json").exists()
    assert not (out / "sub").exists()  # no --recurse

    tags = read(out / "Zoë's café.jpg", "Subject", "Keywords")
    assert tags["Subject"] == ["holiday", "Côte d'Azur"]
    assert tags["Keywords"] == ["holiday", "Côte d'Azur"]
    assert "Subject" not in read(photos / "Zoë's café.jpg", "Subject")


def test_commands_chain_on_the_working_copy(photos):
    """The main use case: rename, then compress, then tag, each building on the last."""
    for name in ("Zoë's café.jpg", "IMG_20230514_142301.jpg", "ABC_20250101_0935790135.png"):
        write(photos / name, "-DateTimeOriginal=2022:03:04 05:06:07")
    originals = {p: p.read_bytes() for p in photos.iterdir() if p.is_file()}

    imfix("rename", photos, "--prefix", "trip")
    result = imfix("compress", photos)
    assert "Working on the copies" in result.output
    assert "3 compressed" in result.output
    imfix("tag", photos, "done")

    out = photos / "imfix"
    assert names(out) == ["trip_2022-03-04_05-06-07.jpg", "trip_2022-03-04_05-06-07_1.jpg", "trip_2022-03-04_05-06-07_2.jpg"]
    for name in names(out):
        tags = read(out / name, "Subject", "DateTimeOriginal")
        assert tags["Subject"] == "done"
        assert tags["DateTimeOriginal"] == "2022:03:04 05:06:07"
    assert {p: p.read_bytes() for p in photos.iterdir() if p.is_file()} == originals


def test_compress_twice_skips_already_compressed(photos):
    imfix("compress", photos)
    imfix("rename", photos)  # compressed state must follow the files to their new names
    result = imfix("compress", photos)
    assert "0 compressed" in result.output and "3 already compressed" in result.output
    result = imfix("compress", photos, "--recompress")
    assert "already compressed" not in result.output


def test_tag_twice_does_not_duplicate(photos):
    imfix("tag", photos, "a")
    imfix("tag", photos, "a", "b")
    assert read(photos / "imfix" / "Zoë's café.jpg", "Subject")["Subject"] == ["a", "b"]


def test_in_place_changes_originals_without_working_copy(photos):
    imfix("tag", photos, "a", "--in-place")
    assert read(photos / "Zoë's café.jpg", "Subject")["Subject"] == "a"
    assert not (photos / "imfix").exists()


def test_recurse_keeps_layout_and_skips_working_copy(photos):
    imfix("tag", photos, "x", "-r")
    assert (photos / "imfix" / "sub" / "PXL_20240202_101010123.jpg").exists()
    imfix("tag", photos, "y", "-r")
    assert not (photos / "imfix" / "imfix").exists()
    assert read(photos / "imfix" / "sub" / "PXL_20240202_101010123.jpg", "Subject")["Subject"] == ["x", "y"]


def test_fresh_starts_again_from_originals(photos):
    imfix("rename", photos)
    result = imfix("tag", photos, "x", "--fresh")
    assert "Deleted the old working copy" in result.output
    assert names(photos / "imfix") == names(photos)


def test_warns_about_new_originals(photos):
    imfix("tag", photos, "x")
    make_image(photos / "new.jpg")
    result = imfix("tag", photos, "y")
    assert "1 more file(s)" in result.output
    assert not (photos / "imfix" / "new.jpg").exists()


def test_refuses_to_use_a_foreign_output_folder(photos, tmp_path):
    foreign = tmp_path / "mine"
    make_image(foreign / "precious.jpg")
    for extra in ([], ["--fresh"]):
        result = CliRunner().invoke(cli, ["tag", str(photos), "x", "-o", str(foreign), *extra])
        assert result.exit_code != 0
        assert "didn't put there" in result.output
    assert names(foreign) == ["precious.jpg"]
    assert "Subject" not in read(foreign / "precious.jpg", "Subject")


def test_dry_run_changes_nothing(photos):
    before = {p: p.stat().st_mtime for p in photos.rglob("*")}
    for command in (["tag", "x"], ["date"], ["copyright", "--none"], ["author", "Me"], ["rename"], ["compress"]):
        result = imfix(command[0], photos, *command[1:], "--dry-run", "-r")
        assert result.exit_code == 0
        assert "Dry run" in result.output
    assert {p: p.stat().st_mtime for p in photos.rglob("*")} == before
    assert not (photos / "imfix").exists()


# --- metadata commands ----------------------------------------------------------------------

def test_date_only_fills_missing_dates(photos):
    write(photos / "IMG_20230514_142301.jpg", "-DateTimeOriginal=2001:01:01 01:01:01")
    imfix("date", photos, "-r", "-v")
    out = photos / "imfix"
    assert read(out / "IMG_20230514_142301.jpg", "DateTimeOriginal")["DateTimeOriginal"] == "2001:01:01 01:01:01"
    assert read(out / "ABC_20250101_0935790135.png", "DateTimeOriginal")["DateTimeOriginal"] == "2025:01:01 00:00:00"
    assert read(out / "sub" / "PXL_20240202_101010123.jpg", "DateTimeOriginal")["DateTimeOriginal"] == "2024:02:02 10:10:10"
    assert "DateTimeOriginal" not in read(out / "Zoë's café.jpg", "DateTimeOriginal")


def test_copyright_remove_and_set(photos):
    f = photos / "Zoë's café.jpg"
    write(f, "-Copyright=© Zoë", "-XMP-dc:Rights=All rights", "-IPTC:CopyrightNotice=Zoë", "-Artist=Keep me")
    imfix("copyright", photos, "--none", "-i")
    assert set(read(f, "Copyright", "Rights", "CopyrightNotice", "Artist")) == {"SourceFile", "Artist"}

    imfix("copyright", photos, "© 2025 Zoë", "-i")
    tags = read(f, "Copyright", "Rights", "CopyrightNotice")
    assert tags["Copyright"] == tags["Rights"] == tags["CopyrightNotice"] == "© 2025 Zoë"


def test_author_set_and_clear(photos):
    f = photos / "Zoë's café.jpg"
    imfix("author", photos, "Zoë Ångström", "-i")
    tags = read(f, "Artist", "Creator", "By-line", "XPAuthor")
    assert tags["Artist"] == tags["Creator"] == tags["By-line"] == tags["XPAuthor"] == "Zoë Ångström"

    imfix("author", photos, "--none", "-i")
    assert set(read(f, "Artist", "Creator", "By-line", "XPAuthor")) == {"SourceFile"}


@pytest.mark.parametrize("command", ["author", "copyright"])
def test_needs_value_or_none(photos, command):
    assert CliRunner().invoke(cli, [command, str(photos)]).exit_code == 2


def test_bad_file_is_reported(tmp_path):
    make_image(tmp_path / "good.jpg")
    (tmp_path / "bad.jpg").write_text("not an image")
    result = imfix("tag", tmp_path, "x")
    assert "Failed: bad.jpg" in result.output
    assert "1 tagged" in result.output and "1 failed" in result.output


# --- rename ---------------------------------------------------------------------------------

def test_rename_uses_date_taken_then_file_date_with_counters(photos):
    for name in ("Zoë's café.jpg", "IMG_20230514_142301.jpg", "ABC_20250101_0935790135.png"):
        write(photos / name, "-DateTimeOriginal=2022:03:04 05:06:07")
    no_date = make_image(photos / "nodate.jpg")
    os.utime(no_date, (1_600_000_000, 1_600_000_000))

    imfix("rename", photos, "--in-place")
    result_names = names(photos)

    # Three images share a time: the first in name order keeps the plain name, the others count up.
    assert {"2022-03-04_05-06-07.png", "2022-03-04_05-06-07_1.jpg", "2022-03-04_05-06-07_2.jpg"} <= set(result_names)
    if not hasattr(os.stat(photos), "st_birthtime"):  # no creation date here, so the modified date is used
        assert datetime.fromtimestamp(1_600_000_000).strftime("%Y-%m-%d_%H-%M-%S.jpg") in result_names

    # Running again changes nothing.
    result = imfix("rename", photos, "--in-place")
    assert names(photos) == result_names
    assert "4 already named" in result.output


@pytest.mark.parametrize("prefix, expected", [("trip", "trip_"), ("trip_", "trip_"), ("Côte-", "Côte-")])
def test_rename_prefix(tmp_path, prefix, expected):
    write(make_image(tmp_path / "a.jpg"), "-DateTimeOriginal=2022:03:04 05:06:07")
    imfix("rename", tmp_path, "--prefix", prefix)
    assert names(tmp_path / "imfix") == [f"{expected}2022-03-04_05-06-07.jpg"]


def test_rename_rejects_bad_prefix(tmp_path):
    assert CliRunner().invoke(cli, ["rename", str(tmp_path), "--prefix", "a/b"]).exit_code == 2


def test_rename_includes_videos_in_local_time(tmp_path):
    make_video(tmp_path / "clip.mp4")
    imfix("rename", tmp_path)
    utc = datetime.fromisoformat("2021-06-07T08:09:10+00:00")
    assert names(tmp_path / "imfix") == [utc.astimezone().strftime("%Y-%m-%d_%H-%M-%S.mp4")]


# --- compress -------------------------------------------------------------------------------

def test_compress_image_keeps_metadata_and_dates(photos):
    src = photos / "Zoë's café.jpg"
    make_image(src, "1920x1080")
    write(src, "-DateTimeOriginal=2022:03:04 05:06:07", "-XMP-dc:Subject=kept")
    os.utime(src, (1_600_000_000, 1_600_000_000))

    result = imfix("compress", photos, "--image-format", "webp")
    assert result.exit_code == 0
    out = photos / "imfix" / "Zoë's café.webp"
    assert not (photos / "imfix" / "Zoë's café.jpg").exists()  # replaced by the compressed version
    tags = read(out, "DateTimeOriginal", "Subject")
    assert tags["DateTimeOriginal"] == "2022:03:04 05:06:07"
    assert tags["Subject"] == "kept"
    assert out.stat().st_mtime == 1_600_000_000

    stripped = photos.parent / "stripped"
    imfix("compress", photos, "-o", stripped, "--strip-metadata")
    assert "DateTimeOriginal" not in read(stripped / "Zoë's café.jpg", "DateTimeOriginal")


def test_compress_audio_to_opus_and_mp3_keeps_tags(tmp_path):
    make_audio(tmp_path / "Zoë.wav")
    imfix("compress", tmp_path)
    out = tmp_path / "imfix" / "Zoë.opus"
    assert out.exists() and not (tmp_path / "imfix" / "Zoë.wav").exists()
    assert read(out, "Title")["Title"] == "Zoë's song"

    mp3_dir = tmp_path.parent / "mp3"
    imfix("compress", tmp_path, "-o", mp3_dir, "--audio-format", "mp3", "--audio-bitrate", "64k")
    assert read(mp3_dir / "Zoë.mp3", "Title", "AudioBitrate")["Title"] == "Zoë's song"


def test_compress_video_keeps_dates(tmp_path):
    make_video(tmp_path / "clip.mov")
    result = imfix("compress", tmp_path, "-t", "video", "--preset", "ultrafast")
    assert "1 compressed" in result.output
    tags = read(tmp_path / "imfix" / "clip.mp4", "CreateDate", "CompressorID")
    assert tags["CreateDate"] == "2021:06:07 08:09:10"
    assert tags["CompressorID"] == "avc1"


def test_compress_only_selected_types(tmp_path):
    make_image(tmp_path / "a.png")
    make_audio(tmp_path / "b.wav")
    imfix("compress", tmp_path, "-t", "audio")
    assert names(tmp_path / "imfix") == ["a.png", "b.opus"]


def test_compress_name_clashes_never_overwrite(tmp_path):
    make_image(tmp_path / "a.jpg")
    make_image(tmp_path / "a.png")
    make_image(tmp_path / "x.bmp")
    make_image(tmp_path / "x.jpg")
    imfix("compress", tmp_path)
    assert names(tmp_path / "imfix") == ["a.jpg", "a_1.jpg", "x.jpg", "x_1.jpg"]


def test_compress_keeps_original_when_bigger(tmp_path):
    make_image(tmp_path / "small.jpg", "320x240", "-q:v", "31")
    before = (tmp_path / "small.jpg").read_bytes()
    result = imfix("compress", tmp_path, "--image-quality", "2")
    assert "kept the original" in result.output
    assert (tmp_path / "imfix" / "small.jpg").read_bytes() == before
    assert not any(p.name.startswith(".imfix-") for p in (tmp_path / "imfix").iterdir())
