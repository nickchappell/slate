from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

# See "Frame Extraction Strategy" in PROJECT_SPEC.md: attempt ffmpeg first
# (handles H.264/H.265 and regular ProRes), fall back to qlmanage+sips (uses
# AVFoundation/QuickLook plugins, handles ProRes RAW and other manufacturer
# RAW formats ffmpeg can't decode). Detection is attempt, not predict -- run
# the real command and check its result, no static codec allowlist.

EXTRACTION_WIDTH = 896
SUBPROCESS_TIMEOUT = 60

# Frame positions are fractions of clip duration, floored/capped away from
# the true start and end -- a position right at 0% or 100% risks landing on
# a black/fade/empty frame outside the real content. See "Multi-frame input
# for VLM inference" in CLAUDE.md.
MIN_FRAME_POSITION_FRACTION = 0.05
MAX_FRAME_POSITION_FRACTION = 0.9


class ExtractionError(Exception):
    pass


def _frame_position_fractions(num_frames: int) -> list[float]:
    """Evenly spaces `num_frames` positions across [0.0, 1.0], then clamps
    each into [MIN_FRAME_POSITION_FRACTION, MAX_FRAME_POSITION_FRACTION] --
    for the default num_frames=3 this produces [0.05, 0.5, 0.9] exactly
    (only the two endpoints move; the true midpoint is already in range).
    num_frames=1 is a degenerate case: just MIN_FRAME_POSITION_FRACTION."""
    if num_frames <= 1:
        return [MIN_FRAME_POSITION_FRACTION]
    return [
        max(
            MIN_FRAME_POSITION_FRACTION,
            min(i / (num_frames - 1), MAX_FRAME_POSITION_FRACTION),
        )
        for i in range(num_frames)
    ]


def _probe_duration_seconds(source: Path) -> float | None:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(source),
            ],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
    except subprocess.TimeoutExpired, OSError:
        return None
    if result.returncode != 0:
        return None
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def _try_ffmpeg(source: Path, output_jpeg: Path, timestamp_seconds: float) -> bool:
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-ss",
                f"{timestamp_seconds:.3f}",
                "-i",
                str(source),
                "-vframes",
                "1",
                "-vf",
                f"scale={EXTRACTION_WIDTH}:-1",
                str(output_jpeg),
            ],
            capture_output=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
    except subprocess.TimeoutExpired, OSError:
        return False
    return (
        result.returncode == 0
        and output_jpeg.is_file()
        and output_jpeg.stat().st_size > 0
    )


def _try_qlmanage(source: Path, output_jpeg: Path) -> bool:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_dir_path = Path(tmp_dir)
        try:
            result = subprocess.run(
                ["qlmanage", "-t", "-s", "1024", "-o", str(tmp_dir_path), str(source)],
                capture_output=True,
                timeout=SUBPROCESS_TIMEOUT,
            )
        except subprocess.TimeoutExpired, OSError:
            return False

        # qlmanage -t always outputs PNG, regardless of the requested name.
        png_path = tmp_dir_path / f"{source.name}.png"
        if result.returncode != 0 or not png_path.is_file():
            return False

        try:
            sips_result = subprocess.run(
                [
                    "sips",
                    "-s",
                    "format",
                    "jpeg",
                    str(png_path),
                    "--out",
                    str(output_jpeg),
                ],
                capture_output=True,
                timeout=SUBPROCESS_TIMEOUT,
            )
        except subprocess.TimeoutExpired, OSError:
            return False

    return (
        sips_result.returncode == 0
        and output_jpeg.is_file()
        and output_jpeg.stat().st_size > 0
    )


def extract_frames(source: Path, output_dir: Path, num_frames: int) -> list[Path]:
    """Extracts up to `num_frames` frames from `source`, spread across the
    clip per _frame_position_fractions(), into deterministic-per-call names
    (frame_0.jpg, frame_1.jpg, ...) under output_dir. Falls back to a single
    qlmanage/sips-derived frame (RAW formats ffmpeg can't decode, or a
    ffprobe-unreadable duration) -- so the returned list may be shorter than
    `num_frames`. Raises ExtractionError if neither path could produce
    anything; caller is expected to mark the group "error" and move on, not
    halt the batch (see Frame Extraction Strategy, step 3)."""
    output_dir.mkdir(parents=True, exist_ok=True)

    duration = _probe_duration_seconds(source)
    if duration is not None and duration > 0:
        timestamps = [f * duration for f in _frame_position_fractions(num_frames)]
        frame_paths = [output_dir / f"frame_{i}.jpg" for i in range(len(timestamps))]
        if all(
            _try_ffmpeg(source, path, timestamp)
            for path, timestamp in zip(frame_paths, timestamps, strict=True)
        ):
            return frame_paths

    fallback_path = output_dir / "frame_0.jpg"
    if _try_qlmanage(source, fallback_path):
        return [fallback_path]

    raise ExtractionError(
        f"ffmpeg and qlmanage both failed to decode a frame from {source.name}"
    )


def build_montage(frame_paths: list[Path], output_jpeg: Path) -> None:
    """Composites frame_paths into one JPEG for human review in review/ --
    a left-to-right strip when there's more than one frame, a straight copy
    when there's only one (e.g. the qlmanage single-frame fallback). Purely
    a review artifact: the model captions from frame_paths directly, not
    from this file, so this never affects caption quality."""
    output_jpeg.parent.mkdir(parents=True, exist_ok=True)

    if len(frame_paths) == 1:
        shutil.copyfile(frame_paths[0], output_jpeg)
        return

    cmd = ["ffmpeg", "-y"]
    for path in frame_paths:
        cmd += ["-i", str(path)]
    cmd += ["-filter_complex", f"hstack=inputs={len(frame_paths)}", str(output_jpeg)]

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=SUBPROCESS_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as e:
        raise ExtractionError(
            f"ffmpeg failed to build a montage from {len(frame_paths)} frames: {e}"
        ) from e

    if (
        result.returncode != 0
        or not output_jpeg.is_file()
        or output_jpeg.stat().st_size == 0
    ):
        raise ExtractionError(
            f"ffmpeg failed to build a montage from {len(frame_paths)} frames"
        )
