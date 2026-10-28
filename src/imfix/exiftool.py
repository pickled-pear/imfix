"""A thin wrapper around the exiftool command.

Every call starts exiftool once for the whole batch, because starting it once per file is very
slow on Windows. All file names and tag values are passed through stdin as a UTF-8 argument
file ("-@ -") together with "-charset filename=utf8". Windows command lines mangle non-ASCII
text, so this is what makes names like "Zoë's café.jpg" work.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import click

# Printed to stderr by exiftool after each file, so errors can be matched to the file they belong to.
_MARKER = "@@imfix-done@@"

# Options used for every write. -P keeps the file modified date, -m ignores minor problems
# (such as a tag that a format can't hold), and the copies/originals are overwritten without
# leaving "_original" backups next to them.
_WRITE_OPTIONS = ["-charset", "filename=utf8", "-overwrite_original", "-P", "-m"]


def find(name: str, install_hint: str) -> str:
    exe = shutil.which(name)
    if exe is None:
        raise click.ClickException(f"{name} was not found on the PATH. {install_hint}")
    return exe


def _key(path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _check_line(value: str) -> str:
    # The argument file has one argument per line, so a line break would split an argument in two.
    if "\n" in value or "\r" in value:
        raise click.ClickException(f"Values can't contain line breaks: {value!r}")
    return value


class ExifTool:
    def __init__(self, verbose: bool = False):
        self.exe = find("exiftool", "Install it from https://exiftool.org (Windows: winget install OliverBetz.ExifTool).")
        self.verbose = verbose

    def _run(self, command: list[str], lines: list[str]) -> subprocess.CompletedProcess:
        argfile = "".join(_check_line(str(line)) + "\n" for line in lines)
        if self.verbose:
            click.echo(click.style(f"  exiftool {' '.join(command)}  ({len(lines)} argument lines)", dim=True))
        return subprocess.run(
            [self.exe, *command],
            input=argfile.encode("utf-8"),
            capture_output=True,
        )

    def read(self, paths: list[Path], tags: list[str]) -> dict[str, dict]:
        """Read `tags` from every file. Returns {normalised path: {tag: value}}."""
        if not paths:
            return {}
        lines = ["-json", *(f"-{tag}" for tag in tags), *map(str, paths)]
        proc = self._run(["-charset", "filename=utf8", "-@", "-"], lines)
        stdout = proc.stdout.decode("utf-8", "replace").strip()
        try:
            records = json.loads(stdout) if stdout else []
        except json.JSONDecodeError:
            raise click.ClickException(
                "Could not read exiftool output:\n" + proc.stderr.decode("utf-8", "replace")
            )
        return {_key(r["SourceFile"]): r for r in records}

    @staticmethod
    def lookup(results: dict[str, dict], path: Path) -> dict:
        return results.get(_key(path), {})

    def write(self, jobs: list[tuple[Path, list[str]]]) -> dict[Path, list[str]]:
        """Apply each job's exiftool arguments to its file.

        Returns the problems per file: {path: [messages]}. A file whose messages include one
        starting with "Error" was not changed.
        """
        if not jobs:
            return {}

        lines: list[str] = []
        for i, (path, args) in enumerate(jobs):
            if i:
                lines.append("-execute")
            lines += [*args, "-echo4", f"{_MARKER}{i}", str(path)]

        proc = self._run(["-@", "-", "-common_args", *_WRITE_OPTIONS], lines)

        problems: dict[Path, list[str]] = {}
        pending: list[str] = []
        for line in proc.stderr.decode("utf-8", "replace").splitlines():
            if line.startswith(_MARKER):
                index = int(line[len(_MARKER):])
                if pending:
                    problems[jobs[index][0]] = pending
                pending = []
            elif line.strip():
                pending.append(line.strip())

        if pending:
            # Output after the last marker means exiftool itself failed rather than one file.
            raise click.ClickException("exiftool failed:\n" + "\n".join(pending))

        return problems
