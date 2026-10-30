"""Building the ffmpeg commands that `imfix compress` runs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from imfix import __version__
from imfix.files import AUDIO_EXTS, IMAGE_EXTS, VIDEO_EXTS

AUDIO_CODECS = {"opus": "libopus", "mp3": "libmp3lame", "m4a": "aac"}
DEFAULT_AUDIO_BITRATE = {"opus": "96k", "mp3": "160k", "m4a": "128k"}
VIDEO_CODECS = {"h264": "libx264", "h265": "libx265"}
PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"]

# Compressed files are marked inside the file itself, so imfix can tell they've already been
# compressed (and not do it again) without keeping any record of its own. Images and videos get
# an entry in the standard XMP processing history; audio gets an "imfix" tag written by ffmpeg.
# These are the tags exiftool reports the markers under.
MARKER_TAGS = ["HistorySoftwareAgent", "Imfix", "UserDefinedText"]


def _values(value) -> list:
    return value if isinstance(value, list) else [value]


def is_marked(tags: dict) -> bool:
    """Whether exiftool's `tags` (read with MARKER_TAGS) show the file was compressed by imfix."""
    return (
        any(str(v).startswith("imfix") for v in _values(tags.get("HistorySoftwareAgent")))
        or tags.get("Imfix") == "compressed"                        # Opus/Ogg and M4A
        or "(imfix) compressed" in _values(tags.get("UserDefinedText"))  # MP3
    )


def history_entry(parameters: str = "compressed") -> str:
    """exiftool argument adding an imfix entry to the file's XMP history."""
    when = datetime.now().strftime("%Y:%m:%d %H:%M:%S")
    action = "converted" if parameters == "compressed" else "checked"
    return f"-XMP-xmpMM:History+={{Action={action},SoftwareAgent=imfix {__version__},When={when},Parameters={parameters}}}"


def kind_of(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in VIDEO_EXTS:
        return "video"
    return None


@dataclass
class Settings:
    image_format: str = "jpg"
    image_quality: int = 5
    audio_format: str = "opus"
    audio_bitrate: str = "96k"
    video_codec: str = "h264"
    crf: int = 28
    preset: str = "medium"
    strip_metadata: bool = False

    def extension(self, kind: str) -> str:
        return {"image": self.image_format, "audio": self.audio_format, "video": "mp4"}[kind]

    def ffmpeg_args(self, kind: str, src: Path, dst: Path) -> list[str]:
        """The ffmpeg arguments (after the program name) that compress `src` into `dst`."""
        base = ["-hide_banner", "-loglevel", "error", "-y"]
        if kind == "image":
            if self.image_format == "jpg":
                codec = ["-q:v", str(self.image_quality)]
            else:
                codec = ["-c:v", "libwebp", "-quality", str(self.image_quality)]
            # -noautorotate keeps pixels as stored, so the EXIF orientation copied later still applies.
            # Metadata is copied afterwards by exiftool, which does a far more complete job.
            return [*base, "-noautorotate", "-i", str(src), "-map_metadata", "-1", "-frames:v", "1", *codec, str(dst)]

        if kind == "audio":
            if self.strip_metadata:
                meta = "-1"
            elif src.suffix.lower() in (".ogg", ".opus"):
                meta = "0:s:a:0"  # Ogg keeps its tags on the audio stream, not the file.
            else:
                meta = "0"
            args = [*base, "-i", str(src), "-map", "0:a:0", "-map_metadata", meta, "-metadata", "imfix=compressed"]
            if self.audio_format == "m4a":
                args += ["-movflags", "+use_metadata_tags"]  # Otherwise M4A drops the imfix tag.
            if self.audio_format != "opus" and not self.strip_metadata:
                # Keep cover art. ffmpeg can't put it in Opus files, so it's dropped there.
                args += ["-map", "0:v:0?", "-c:v", "copy", "-disposition:v", "attached_pic"]
            return [*args, "-c:a", AUDIO_CODECS[self.audio_format], "-b:a", self.audio_bitrate, str(dst)]

        # Video: -stats shows progress, as videos can take a while.
        codec = ["-c:v", VIDEO_CODECS[self.video_codec], "-crf", str(self.crf), "-preset", self.preset, "-pix_fmt", "yuv420p"]
        if self.video_codec == "h265":
            codec += ["-tag:v", "hvc1"]  # Needed for H.265 to play on Apple devices.
        meta = "-1" if self.strip_metadata else "0"
        return [
            *base, "-stats", "-i", str(src),
            "-map", "0:v:0", "-map", "0:a?", "-map_metadata", meta,
            *codec, "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart+use_metadata_tags", str(dst),
        ]

    def exiftool_args(self, kind: str, src: Path) -> list[str] | None:
        """exiftool arguments to finish a compressed image or video: copy the original's metadata
        across and add the imfix marker. None for audio, which ffmpeg has already finished."""
        if kind == "audio":
            return None
        copy = [] if self.strip_metadata else ["-TagsFromFile", str(src), "-all:all", "-ICC_Profile"]
        return [*copy, history_entry()]
