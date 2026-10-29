# imfix

Batch-edit photo metadata and compress media from the command line. A wrapper around
[exiftool](https://exiftool.org) and [ffmpeg](https://ffmpeg.org) that works on Windows and Linux,
including files with non-ASCII names.

## Install

Needs Python 3.10+ and exiftool on the PATH (`winget install OliverBetz.ExifTool` on Windows,
`sudo apt install libimage-exiftool-perl` on Debian/Ubuntu). `imfix compress` also needs ffmpeg
(`winget install Gyan.FFmpeg`, or `sudo apt install ffmpeg`).

```bash
pipx install git+https://github.com/pickled-pear/imfix
```

or `uv tool install git+https://github.com/pickled-pear/imfix`. Either gives you an `imfix`
command (`imfix.exe` on Windows). Upgrade later with `pipx upgrade imfix` (or `uv tool upgrade imfix`).

## Usage

```
imfix COMMAND DIR [OPTIONS]
```

Run `imfix` on its own for help, and `imfix COMMAND --help` for help on one command.
Use `.` as `DIR` for the current folder.

| Command | What it does |
|---|---|
| `imfix tag DIR TAG...` | Add tags (XMP + IPTC keywords) to every image |
| `imfix date DIR` | Set the date taken from the filename, for images that have none |
| `imfix rename DIR` | Rename images and videos to `yyyy-mm-dd_hh-mm-ss` (`--prefix trip` gives `trip_yyyy-...`) |
| `imfix compress DIR` | Compress images (JPEG/WebP), audio (Opus/MP3/M4A) and video (H.264/H.265 MP4) |
| `imfix copyright DIR --none` | Remove all copyright info (or give text to set it) |
| `imfix author DIR NAME` | Set the author (`--none` to remove it) |

Options on every command:

| Option | |
|---|---|
| `-r`, `--recurse` | Include subfolders |
| `-v`, `--verbose` | Show every file and the commands being run |
| `-n`, `--dry-run` | Show what would happen without changing anything |
| `-o`, `--output FOLDER` | Use another folder as the working copy. Default: an `imfix` folder inside `DIR` |
| `--fresh` | Delete the working copy and copy the originals again |
| `-i`, `--in-place` | Change the originals directly, without a working copy (not available for `compress`) |

## Running several commands: the working copy

imfix never changes your originals unless you ask it to. Instead, all commands on a folder share
one **working copy**:

1. The first command you run on `DIR` copies every image, audio and video file in it into
   `DIR/imfix/`, then changes those copies.
2. Every later command on the same `DIR` sees that `imfix/` already exists and changes the copies
   in it, so each command builds on the one before.

```bash
imfix date .                     # copies the files into ./imfix, then sets missing dates
imfix rename . --prefix holiday  # renames the dated copies
imfix compress .                 # compresses the renamed copies
imfix tag . holiday              # tags the compressed copies
```

Each command prints `Working on the copies in ...` when it is using an existing working copy.

Things to know:

- **Starting again.** `--fresh` on any command deletes the working copy (and every change made
  to it) and copies the originals again before running. Deleting the `imfix` folder yourself
  does the same.
- **New files.** Files added to `DIR` after the working copy was made aren't copied
  automatically. imfix warns you when it spots them; use `--fresh` to include them.
- **Subfolders.** `--recurse` on the *first* command decides whether subfolders are copied
  (keeping their layout). On later commands it decides whether the copies in subfolders are
  changed, so use it on every command if you want subfolders included.
- **A different folder.** `-o FOLDER` uses another folder as the working copy. Give the same
  `-o` on every command. imfix won't use or delete a folder that already holds files it didn't
  put there.
- **Changing the originals.** `--in-place` skips the working copy and changes the files in `DIR`
  directly. `compress` doesn't offer it, as it replaces files.
- **Previewing.** `--dry-run` shows what a command would do without copying or changing anything.
- **Compressing twice.** `compress` remembers which copies it has already compressed (in
  `imfix/.imfix.json`, which follows renames), so running it again skips them rather than
  re-encoding and losing quality. Use `--recompress` to force it. If a file comes out bigger
  than before, the original is kept.

## Dates from filenames

`imfix date` looks for a date (`20230514`, `2023-05-14`, `2023_05_14`, `2023.05.14`) followed by an
optional time. A run of 6 to 9 digits after the date is read as `HHMMSS` plus optional
milliseconds. A longer run, or one that isn't a real time, is treated as a random id and the time
is set to midnight:

| Filename | Date set |
|---|---|
| `IMG_20230514_142301.jpg` | 2023-05-14 14:23:01 |
| `PXL_20230514_142301123.jpg` | 2023-05-14 14:23:01 |
| `ABC_20250101_0935790135.jpg` | 2025-01-01 00:00:00 |
| `2023-05-14 14.23.01.png` | 2023-05-14 14:23:01 |
| `Screenshot_2023-05-14-14-23-01.png` | 2023-05-14 14:23:01 |
| `IMG-20230514-WA0001.jpg` | 2023-05-14 00:00:00 |

## Compression

| Type | Inputs | Output | Main options |
|---|---|---|---|
| Images | jpg jpeg png bmp tif tiff webp | JPEG or WebP | `--image-format`, `--image-quality` |
| Audio | mp3 wav flac m4a aac ogg opus wma aiff aif | Opus, MP3 or M4A | `--audio-format`, `--audio-bitrate` |
| Video | mp4 mov m4v mkv avi wmv webm flv 3gp | MP4 | `--video-codec`, `--crf`, `--preset` |

Use `-t image`, `-t audio` or `-t video` (more than once if needed) to compress only some types.
Metadata (date taken, camera, GPS, tags, audio tags and cover art) and file dates are kept
unless `--strip-metadata` is given. Opus files can't hold cover art, so it's dropped there.

Video encoding is slow compared with the rest. H.264 at the default `--crf 28` typically
shrinks phone videos a lot with little visible loss; `--video-codec h265` is smaller again at the
same quality but slower to encode. HDR videos (such as recent iPhone footage) are converted to
standard 8-bit video, so their colours may look flatter.

## Development

```bash
uv venv
uv pip install -e ".[test]"
uv run pytest
```
