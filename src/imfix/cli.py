"""imfix command-line interface."""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

import click

from imfix import __version__
from imfix.compress import (
    DEFAULT_AUDIO_BITRATE, MARKER_TAGS, PRESETS, VIDEO_CODECS, Settings, history_entry, is_marked, kind_of,
)
from imfix.dates import date_from_filename, parse_exif_date, to_exif
from imfix.exiftool import ExifTool, find
from imfix.files import (
    AUDIO_EXTS,
    IMAGE_EXTS,
    MEDIA_EXTS,
    METADATA_EXTS,
    TEMP_PREFIX,
    VIDEO_EXTS,
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

# Image types whose extension imfix corrects in the working copy when it doesn't match the
# contents (e.g. a WebP image saved as photo.jpg), keyed by exiftool's FileTypeExtension.
IMAGE_TYPE_EXTS = {
    "jpg": {".jpg", ".jpeg", ".jpe"}, "png": {".png"}, "webp": {".webp"}, "gif": {".gif"},
    "tif": {".tif", ".tiff"}, "heic": {".heic", ".heif"}, "avif": {".avif"}, "bmp": {".bmp"},
}

# Characters Windows doesn't allow in filenames.
BAD_NAME_CHARS = set('<>:"/\\|?*')

CONTEXT = {"help_option_names": ["-h", "--help"], "max_content_width": 100}

_WORKING_COPY_STEPS = f"""How DIR is used: the first command run on DIR copies all its images, audio
and video into DIR/{DEFAULT_OUTPUT} (the working copy, or the --output folder) and
changes the copies. Every later command on the same DIR changes those same
copies, so commands build on each other, e.g. "imfix rename ." then
"imfix compress .". The originals are never changed. --fresh deletes the
working copy and copies the originals again, which is also how to pick up
files added to DIR later. Running a command inside DIR/imfix does the same as
running it on DIR. --recurse only matters when the working copy is made: it
decides whether subfolders are copied. After that, every command changes all
the copies, subfolders included. Folders starting with "." are always
ignored."""

# Shown at the end of each command's --help.
WORKING_COPY_EPILOG = _WORKING_COPY_STEPS + """

--in-place skips the working copy and changes the originals in DIR directly."""

COMPRESS_EPILOG = _WORKING_COPY_STEPS + """

compress has no --in-place, so it always works on the working copy."""


# --------------------------------------------------------------------------------------------
# Shared options and run state
# --------------------------------------------------------------------------------------------


class Run:
    """The options every subcommand shares, and the folder the command works on."""

    def __init__(self, directory: Path, output: Path | None, in_place: bool, fresh: bool,
                 recurse: bool, verbose: bool, dry_run: bool):
        if in_place and output is not None:
            raise click.UsageError("--output and --in-place can't be used together.")
        if in_place and fresh:
            raise click.UsageError("--fresh and --in-place can't be used together.")
        self.input = directory.resolve()
        self.in_place = in_place
        self.custom_output = output is not None
        # The working copy folder, or None with --in-place.
        self.working: Path | None = None if in_place else (output.resolve() if output else self.input / DEFAULT_OUTPUT)

        # Running inside a working copy (e.g. "cd imfix" then "imfix compress .") means the same as
        # running on the folder above it, rather than making a working copy of the working copy.
        self.inside_working_copy = (
            not in_place and output is None and self.input.name.casefold() == DEFAULT_OUTPUT.casefold()
        )
        if self.inside_working_copy:
            self.working = self.input
            self.input = self.input.parent
        self.fresh = fresh
        self.recurse = recurse
        self.verbose = verbose
        self.dry_run = dry_run
        self._root: Path | None = None

    @property
    def root(self) -> Path:
        """The folder whose files get changed: the working copy, or DIR itself with --in-place.

        The first time this is used, the working copy is filled with copies of the originals.
        """
        if self._root is None:
            self._root = self._prepare()
            if self.working is not None:
                self._fix_extensions(self._root)
        return self._root

    def _fix_extensions(self, root: Path) -> None:
        """Give images in the working copy the extension that matches what they really are.

        A WebP or PNG saved with a .jpg name opens fine in Windows, but exiftool refuses to
        write to it. Only copies are renamed: with --in-place the originals are left alone.
        """
        images = collect(root, True, set().union(*IMAGE_TYPE_EXTS.values()), exclude=self.working if root == self.input else None)
        types = ExifTool(self.verbose).read(images, ["FileType", "FileTypeExtension"], fast=True)
        taken: dict[Path, set[str]] = {}
        for path in images:
            tags = ExifTool.lookup(types, path)
            real = str(tags.get("FileTypeExtension", "")).lower()
            if real not in IMAGE_TYPE_EXTS or path.suffix.lower() in IMAGE_TYPE_EXTS[real]:
                continue
            if path.parent not in taken:
                taken[path.parent] = {p.name.casefold() for p in path.parent.iterdir()}
            ext = "." + real
            new = path.with_name(unique_stem(path.stem, taken[path.parent], ext) + ext)
            why = f"it's really a {tags.get('FileType', real.upper())} image"
            if self.dry_run or root != self.working:
                self.info(f"Would rename {self.show(path)} -> {new.name} in the working copy ({why}).")
            else:
                path.rename(new)
                self.info(f"Renamed {self.show(path)} -> {new.name} ({why}).")

    def _prepare(self) -> Path:
        working = self.working
        if working is None:
            return self.input
        if self.inside_working_copy:
            self.info(f"{working} is an imfix working copy, so changing it (the originals are in {self.input}).")

        if self.fresh and working.is_dir() and any(working.iterdir()):
            if self.dry_run:
                self.info(f"Would delete the working copy in {working} and start again from the originals.")
                return self.input
            # The default folder is imfix's own. A folder given with --output might not be, so ask first.
            if self.custom_output and not click.confirm(
                f"--fresh will delete {working} and everything in it. Continue?", default=False
            ):
                raise click.Abort()
            shutil.rmtree(working)
            self.info("Deleted the old working copy.")

        copies = collect(working, True, MEDIA_EXTS, exclude=None) if working.is_dir() else []
        if copies:
            self.info(f"Working on the copies in {working} (add --fresh to start again from the originals).")
            # A working copy with subfolders was made with --recurse, so compare like with like.
            made_with_recurse = any(c.parent != working for c in copies)
            originals = collect(self.input, self.recurse or made_with_recurse, MEDIA_EXTS, exclude=working)
            if len(originals) > len(copies):
                self.warn(f"{len(originals) - len(copies)} more file(s) in {self.input} than in the working copy. "
                          "New files aren't added automatically: use --fresh to include them.")
            return working

        sources = collect(self.input, self.recurse, MEDIA_EXTS, exclude=working)
        if not sources:
            # Nothing to copy, so there's no working copy to change: look in DIR itself, which
            # reports that no files were found.
            return working if working.is_dir() else self.input
        if self.dry_run:
            self.info(f"Would copy {len(sources)} file(s) to {working} and change the copies.")
            return self.input
        for src in sources:
            copy_file(src, working / src.relative_to(self.input))
        self.info(f"Copied {len(sources)} file(s) to {working}")
        return working

    def files(self, exts: set[str]) -> list[Path]:
        root = self.root
        exclude = self.working if root == self.input else None
        # The working copy holds only what was chosen when it was made (with or without --recurse),
        # so every file in it is included, however deep.
        recurse = self.recurse or root == self.working
        files = collect(root, recurse, exts, exclude=exclude)
        if not files:
            where = "in or below" if recurse else "in"
            hint = ""
            if not recurse and collect(root, True, exts, exclude=exclude):
                hint = " There are some in subfolders: add -r to include them."
            self.info(f"No matching files found {where} {root}.{hint}")
        else:
            self.detail(f"Found {len(files)} file(s) in {root}")
        return files

    def show(self, path: Path) -> str:
        """A short form of `path` for messages."""
        for base in (self.root, self.input):
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
        first = next(iter(parts))
        text = ", ".join(f"{count} {label}" for label, count in parts.items() if count or label == first)
        if self.dry_run:
            self.info(f"Dry run: {text}. Nothing was changed.")
        elif self.working is None:
            self.info(f"Done: {text}.")
        else:
            self.info(f"Done: {text}. Results in {self.working}")


def common_options(in_place: bool = True):
    """The DIR argument and the options every subcommand accepts."""

    def decorator(f):
        @functools.wraps(f)
        def wrapper(directory, output, fresh, recurse, verbose, dry_run, in_place=False, **kwargs):
            run = Run(directory, output, in_place, fresh, recurse, verbose, dry_run)
            return f(run, **kwargs)

        options = [
            click.argument("directory", metavar="DIR", type=click.Path(exists=True, file_okay=False, path_type=Path)),
            click.option("-r", "--recurse", is_flag=True,
                         help="Include subfolders (except ones starting with \".\") when making the working "
                              "copy, or with --in-place. Commands on an existing working copy always include "
                              "all of it."),
            click.option("-v", "--verbose", is_flag=True, help="Show every file and the commands being run."),
            click.option("-n", "--dry-run", is_flag=True, help="Show what would happen without changing anything."),
            click.option(
                "-o", "--output", metavar="FOLDER", type=click.Path(file_okay=False, path_type=Path),
                help=f'Folder to use as the working copy. Default: an "{DEFAULT_OUTPUT}" folder inside DIR. '
                     "Give the same folder on every command.",
            ),
            click.option("--fresh", is_flag=True,
                         help="Delete the working copy (and every change made to it) and copy the originals again. "
                              "Asks first when the working copy is an --output folder."),
        ]
        if in_place:
            options.append(click.option(
                "-i", "--in-place", is_flag=True,
                help="Change the original files in DIR directly, without a working copy.",
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
    """Apply each file's exiftool arguments to it. Files with None as arguments are left alone."""
    done_label = f"would be {did}" if run.dry_run else did
    counts = {done_label: 0, "unchanged": 0, "failed": 0}

    staged = []  # (path, args, times)
    for path, args in plans:
        if args is None:
            counts["unchanged"] += 1
        elif run.dry_run:
            counts[done_label] += 1
            run.info(f"Would {would} {run.show(path)}")
            run.detail(f"    exiftool {' '.join(args)}")
        else:
            staged.append((path, args, FileTimes.of(path)))

    if staged:
        problems = ExifTool(run.verbose).write([(path, args) for path, args, _ in staged])
        for path, args, times in staged:
            messages = problems.get(path, [])
            errors = [m for m in messages if m.startswith("Error")]
            if errors:
                counts["failed"] += 1
                run.error(f"Failed: {run.show(path)}")
                for message in errors:
                    run.error(f"    {message}")
                if any("looks more like" in m for m in errors):
                    run.error("    Its extension doesn't match what's inside (e.g. a WebP image named .jpg). "
                              "imfix fixes this itself in the working copy; with --in-place, rename the file first.")
                continue

            times.apply(path)
            counts[did] += 1
            run.detail(f"{did.capitalize()}: {run.show(path)}")
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

    Every command works on the files in DIR. Use "." for the current folder.

    \b
    Running several commands in a row:
      1. The first command copies all the images, audio and video in DIR into
         DIR/imfix (the "working copy") and changes the copies.
      2. Every later command on the same DIR changes those same copies, so
         each one builds on the last: rename, then compress, then tag...
      3. The originals are never changed. Add --fresh to any command to
         delete the working copy and copy the originals again (do this to
         pick up files added to DIR later, or to undo everything).
    You can also run commands from inside the working copy: "imfix compress ."
    in DIR/imfix does the same as "imfix compress ." in DIR.
    --in-place skips the working copy and changes the originals directly.
    --recurse only matters when the working copy is made (or with --in-place):
    after that, every command changes all the copies, subfolders included.
    Folders whose names start with "." are always ignored.

    \b
    Every command accepts:
      -r, --recurse    include subfolders when making the working copy
      -v, --verbose    show every file and the commands being run
      -n, --dry-run    show what would happen without changing anything
      -o, --output     use another folder as the working copy
      --fresh          delete the working copy and start again
      -i, --in-place   change the originals (all commands except compress)

    \b
    Examples (the first three run in order, each building on the last):
      imfix date .                          set missing dates from filenames
      imfix rename . --prefix trip          rename to trip_2025-01-01_...
      imfix compress .                      compress them all
      imfix tag . holiday --fresh           start again, just add a tag
      imfix author . "Jane Smith" -i        change the originals directly

    Run "imfix COMMAND --help" for details on a command.
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@cli.command(context_settings=CONTEXT, no_args_is_help=True, short_help="Add tags to every image.",
             epilog=WORKING_COPY_EPILOG)
@common_options()
@click.argument("tags", nargs=-1, required=True)
def tag(run: Run, tags: tuple[str, ...]):
    """Add one or more TAGS to every image.

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


@cli.command(context_settings=CONTEXT, no_args_is_help=True, short_help="Set missing dates taken from the filenames.",
             epilog=WORKING_COPY_EPILOG)
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


@cli.command(context_settings=CONTEXT, no_args_is_help=True, short_help="Set or remove the copyright.",
             epilog=WORKING_COPY_EPILOG)
@common_options()
@click.argument("text", required=False)
@click.option("--none", "clear", is_flag=True, help="Remove all copyright info instead of setting it.")
def copyright(run: Run, text: str | None, clear: bool):
    """Set TEXT as the copyright of every image, or remove it all with --none.

    --none removes every tag with "copyright" in its name (EXIF, IPTC, XMP, PNG) plus the XMP
    rights tags (dc:Rights, xmpRights and PLUS licensing).

    \b
    Examples:
      imfix copyright . --none
      imfix copyright . "© 2025 Jane Smith"
    """
    if clear == (text is not None):
        raise click.UsageError('Give either the copyright TEXT or --none (e.g. imfix copyright . --none).')

    if clear:
        args = ["-*copyright*=", "-XMP-dc:Rights=", "-XMP-xmpRights:all=", "-XMP-plus:all="]
        did, would = "cleaned", "remove the copyright from"
    else:
        args = iptc_charset([text]) + [f"-Copyright={text}", f"-XMP-dc:Rights={text}", f"-IPTC:CopyrightNotice={text}"]
        did, would = "updated", f'set the copyright to "{text}" on'

    files = run.files(METADATA_EXTS)
    if files:
        apply_metadata(run, [(f, args) for f in files], did=did, would=would)


@cli.command(context_settings=CONTEXT, no_args_is_help=True, short_help="Set or remove the author.",
             epilog=WORKING_COPY_EPILOG)
@common_options()
@click.argument("name", required=False)
@click.option("--none", "clear", is_flag=True, help="Remove the author instead of setting one.")
def author(run: Run, name: str | None, clear: bool):
    """Set NAME as the author of every image, or remove it with --none.

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


def _check_prefix(ctx, param, value: str | None) -> str | None:
    if value is None:
        return None
    if BAD_NAME_CHARS & set(value) or value != value.strip():
        raise click.BadParameter('can\'t contain < > : " / \\ | ? * or start/end with spaces.')
    return value


@cli.command(context_settings=CONTEXT, no_args_is_help=True, short_help="Rename to yyyy-mm-dd_hh-mm-ss.",
             epilog=WORKING_COPY_EPILOG)
@common_options()
@click.option("-p", "--prefix", callback=_check_prefix,
              help='Text to put before every name, e.g. "trip" gives trip_2025-01-01_09-30-00.jpg.')
def rename(run: Run, prefix: str | None):
    """Rename every image and video to yyyy-mm-dd_hh-mm-ss.

    The date taken is used where there is one. Otherwise the file's created date is used
    (or its modified date on systems that don't record one, which includes most of Linux).
    Files with the same time get _1, _2... added, in order of their original names.

    Tip: run "imfix date" first so images with a date in their name keep it.

    \b
    Examples:
      imfix rename .                    2025-01-01_09-30-00.jpg
      imfix rename . --prefix trip      trip_2025-01-01_09-30-00.jpg
    """
    files = run.files(METADATA_EXTS | IMAGE_EXTS | VIDEO_EXTS)
    if not files:
        return

    start = ""
    if prefix:
        start = prefix if prefix[-1] in "_- " else prefix + "_"

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

    # Names already used in each folder. Files being renamed don't block their own names.
    batch = {os.path.normcase(f) for f in files}
    taken: dict[Path, set[str]] = {}
    for f in files:
        if f.parent not in taken:
            taken[f.parent] = {p.stem.casefold() for p in f.parent.iterdir() if os.path.normcase(p) not in batch}

    counts = {"renamed": 0, "already named": 0}
    moves = []
    for when, source, f in dated:
        new = f.with_name(unique_stem(start + when.strftime(RENAME_FORMAT), taken[f.parent]) + f.suffix)
        if new.name == f.name:
            counts["already named"] += 1
            run.detail(f"{run.show(f)} is already named correctly")
            continue
        counts["renamed"] += 1
        if run.dry_run or run.verbose:
            run.info(f"{'Would rename' if run.dry_run else 'Renaming'} {run.show(f)} -> {new.name}  ({source})")
        moves.append((f, new))

    if not run.dry_run:
        # Two steps, so one file can take a name another file in the batch is giving up.
        temporary = []
        for src, dst in moves:
            temp = src.with_name(f"{TEMP_PREFIX}{uuid.uuid4().hex}{src.suffix}")
            src.rename(temp)
            temporary.append((temp, dst))
        for temp, dst in temporary:
            temp.rename(dst)
    else:
        counts = {"would be renamed": counts["renamed"], "already named": counts["already named"]}
    run.summary(counts)


@cli.command(context_settings=CONTEXT, no_args_is_help=True, short_help="Compress images, audio and video.",
             epilog=COMPRESS_EPILOG)
@common_options(in_place=False)
@click.option("-t", "--type", "types", multiple=True, type=click.Choice(["image", "audio", "video"]),
              help="Only compress this type of file. Can be given more than once. Default: all three.")
@click.option("--image-format", type=click.Choice(["jpg", "webp"]), default="jpg", show_default=True,
              help="Image output format. Use webp to keep PNG transparency.")
@click.option("--image-quality", type=int,
              help="JPEG: 2 (best) to 31 (worst), default 5.  WebP: 0 to 100 (best), default 75.")
@click.option("--audio-format", type=click.Choice(["opus", "mp3", "m4a"]), default="opus", show_default=True,
              help="Audio output format. Opus is smallest for the quality; mp3 plays everywhere.")
@click.option("--audio-bitrate",
              help="Audio bitrate, e.g. 64k. Defaults: opus 96k, mp3 160k, m4a 128k. "
                   "Opus guide: 32k speech, 64k podcasts, 96k-128k music.")
@click.option("--video-codec", type=click.Choice(list(VIDEO_CODECS)), default="h264", show_default=True,
              help="h265 gives smaller files but encodes slower and plays on fewer old devices.")
@click.option("--crf", type=click.IntRange(0, 51), default=28, show_default=True,
              help="Video quality. Lower = better and bigger. 18 is near-lossless, 30+ is small.")
@click.option("--preset", type=click.Choice(PRESETS), default="medium", show_default=True,
              help="Video encoding speed. Slower gives slightly smaller files.")
@click.option("--strip-metadata", is_flag=True, help="Don't keep any metadata in the compressed files.")
@click.option("--recompress", is_flag=True, help="Compress files again even if imfix already compressed them.")
def compress(run: Run, types: tuple[str, ...], image_format: str, image_quality: int | None, audio_format: str,
             audio_bitrate: str | None, video_codec: str, crf: int, preset: str, strip_metadata: bool,
             recompress: bool):
    """Compress every image, audio and video file with ffmpeg.

    \b
      Images  -> JPEG (default) or WebP
      Audio   -> Opus (default), MP3 or M4A
      Video   -> MP4 with H.264 (default) or H.265

    Each file in the working copy is replaced by its compressed version (the originals in DIR
    are untouched). Metadata (date taken, camera, GPS, tags, audio tags and cover art) and file
    dates are kept unless --strip-metadata is given. If a compressed file comes out bigger, the
    original is kept instead.

    Compressed files are marked inside the file (in the XMP history for images and video, and
    an "imfix" tag for audio), so running this again skips them rather than losing quality.

    Needs ffmpeg on the PATH.

    \b
    Examples:
      imfix compress .                          everything, with the defaults
      imfix compress . -t audio --audio-bitrate 64k
      imfix compress . -t video --video-codec h265 --crf 30
    """
    if image_quality is None:
        image_quality = 5 if image_format == "jpg" else 75
    valid = range(2, 32) if image_format == "jpg" else range(0, 101)
    if image_quality not in valid:
        raise click.BadParameter(f"must be between {valid.start} and {valid.stop - 1} for {image_format}",
                                 param_hint="--image-quality")

    settings = Settings(image_format, image_quality, audio_format, audio_bitrate or DEFAULT_AUDIO_BITRATE[audio_format],
                        video_codec, crf, preset, strip_metadata)
    exts = {"image": IMAGE_EXTS, "audio": AUDIO_EXTS, "video": VIDEO_EXTS}
    wanted = set().union(*(exts[t] for t in (types or exts)))

    files = run.files(wanted)
    if not files:
        return
    ffmpeg = find("ffmpeg", "Install it from https://ffmpeg.org (Windows: winget install ffmpeg).")
    exiftool = ExifTool(run.verbose)
    markers = {} if recompress else exiftool.read(files, MARKER_TAGS)

    counts = {"compressed": 0, "kept original": 0, "already compressed": 0, "failed": 0}
    if run.dry_run:
        counts = {"would be compressed": 0, "already compressed": 0}

    # Names in use per folder, so photo.png and photo.jpg don't both become photo.jpg.
    taken: dict[Path, set[str]] = {}
    jobs = []  # (original, compressed temp file, final path, kind, times)
    kept = []  # (original, times) for images and videos that didn't get smaller
    total_before = total_after = 0

    for i, src in enumerate(files, 1):
        kind = kind_of(src)
        if is_marked(ExifTool.lookup(markers, src)):
            counts["already compressed"] += 1
            run.detail(f"Skipping {run.show(src)}: already compressed (use --recompress to do it again)")
            continue

        folder = src.parent
        if folder not in taken:
            # Every name that exists now stays reserved, so a file that is skipped, fails or is kept
            # can never be overwritten by another file's compressed version.
            taken[folder] = {p.name.casefold() for p in folder.iterdir()}
        ext = "." + settings.extension(kind)
        if (src.stem + ext).casefold() == src.name.casefold():
            final = src
        else:
            final = folder / (unique_stem(src.stem, taken[folder], ext) + ext)
        temp = folder / f"{TEMP_PREFIX}{uuid.uuid4().hex}{ext}"
        command = [ffmpeg, *settings.ffmpeg_args(kind, src, temp)]

        if run.dry_run:
            counts["would be compressed"] += 1
            run.info(f"Would compress {run.show(src)} -> {final.name}")
            run.detail("    " + subprocess.list2cmdline(command))
            continue

        run.info(f"[{i}/{len(files)}] Compressing {run.show(src)}...")
        run.detail("    " + subprocess.list2cmdline(command))
        times = FileTimes.of(src)
        # Videos show ffmpeg's progress line; other files are quick, so their output is captured.
        result = subprocess.run(command, stdout=subprocess.DEVNULL if kind == "video" else subprocess.PIPE,
                                stderr=None if kind == "video" else subprocess.PIPE)
        if result.returncode != 0 or not temp.exists():
            counts["failed"] += 1
            detail = (result.stderr or b"").decode("utf-8", "replace").strip()
            run.error(f"    Failed{': ' + detail if detail else ''}")
            temp.unlink(missing_ok=True)
            continue

        before, after = src.stat().st_size, temp.stat().st_size
        if after >= before:
            run.info(f"    {_size_change(before, after)} - kept the original")
            temp.unlink()
            counts["kept original"] += 1
            if kind != "audio":
                kept.append((src, times))
            continue

        total_before += before
        total_after += after
        run.info(f"    {_size_change(before, after)}")
        jobs.append((src, temp, final, kind, times))

    if not run.dry_run:
        meta_jobs = [(temp, args) for src, temp, _, kind, _ in jobs
                     if (args := settings.exiftool_args(kind, src)) is not None]
        # Mark files that were kept as they were, so later runs don't try (and fail) to shrink them again.
        meta_jobs += [(src, [history_entry("kept: already well compressed")]) for src, _ in kept]
        if meta_jobs:
            run.info(f"Finishing {len(meta_jobs)} file(s) with exiftool...")
            problems = exiftool.write(meta_jobs)
            for src, temp, final, _, _ in jobs:
                errors = [m for m in problems.get(temp, []) if m.startswith("Error")]
                if errors:
                    run.warn(f"Metadata not copied to {final.name} (and it may be compressed again next time): {errors[0]}")

        for src, times in kept:
            times.apply(src)

        # Swap each original for its compressed version.
        for src, temp, final, _, times in jobs:
            times.apply(temp)
            src.unlink()
            os.replace(temp, final)
            counts["compressed"] += 1

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
