"""imfix command-line interface."""

from __future__ import annotations

import functools
import os
import subprocess
import sys
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import click

from imfix import __version__
from imfix.dates import date_from_filename, parse_exif_date, to_exif
from imfix.exiftool import ExifTool, find
from imfix.files import (
    COMPRESS_EXTS,
    METADATA_EXTS,
    FileTimes,
    best_file_date,
    collect,
    copy_file,
    unique_stem,
)

DEFAULT_OUTPUT = "imfix"

# Tags that count as "the date the photo was taken", most trusted first.
DATE_TAGS = ["DateTimeOriginal", "CreateDate", "DateCreated"]

RENAME_FORMAT = "%Y-%m-%d_%H-%M-%S"

CONTEXT = {"help_option_names": ["-h", "--help"], "max_content_width": 100}


# --------------------------------------------------------------------------------------------
# Shared options and run state
# --------------------------------------------------------------------------------------------


class Run:
    """The options every subcommand shares, plus helpers for output and destinations."""

    def __init__(self, directory: Path, output: Path | None, in_place: bool, recurse: bool, verbose: bool, dry_run: bool):
        if in_place and output is not None:
            raise click.UsageError("--output and --in-place can't be used together.")
        self.input = directory.resolve()
        self.in_place = in_place
        self.output = None if in_place else (output.resolve() if output else self.input / DEFAULT_OUTPUT)
        self.recurse = recurse
        self.verbose = verbose
        self.dry_run = dry_run

    def files(self, exts: set[str]) -> list[Path]:
        files = collect(self.input, self.recurse, exts, exclude=self.output)
        if not files:
            where = "in or below" if self.recurse else "in"
            self.info(f"No matching files found {where} {self.input}")
        elif self.verbose:
            self.info(f"Found {len(files)} file(s) in {self.input}")
        return files

    def destination_dir(self, src: Path) -> Path:
        if self.in_place:
            return src.parent
        return self.output / src.parent.relative_to(self.input)

    def show(self, path: Path) -> str:
        """A short form of `path` for messages."""
        for base in (self.input, self.output):
            if base is not None:
                try:
                    return str(path.relative_to(base))
                except ValueError:
                    pass
        return str(path)

    def info(self, message: str) -> None:
        click.echo(message)

    def detail(self, message: str) -> None:
        if self.verbose:
            click.echo(message)

    def warn(self, message: str) -> None:
        click.echo(click.style(message, fg="yellow"), err=True)

    def error(self, message: str) -> None:
        click.echo(click.style(message, fg="red"), err=True)

    def summary(self, parts: dict[str, int]) -> None:
        text = ", ".join(f"{count} {label}" for label, count in parts.items() if count or label == next(iter(parts)))
        if self.dry_run:
            self.info(f"Dry run: {text}. Nothing was changed.")
        elif self.in_place:
            self.info(f"Done: {text}.")
        else:
            self.info(f"Done: {text}. Output in {self.output}")


def common_options(in_place: bool = True):
    """The DIR argument and the options every subcommand accepts."""

    def decorator(f):
        @functools.wraps(f)
        def wrapper(directory, output, recurse, verbose, dry_run, in_place=False, **kwargs):
            run = Run(directory, output, in_place, recurse, verbose, dry_run)
            return f(run, **kwargs)

        options = [
            click.argument("directory", metavar="DIR", type=click.Path(exists=True, file_okay=False, path_type=Path)),
            click.option("-r", "--recurse", is_flag=True, help="Include files in subfolders."),
            click.option("-v", "--verbose", is_flag=True, help="Show every file and the commands being run."),
            click.option("-n", "--dry-run", is_flag=True, help="Show what would happen without changing anything."),
            click.option(
                "-o", "--output", metavar="FOLDER", type=click.Path(file_okay=False, path_type=Path),
                help=f'Folder for the results. Default: an "{DEFAULT_OUTPUT}" folder inside DIR.',
            ),
        ]
        if in_place:
            options.append(click.option(
                "-i", "--in-place", is_flag=True,
                help="Change the original files instead of writing copies to the output folder.",
            ))
        for option in reversed(options):
            wrapper = option(wrapper)
        return wrapper

    return decorator


def iptc_charset(values) -> list[str]:
    """IPTC needs to be told about UTF-8, otherwise accented characters get mangled."""
    if any(not v.isascii() for v in values):
        return ["-IPTC:CodedCharacterSet=UTF8"]
    return []


def apply_metadata(run: Run, plans: list[tuple[Path, list[str] | None]], did: str, would: str) -> None:
    """Copy files to the output folder (unless --in-place) and apply exiftool arguments to them.

    `plans` holds (file, exiftool arguments). Files with None as arguments are copied unchanged,
    so the output folder still ends up with the full set of images.
    """
    done_label = f"would be {did}" if run.dry_run else did
    counts = {done_label: 0, "unchanged": 0, "skipped": 0, "failed": 0}

    staged = []  # (src, dst, args, times)
    for src, args in plans:
        dst = run.destination_dir(src) / src.name

        if not run.in_place and dst.exists():
            run.warn(f"Skipping {run.show(src)}: already in the output folder.")
            counts["skipped"] += 1
            continue

        if args is None:
            counts["unchanged"] += 1
            if not run.in_place and not run.dry_run:
                copy_file(src, dst)
            continue

        if run.dry_run:
            counts[done_label] += 1
            run.info(f"Would {would} {run.show(src)}")
            run.detail(f"    exiftool {' '.join(args)}")
            continue

        times = FileTimes.of(src)
        if not run.in_place:
            copy_file(src, dst)
        staged.append((src, dst, args, times))

    if staged:
        problems = ExifTool(run.verbose).write([(dst, args) for _, dst, args, _ in staged])
        for src, dst, args, times in staged:
            messages = problems.get(dst, [])
            errors = [m for m in messages if m.startswith("Error")]
            if errors:
                counts["failed"] += 1
                run.error(f"Failed: {run.show(src)}")
                for message in errors:
                    run.error(f"    {message}")
                if not run.in_place:
                    dst.unlink(missing_ok=True)
                continue

            times.apply(dst)
            counts[did] += 1
            run.detail(f"{did.capitalize()}: {run.show(src)}")
            for message in messages:
                run.detail(f"    {message}")

    run.summary(counts)


# --------------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------------


@click.group(context_settings=CONTEXT, invoke_without_command=True)
@click.version_option(__version__, "-V", "--version")
@click.pass_context
def cli(ctx: click.Context):
    """imfix - batch-edit image metadata with exiftool.

    \b
    Every command works on all images in DIR. Use "." for the current folder.
    Results go to an "imfix" folder inside DIR, so the originals are never
    changed unless you pass --in-place.

    \b
    Every command accepts:
      -r, --recurse    include subfolders
      -v, --verbose    show every file and the commands being run
      -n, --dry-run    show what would happen without changing anything
      -o, --output     choose the output folder

    \b
    Examples:
      imfix tag . holiday "Côte d'Azur"     add two tags to every image here
      imfix date . -r                       set missing dates from filenames
      imfix rename "D:\\Photos\\2025" -n      preview the new names
      imfix compress . --format webp        compress images to WebP
      imfix author . "Jane Smith" -i        change the originals directly

    Run "imfix COMMAND --help" for details on a command.
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@cli.command(context_settings=CONTEXT, no_args_is_help=True, short_help="Add tags to every image.")
@common_options()
@click.argument("tags", nargs=-1, required=True)
def tag(run: Run, tags: tuple[str, ...]):
    """Add one or more TAGS to every image in DIR.

    Tags are added to any existing ones (duplicates are skipped) and are written as XMP and IPTC
    keywords, so they show up as Tags in Windows Explorer, Lightroom, digiKam and similar.

    \b
    Example:
      imfix tag . holiday "summer 2025"
    """
    args = iptc_charset(tags)
    for t in tags:
        # Removing first, then adding, means a tag is never added twice.
        args += [f"-XMP-dc:Subject-={t}", f"-XMP-dc:Subject+={t}", f"-IPTC:Keywords-={t}", f"-IPTC:Keywords+={t}"]

    files = run.files(METADATA_EXTS)
    if files:
        apply_metadata(run, [(f, args) for f in files], did="tagged", would="tag")


@cli.command(context_settings=CONTEXT, no_args_is_help=True, short_help="Set missing dates taken from the filenames.")
@common_options()
def date(run: Run):
    """Set the date taken from the filename, for images that don't have one.

    Images that already have a date taken (DateTimeOriginal, CreateDate or DateCreated) are
    left alone. Recognised names include:

    \b
      IMG_20230514_142301.jpg           2023-05-14 14:23:01
      PXL_20230514_142301123.jpg        2023-05-14 14:23:01
      ABC_20250101_0935790135.jpg       2025-01-01 00:00:00 (random number, not a time)
      2023-05-14 14.23.01.png           2023-05-14 14:23:01
      Screenshot_2023-05-14-14-23-01    2023-05-14 14:23:01
      IMG-20230514-WA0001.jpg           2023-05-14 00:00:00
    """
    files = run.files(METADATA_EXTS)
    if not files:
        return

    existing = ExifTool(run.verbose).read(files, DATE_TAGS)
    plans = []
    for f in files:
        tags = ExifTool.lookup(existing, f)
        current = next((tags[t] for t in DATE_TAGS if parse_exif_date(tags.get(t))), None)
        if current:
            run.detail(f"Leaving {run.show(f)}: already dated {current}")
            plans.append((f, None))
            continue

        found = date_from_filename(f.name)
        if found is None:
            run.detail(f"Leaving {run.show(f)}: no date in the filename")
            plans.append((f, None))
            continue

        value = to_exif(found)
        plans.append((f, [f"-DateTimeOriginal={value}", f"-CreateDate={value}"]))

    apply_metadata(run, plans, did="dated", would="date")


@cli.command("strip-copyright", context_settings=CONTEXT, no_args_is_help=True, short_help="Remove all copyright info.")
@common_options()
def strip_copyright(run: Run):
    """Remove all copyright and usage-rights information from every image in DIR.

    Removes every tag with "copyright" in its name (EXIF, IPTC, XMP, PNG) plus the XMP rights
    tags (dc:Rights, xmpRights and PLUS licensing).
    """
    args = ["-*copyright*=", "-XMP-dc:Rights=", "-XMP-xmpRights:all=", "-XMP-plus:all="]
    files = run.files(METADATA_EXTS)
    if files:
        apply_metadata(run, [(f, args) for f in files], did="cleaned", would="strip copyright from")


@cli.command(context_settings=CONTEXT, no_args_is_help=True, short_help="Set or remove the author.")
@common_options()
@click.argument("name", required=False)
@click.option("--none", "clear", is_flag=True, help="Remove the author instead of setting one.")
def author(run: Run, name: str | None, clear: bool):
    """Set NAME as the author of every image in DIR, or remove it with --none.

    Writes EXIF Artist, XMP Creator, IPTC By-line and the Windows "Authors" field.

    \b
    Examples:
      imfix author . "Jane Smith"
      imfix author . --none
    """
    if clear == (name is not None):
        raise click.UsageError('Give either a NAME or --none (e.g. imfix author . "Jane Smith").')

    if clear:
        args = ["-Artist=", "-XMP-dc:Creator=", "-IPTC:By-line=", "-XPAuthor=", "-Author="]
        did, would = "cleared", "remove the author from"
    else:
        args = iptc_charset([name]) + [
            f"-Artist={name}", f"-XMP-dc:Creator={name}", f"-IPTC:By-line={name}", f"-XPAuthor={name}",
        ]
        did, would = "updated", f'set the author to "{name}" on'

    files = run.files(METADATA_EXTS)
    if files:
        apply_metadata(run, [(f, args) for f in files], did=did, would=would)


@cli.command(context_settings=CONTEXT, no_args_is_help=True, short_help="Rename to yyyy-mm-dd_hh-mm-ss.")
@common_options()
def rename(run: Run):
    """Rename every image in DIR to yyyy-mm-dd_hh-mm-ss.

    The date taken is used where there is one. Otherwise the file's created date is used
    (or its modified date on systems that don't record one, which includes most of Linux).
    Images with the same time get _1, _2... added, in order of their original names.

    Tip: run "imfix date" first so images with a date in their name keep it.
    """
    files = run.files(METADATA_EXTS | COMPRESS_EXTS)
    if not files:
        return

    tags = ExifTool(run.verbose).read(files, DATE_TAGS)
    dated: list[tuple[datetime, str, Path]] = []
    for f in files:
        values = ExifTool.lookup(tags, f)
        when = next((d for t in DATE_TAGS if (d := parse_exif_date(values.get(t)))), None)
        source = "date taken"
        if when is None:
            when, source = best_file_date(f), "file date"
        dated.append((when, source, f))
    dated.sort(key=lambda item: (item[0], item[2].name.casefold()))

    # Names already used in each destination folder. Files being renamed don't block their own names.
    batch = {os.path.normcase(f) for f in files}
    taken: dict[Path, set[str]] = {}
    for _, _, f in dated:
        folder = run.destination_dir(f)
        if folder not in taken:
            taken[folder] = {
                p.stem.casefold() for p in (folder.iterdir() if folder.is_dir() else [])
                if os.path.normcase(p) not in batch
            }

    plan = []
    for when, source, f in dated:
        folder = run.destination_dir(f)
        new = folder / (unique_stem(when.strftime(RENAME_FORMAT), taken[folder]) + f.suffix)
        plan.append((f, new, source))

    counts = {"renamed": 0, "already named": 0}
    moves = []
    for src, dst, source in plan:
        if run.in_place and src.name == dst.name:
            counts["already named"] += 1
            run.detail(f"{run.show(src)} is already named correctly")
            continue
        if run.dry_run or run.verbose:
            run.info(f"{'Would rename' if run.dry_run else 'Renaming'} {run.show(src)} -> {dst.name}  ({source})")
        counts["renamed"] += 1
        if not run.dry_run:
            moves.append((src, dst))

    if run.in_place:
        # Two steps, so one file can take a name another file in the batch is giving up.
        temporary = []
        for src, dst in moves:
            temp = src.with_name(f".imfix-{uuid.uuid4().hex}{src.suffix}")
            src.rename(temp)
            temporary.append((temp, dst))
        for temp, dst in temporary:
            temp.rename(dst)
    else:
        for src, dst in moves:
            copy_file(src, dst)

    if run.dry_run:
        counts = {"would be renamed": counts["renamed"], "already named": counts["already named"]}
    run.summary(counts)


@cli.command(context_settings=CONTEXT, no_args_is_help=True, short_help="Compress images to JPEG or WebP.")
@common_options(in_place=False)
@click.option("-f", "--format", "fmt", type=click.Choice(["jpg", "webp"]), default="jpg", show_default=True,
              help="Output format. Use webp to keep PNG transparency.")
@click.option("-q", "--quality", type=int,
              help="JPEG: 2 (best) to 31 (worst), default 5.  WebP: 0 to 100 (best), default 75.")
@click.option("--strip-metadata", is_flag=True, help="Don't copy metadata to the compressed images.")
def compress(run: Run, fmt: str, quality: int | None, strip_metadata: bool):
    """Compress every image in DIR with ffmpeg.

    Images are re-encoded as JPEG (default) or WebP into the output folder. Metadata (date
    taken, camera, GPS, tags...) and file dates are copied over unless --strip-metadata is
    given. This command never changes the originals, so it has no --in-place option.

    Supported inputs: jpg jpeg png bmp tif tiff webp. Needs ffmpeg on the PATH.
    """
    if quality is None:
        quality = 5 if fmt == "jpg" else 75
    valid = range(2, 32) if fmt == "jpg" else range(0, 101)
    if quality not in valid:
        raise click.BadParameter(f"must be between {valid.start} and {valid.stop - 1} for {fmt}", param_hint="--quality")

    files = run.files(COMPRESS_EXTS)
    if not files:
        return
    ffmpeg = find("ffmpeg", "Install it from https://ffmpeg.org (Windows: winget install ffmpeg).")
    exiftool = None if strip_metadata or run.dry_run else ExifTool(run.verbose)

    if fmt == "jpg":
        codec_args = ["-q:v", str(quality)]
    else:
        codec_args = ["-c:v", "libwebp", "-quality", str(quality)]

    # Two inputs can map to the same output (photo.png and photo.jpg -> photo.jpg).
    taken: dict[Path, set[str]] = defaultdict(set)
    counts = {"compressed": 0, "skipped": 0, "failed": 0}
    if run.dry_run:
        counts = {"would be compressed": 0, "skipped": 0}
    done = []
    total_before = total_after = 0

    for i, src in enumerate(files, 1):
        folder = run.destination_dir(src)
        dst = folder / f"{unique_stem(src.stem, taken[folder])}.{fmt}"
        prefix = f"[{i}/{len(files)}]"

        if dst.exists():
            run.warn(f"{prefix} Skipping {run.show(src)}: {run.show(dst)} already exists.")
            counts["skipped"] += 1
            continue

        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                   # Keep pixels as stored, so the copied EXIF orientation still displays correctly.
                   "-noautorotate", "-i", str(src),
                   "-map_metadata", "-1", "-frames:v", "1", *codec_args, str(dst)]

        if run.dry_run:
            counts["would be compressed"] += 1
            run.info(f"Would compress {run.show(src)} -> {run.show(dst)}")
            run.detail("    " + subprocess.list2cmdline(command))
            continue

        run.info(f"{prefix} Compressing {run.show(src)}...")
        run.detail("    " + subprocess.list2cmdline(command))
        folder.mkdir(parents=True, exist_ok=True)
        times = FileTimes.of(src)
        result = subprocess.run(command, capture_output=True)
        if result.returncode != 0 or not dst.exists():
            counts["failed"] += 1
            run.error(f"    Failed: {result.stderr.decode('utf-8', 'replace').strip()}")
            dst.unlink(missing_ok=True)
            continue

        before, after = src.stat().st_size, dst.stat().st_size
        total_before += before
        total_after += after
        run.info(f"    {_size_change(before, after)}")
        counts["compressed"] += 1
        done.append((src, dst, times))

    if exiftool and done:
        run.info(f"Copying metadata for {len(done)} file(s)...")
        problems = exiftool.write([
            (dst, ["-TagsFromFile", str(src), "-all:all", "-ICC_Profile"]) for src, dst, _ in done
        ])
        for src, dst, _ in done:
            errors = [m for m in problems.get(dst, []) if m.startswith("Error")]
            if errors:
                run.warn(f"Metadata not copied to {run.show(dst)}: {errors[0]}")

    for _, dst, times in done:
        times.apply(dst)

    if total_before:
        run.info(f"Total: {_size_change(total_before, total_after)}")
    run.summary(counts)


def _size_change(before: int, after: int) -> str:
    text = f"{before / 1_000_000:.2f} MB -> {after / 1_000_000:.2f} MB"
    if after >= before:
        return f"{text} (no saving - the original was already well compressed)"
    return f"{text} ({100 * (1 - after / before):.0f}% smaller)"


def main():
    # Never crash on a filename the console can't display (e.g. a non-UTF-8 Windows console).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    cli()


if __name__ == "__main__":
    main()
