# imfix

Batch-edit image metadata from the command line. A wrapper around [exiftool](https://exiftool.org)
(and ffmpeg for compression) that works on Windows and Linux, including files with non-ASCII names.

## Install

Needs Python 3.10+ and exiftool on the PATH (`winget install OliverBetz.ExifTool` on Windows,
`sudo apt install libimage-exiftool-perl` on Debian/Ubuntu). `imfix compress` also needs ffmpeg
(`winget install ffmpeg`).

```bash
uv tool install .
```

or `pipx install .`. Either gives you an `imfix` command (`imfix.exe` on Windows).

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
| `imfix compress DIR` | Re-encode images as JPEG or WebP, keeping metadata |
| `imfix strip-copyright DIR` | Remove all copyright and rights info |
| `imfix author DIR NAME` | Set the author (`--none` to remove it) |
| `imfix rename DIR` | Rename to `yyyy-mm-dd_hh-mm-ss`, adding `_1`, `_2`... for clashes |

Options on every command:

| Option | |
|---|---|
| `-r`, `--recurse` | Include subfolders |
| `-v`, `--verbose` | Show every file and the commands being run |
| `-n`, `--dry-run` | Show what would happen without changing anything |
| `-o`, `--output DIR` | Where results go. Default: an `imfix` folder inside the input folder |
| `-i`, `--in-place` | Change the originals instead (not available for `compress`) |

By default the originals are never touched: results are written to `DIR/imfix/`, keeping the
subfolder layout when `--recurse` is used. Files that already exist in the output folder are
skipped. File created and modified dates are kept on every copy.

To run several commands one after another, either work on the output folder in place:

```bash
imfix date .
imfix rename ./imfix --in-place
```

or use `--in-place` on the originals throughout.

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

## Development

```bash
uv venv
uv pip install -e ".[test]"
uv run pytest
```
