from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

# See "Frame Extraction Strategy" in PROJECT_SPEC.md: attempt ffmpeg first
# (handles H.264/H.265 and regular ProRes), fall back to qlmanage+sips (uses
# AVFoundation/QuickLook plugins, handles ProRes RAW and other manufacturer
# RAW formats ffmpeg can't decode). Detection is attempt, not predict -- run
# the real command and check its result, no static codec allowlist.

EXTRACTION_TIMESTAMP = "00:00:01"
EXTRACTION_WIDTH = 896
SUBPROCESS_TIMEOUT = 60


class ExtractionError(Exception):
    pass


def _try_ffmpeg(source: Path, output_jpeg: Path) -> bool:
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", EXTRACTION_TIMESTAMP,
                "-i", str(source),
                "-vframes", "1",
                "-vf", f"scale={EXTRACTION_WIDTH}:-1",
                str(output_jpeg),
            ],
            capture_output=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
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
        except (subprocess.TimeoutExpired, OSError):
            return False

        # qlmanage -t always outputs PNG, regardless of the requested name.
        png_path = tmp_dir_path / f"{source.name}.png"
        if result.returncode != 0 or not png_path.is_file():
            return False

        try:
            sips_result = subprocess.run(
                ["sips", "-s", "format", "jpeg", str(png_path), "--out", str(output_jpeg)],
                capture_output=True,
                timeout=SUBPROCESS_TIMEOUT,
            )
        except (subprocess.TimeoutExpired, OSError):
            return False

    return (
        sips_result.returncode == 0
        and output_jpeg.is_file()
        and output_jpeg.stat().st_size > 0
    )


def extract_frame(source: Path, output_jpeg: Path) -> None:
    """Raises ExtractionError if neither ffmpeg nor qlmanage/sips could
    produce a frame -- caller is expected to mark the group "error" and
    move on, not halt the batch (see Frame Extraction Strategy, step 3)."""
    output_jpeg.parent.mkdir(parents=True, exist_ok=True)

    if _try_ffmpeg(source, output_jpeg):
        return
    if _try_qlmanage(source, output_jpeg):
        return

    raise ExtractionError(
        f"ffmpeg and qlmanage both failed to decode a frame from {source.name}"
    )
