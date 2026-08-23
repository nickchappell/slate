from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

# See "MOV/MP4 Pairing Logic" in PROJECT_SPEC.md.

VIDEO_EXTENSIONS = {".mov", ".mp4"}
DURATION_TOLERANCE_SECONDS = 0.1
FRAME_COUNT_TOLERANCE = 1


@dataclass
class FileGroup:
    files: list[Path]  # sorted, stable order
    status: str  # "ok" or "error"
    error: str | None = None
    warning: str | None = None
    source_file: Path | None = (
        None  # selected file for captioning; None if status == "error"
    )

    @property
    def original_files(self) -> list[str]:
        return [f.name for f in self.files]


def discover_input_dir(input_dir: Path) -> list[Path]:
    # Flat listing, not recursive -- matches mappings.json's bare-filename
    # schema, which assumes one flat directory per camera dump/import.
    return sorted(
        p
        for p in input_dir.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
    )


def group_by_stem(files: list[Path]) -> list[list[Path]]:
    groups: dict[str, list[Path]] = {}
    for f in files:
        groups.setdefault(f.stem, []).append(f)
    return [sorted(v) for _, v in sorted(groups.items())]


def _ffprobe_stream_info(path: Path) -> dict | None:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired, OSError:
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _extract_duration_and_frames(info: dict) -> tuple[float | None, int | None]:
    duration: float | None = None
    frames: int | None = None

    fmt = info.get("format", {})
    if "duration" in fmt:
        try:
            duration = float(fmt["duration"])
        except TypeError, ValueError:
            duration = None

    for stream in info.get("streams", []):
        if stream.get("codec_type") != "video":
            continue
        if duration is None and "duration" in stream:
            try:
                duration = float(stream["duration"])
            except TypeError, ValueError:
                pass
        if "nb_frames" in stream:
            try:
                frames = int(stream["nb_frames"])
            except TypeError, ValueError:
                pass
        break

    return duration, frames


def verify_pair(file_a: Path, file_b: Path) -> tuple[bool, str | None]:
    """Returns (matches, warning_or_error_message).

    True + a message means "trust the filename match, but here's why we
    couldn't independently verify it" (ffprobe read failure). False always
    comes with a message naming the specific mismatch.
    """
    info_a = _ffprobe_stream_info(file_a)
    info_b = _ffprobe_stream_info(file_b)

    if info_a is None or info_b is None:
        unreadable = file_a.name if info_a is None else file_b.name
        return (
            True,
            f"ffprobe could not read stream info from {unreadable}; "
            "trusting filename match",
        )

    dur_a, frames_a = _extract_duration_and_frames(info_a)
    dur_b, frames_b = _extract_duration_and_frames(info_b)

    if dur_a is not None and dur_b is not None:
        if abs(dur_a - dur_b) <= DURATION_TOLERANCE_SECONDS:
            return True, None
        if (
            frames_a is not None
            and frames_b is not None
            and abs(frames_a - frames_b) <= FRAME_COUNT_TOLERANCE
        ):
            return True, None
        return (
            False,
            f"same-stem files {file_a.name}/{file_b.name} do not appear to be "
            f"the same recording -- durations differ by {abs(dur_a - dur_b):.1f}s",
        )

    if frames_a is not None and frames_b is not None:
        if abs(frames_a - frames_b) <= FRAME_COUNT_TOLERANCE:
            return True, None
        return (
            False,
            f"same-stem files {file_a.name}/{file_b.name} do not appear to be "
            f"the same recording -- frame counts differ ({frames_a} vs {frames_b})",
        )

    return (
        True,
        f"could not read duration/frame count for {file_a.name}/{file_b.name}; "
        "trusting filename match",
    )


def build_group(files: list[Path]) -> FileGroup:
    if len(files) == 1:
        return FileGroup(files=files, status="ok", source_file=files[0])

    if len(files) == 2:
        matches, message = verify_pair(files[0], files[1])
        if not matches:
            return FileGroup(files=files, status="error", error=message)
        source = min(files, key=lambda p: p.stat().st_size)
        return FileGroup(files=files, status="ok", source_file=source, warning=message)

    # Not a documented case (camera writes exactly one MOV + one MP4 per
    # recording) -- surfaced as an error rather than guessing which files
    # actually belong together.
    names = ", ".join(f.name for f in files)
    return FileGroup(
        files=files,
        status="error",
        error=f"{len(files)} files share this stem, expected 1 or 2: {names}",
    )


def build_groups(files: list[Path]) -> list[FileGroup]:
    return [build_group(group) for group in group_by_stem(files)]
