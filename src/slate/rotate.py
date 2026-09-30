from __future__ import annotations

import json
import shlex
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rich.prompt import Confirm

from slate import output

# See "--rotate: fixing 90-degree camera rotation" in PROJECT_SPEC.md.
#
# The fix is a single change to the video track's `tkhd` display matrix,
# spliced in via Bento4 (`mp4extract`/`mp4edit --replace`) rather than any
# full-file remux. Verified empirically against three real fixtures
# (Final Cut Camera H.265, Kino H.265, Kino ProRes -- the latter two also
# carrying a `mebx` timed-metadata track): decoded video/audio are
# bit-for-bit identical before and after, and every atom this doesn't
# explicitly touch (audio hdlr/stsd, `tmcd`, `mebx` hdlr/stsd/samples, the
# video track's own stsd including an unrecognized vendor `logs` box --
# Apple's "Log" color-profile marker, present on both H.265 fixtures
# regardless of source app, not Final-Cut-Camera-specific) comes through
# byte-for-byte unchanged. GPAC's MP4Box -- the tool an earlier
# AppleScript-based workflow used for this same fix -- was tried against the
# same fixture for comparison and is NOT byte-perfect (it silently
# normalized a non-standard-but-harmless 4 bytes out of the video track's
# stsd during its full remux); Bento4's targeted atom replace never touches
# stsd at all here, so that gap doesn't exist with this approach. No new
# dependency needed -- Bento4 is already required for --add-metadata/
# --metadata-backfill (see preflight.run_metadata_tool_checks()).

SUBPROCESS_TIMEOUT = 60  # matches extraction.py/metadata.py's convention

ORIGINAL_UNROTATED_DIRNAME = "original_unrotated"

# Raw (a, b, u, c, d, v, x, y, w) display matrix values, as the big-endian
# 32-bit fixed-point ints stored in `tkhd` -- all nine treated as 16.16 (the
# spec's last row is technically 2.30, but w is always 1.0 == 0x40000000 for
# a pure rotation with no scale/skew, so 16.16 vs 2.30 never matters here).
_IDENTITY = (65536, 0, 0, 0, 65536, 0, 0, 0, 1073741824)
_ROTATE_90_CW = (0, 65536, 0, -65536, 0, 0, 0, 0, 1073741824)  # ffprobe rotation=-90
_ROTATE_90_CCW = (0, -65536, 0, 65536, 0, 0, 0, 0, 1073741824)  # ffprobe rotation=90
_ROTATE_180 = (-65536, 0, 0, 0, -65536, 0, 0, 0, 1073741824)  # ffprobe rotation=+/-180

_MATRIX_LABELS = {
    _IDENTITY: "none",
    _ROTATE_90_CW: "-90",
    _ROTATE_90_CCW: "90",
    _ROTATE_180: "180",
}

# The only state --rotate corrects today (matches the 3 real fixtures this
# was built and verified against). A file at any other rotation -- 0/90/180,
# an unrecognized matrix, or "couldn't be determined" -- is left alone.
# Auto-detecting/normalizing every possible rotation state is a deliberate
# future improvement (see PROJECT_SPEC.md), not attempted here.
NEEDS_FIX_LABEL = "-90"


@dataclass
class RotationState:
    trak_index: int
    matrix: tuple[int, ...]
    label: str  # "none" | "-90" | "90" | "180" | "other"


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            cmd, capture_output=True, timeout=SUBPROCESS_TIMEOUT, **kwargs
        )
    except subprocess.TimeoutExpired, OSError:
        return None


def _find_video_trak_index(path: Path) -> int | None:
    result = _run(
        ["mp4dump", "--format", "json", "--verbosity", "1", str(path)], text=True
    )
    if result is None or result.returncode != 0:
        return None
    try:
        boxes = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    moov = next((b for b in boxes if b.get("name") == "moov"), None)
    if moov is None:
        return None
    traks = [c for c in moov.get("children", []) if c.get("name") == "trak"]

    for i, trak in enumerate(traks):
        node = trak
        for name in ("mdia", "hdlr"):
            node = next(
                (c for c in node.get("children", []) if c.get("name") == name), None
            )
            if node is None:
                break
        if node is not None and node.get("handler_type") == "vide":
            return i
    return None


def _tkhd_matrix_offset(version: int) -> int | None:
    # ISO/IEC 14496-12 tkhd layout, matrix offset from the start of the box
    # (including its 8-byte size+type header). Confirmed empirically against
    # real files for version 0 (offset 48); version 1's offset (60) follows
    # the same spec but hasn't been exercised against a real fixture -- no
    # test file with 64-bit tkhd timestamps has turned up yet.
    if version == 0:
        return 48
    if version == 1:
        return 60
    return None


def _extract_atom(path: Path, atom_path: str) -> Path | None:
    with tempfile.NamedTemporaryFile(
        prefix="slate-rotate-atom-", suffix=".bin", delete=False
    ) as f:
        out_path = Path(f.name)
    result = _run(["mp4extract", atom_path, str(path), str(out_path)])
    if result is None or result.returncode != 0 or out_path.stat().st_size == 0:
        out_path.unlink(missing_ok=True)
        return None
    return out_path


def read_rotation_state(path: Path) -> RotationState | None:
    """Returns None whenever the state can't be determined -- no video
    track found, Bento4 not installed, or a `tkhd` shape this doesn't know
    how to parse -- never raises. Callers treat None as "leave this file
    alone," not as an error, so a machine without Bento4 sees exactly the
    same captioning/renaming behavior as before --rotate existed."""
    trak_index = _find_video_trak_index(path)
    if trak_index is None:
        return None

    atom_file = _extract_atom(path, f"moov/trak[{trak_index}]/tkhd")
    if atom_file is None:
        return None
    try:
        data = atom_file.read_bytes()
    finally:
        atom_file.unlink(missing_ok=True)

    if len(data) < 9:
        return None
    _size, boxtype, version = struct.unpack(">I4sB", data[0:9])
    if boxtype != b"tkhd":
        return None

    matrix_offset = _tkhd_matrix_offset(version)
    if matrix_offset is None or len(data) < matrix_offset + 36:
        return None

    matrix = struct.unpack(">9i", data[matrix_offset : matrix_offset + 36])
    label = _MATRIX_LABELS.get(matrix, "other")
    return RotationState(trak_index=trak_index, matrix=matrix, label=label)


def rotate_to_identity(path: Path, trak_index: int, output_path: Path) -> bool:
    """Writes a copy of `path` with trak[trak_index]'s tkhd matrix reset to
    identity to `output_path` (must not already exist -- caller picks a
    fresh temp path). Every other atom is left untouched by construction --
    mp4edit --replace only ever rewrites the one atom path given. Returns
    False (output_path left as whatever mp4edit did or didn't write) on any
    failure; the caller is responsible for cleanup in that case."""
    atom_file = _extract_atom(path, f"moov/trak[{trak_index}]/tkhd")
    if atom_file is None:
        return False

    try:
        data = bytearray(atom_file.read_bytes())
        if len(data) < 9:
            return False
        _size, boxtype, version = struct.unpack(">I4sB", data[0:9])
        if boxtype != b"tkhd":
            return False
        matrix_offset = _tkhd_matrix_offset(version)
        if matrix_offset is None or len(data) < matrix_offset + 36:
            return False

        data[matrix_offset : matrix_offset + 36] = struct.pack(">9i", *_IDENTITY)
        atom_file.write_bytes(data)

        result = _run(
            [
                "mp4edit",
                "--replace",
                f"moov/trak[{trak_index}]/tkhd:{atom_file}",
                str(path),
                str(output_path),
            ]
        )
        if (
            result is None
            or result.returncode != 0
            or not output_path.is_file()
            or output_path.stat().st_size == 0
        ):
            return False
        return True
    finally:
        atom_file.unlink(missing_ok=True)


@dataclass
class RotateResult:
    path: Path
    status: str  # "rotated" | "would_rotate" | "skipped" | "error"
    message: str
    preserved_path: Path | None = None  # set only when status == "rotated"


def rotate_file(path: Path, *, dry_run: bool) -> RotateResult:
    """Processes exactly one file -- see run_rotate() below for the
    per-batch loop/summary this backs."""
    if not path.is_file():
        return RotateResult(path=path, status="error", message="file not found")

    state = read_rotation_state(path)
    if state is None:
        return RotateResult(
            path=path,
            status="error",
            message=(
                "could not read the video track's rotation matrix -- is "
                "Bento4 installed (brew install bento4), and is this a "
                "valid .mov/.mp4?"
            ),
        )

    if state.label != NEEDS_FIX_LABEL:
        description = {
            "none": "no rotation (already landscape)",
            "90": "a 90-degree counter-clockwise matrix",
            "180": "a 180-degree matrix",
            "other": "a non-standard rotation matrix",
        }[state.label]
        message = (
            f"current rotation is {description}, not the 90-degree "
            "clockwise state --rotate corrects today -- skipping"
        )
        return RotateResult(
            path=path, status="skipped" if dry_run else "error", message=message
        )

    if dry_run:
        return RotateResult(
            path=path,
            status="would_rotate",
            message=(
                "rotation matrix is 90-degree clockwise -- would reset it "
                f"to identity and move the original into "
                f"{ORIGINAL_UNROTATED_DIRNAME}/"
            ),
        )

    original_dir = path.parent / ORIGINAL_UNROTATED_DIRNAME
    preserved_path = original_dir / path.name
    if preserved_path.exists():
        return RotateResult(
            path=path,
            status="error",
            message=(
                f"{original_dir.name}/{path.name} already exists -- "
                "refusing to overwrite a previously preserved original"
            ),
        )

    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=".slate-rotate-", suffix=path.suffix, delete=False
    ) as f:
        tmp_output = Path(f.name)
    try:
        if not rotate_to_identity(path, state.trak_index, tmp_output):
            return RotateResult(
                path=path,
                status="error",
                message="mp4edit failed to write the corrected file",
            )

        verify = read_rotation_state(tmp_output)
        if verify is None or verify.label != "none":
            return RotateResult(
                path=path,
                status="error",
                message=(
                    "corrected file failed verification (rotation matrix "
                    "wasn't identity afterward) -- original left untouched"
                ),
            )

        original_dir.mkdir(parents=True, exist_ok=True)
        path.rename(preserved_path)
        tmp_output.replace(path)
        tmp_output = None
        return RotateResult(
            path=path,
            status="rotated",
            message=f"rotated; original preserved as {original_dir.name}/{path.name}",
            preserved_path=preserved_path,
        )
    finally:
        if tmp_output is not None:
            tmp_output.unlink(missing_ok=True)


def write_undo_script(results: list[RotateResult], path: Path) -> None:
    """Undoing --rotate never needs ffmpeg/Bento4 -- the original is sitting
    right there, untouched, in original_unrotated/. Reversing it is two
    plain filesystem moves per file, mirroring rename.py's write_undo_script
    (same `mv -n --`/chmod +x shape, no ffmpeg/exiftool/Bento4 involved
    there either, for the same reason: the original bytes were never
    discarded, just filed elsewhere).

    Two `mv` lines per file, not one: the destination (the corrected file's
    path) is currently occupied by the corrected file itself, so a single
    `mv -n -- <preserved> <path>` would refuse (no-clobber) rather than
    restore anything. The corrected file is moved aside to
    `<name>.rotated-backup` first (safe to delete by hand afterward --
    left, not removed, since it's otherwise harmless and this script
    should never delete data on its own), then the true original is moved
    back into place.

    Each pair is preceded by an explicit `[ -f <preserved> ]` guard,
    rather than just letting a missing file surface as `mv`'s own "No such
    file or directory" -- original_unrotated/ (or just this one file in
    it) can legitimately be gone by the time this script runs: deleted by
    hand, already restored by an earlier run of this same script, or the
    whole folder cleaned up once the rotation was trusted. `set -euo
    pipefail` would already stop the script there either way; the guard
    exists to name the actual cause up front instead of a bare `mv`
    error, and to fail *before* the first `mv` of that pair runs, so a
    missing original never leaves the corrected file already renamed
    aside with nothing to restore in its place."""
    lines = ["#!/usr/bin/env bash", "set -euo pipefail", ""]

    for result in results:
        if result.status != "rotated" or result.preserved_path is None:
            continue
        current = shlex.quote(str(result.path))
        backup = shlex.quote(f"{result.path}.rotated-backup")
        preserved = shlex.quote(str(result.preserved_path))
        lines.append(
            f"[ -f {preserved} ] || {{ "
            f'echo "ERROR: {result.preserved_path} not found -- was '
            f"original_unrotated/ deleted, or has this rotation already "
            f'been undone?" >&2; exit 1; }}'
        )
        lines.append(f"mv -n -- {current} {backup}")
        lines.append(f"mv -n -- {preserved} {current}")

    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o755)


def _print_result(result: RotateResult) -> None:
    if result.status == "rotated":
        output.ok(f"{result.path.name}: {result.message}")
    elif result.status == "would_rotate":
        output.info(f"{result.path.name}: {result.message}")
    elif result.status == "skipped":
        output.skip(f"{result.path.name}: {result.message}")
    else:
        output.error(f"{result.path.name}: {result.message}")


def _print_summary(
    results: list[RotateResult], *, rotated_label: str, rotated_count: int
) -> None:
    skipped = sum(1 for r in results if r.status == "skipped")
    errors = sum(1 for r in results if r.status == "error")

    output.console.print("\n[bold]Summary:[/bold]")
    output.console.print(f"  {len(results)} file(s) checked")
    output.console.print(f"  [green]{rotated_count}[/green] {rotated_label}")
    output.console.print(
        f"  [cyan]{skipped}[/cyan] skipped "
        "(current existing rotation was not 90 degrees clockwise)"
    )
    error_color = "bold red" if errors else "dim"
    output.console.print(f"  [{error_color}]{errors} error[/{error_color}]")


def run_rotate(
    files: list[Path],
    *,
    dry_run: bool,
    generate_undo_script: bool = True,
    assume_yes: bool = False,
) -> list[RotateResult]:
    """Processes each file independently -- same one-file-at-a-time-with-a-
    status-line shape as run_phase1's captioning loop in cli.py, just
    without the grouping/pairing step (a rotation matrix is a
    per-physical-file container property, so there's no MOV/MP4 pairing
    concept here).

    Always previews first, exactly like `--dry-run` itself would report --
    every file gets a dry-run check (and its own status line) before
    anything is written, so what's about to happen is visible before the
    confirmation prompt, not just described in the prompt's own count. When
    `dry_run` is True, that preview *is* the whole run (mirrors --dry-run's
    existing report-only behavior, unchanged from before this confirmation
    step existed). Otherwise, confirms (unless `assume_yes`), then re-runs
    `rotate_file()` for real on just the files the preview marked
    `would_rotate` -- a `skipped`/`error` preview result can't change
    between the two passes without the file itself changing on disk in
    between, so there's nothing to gain from re-checking it."""
    preview = [rotate_file(path, dry_run=True) for path in files]
    for result in preview:
        _print_result(result)

    if dry_run:
        _print_summary(
            preview,
            rotated_label="would be rotated",
            rotated_count=sum(1 for r in preview if r.status == "would_rotate"),
        )
        return preview

    to_rotate = [r for r in preview if r.status == "would_rotate"]
    if not to_rotate:
        output.info("Nothing to rotate.")
        return preview

    if not assume_yes:
        message = f"{len(to_rotate)} file(s) to rotate"
        problems = len(preview) - len(to_rotate)
        if problems:
            message += f", {problems} issue(s) reported above"
        if not Confirm.ask(message, default=False):
            output.warn("Aborted -- no files rotated.")
            return preview

    to_rotate_paths = {r.path for r in to_rotate}
    results: list[RotateResult] = []
    for preview_result in preview:
        if preview_result.path not in to_rotate_paths:
            results.append(preview_result)
            continue
        real_result = rotate_file(preview_result.path, dry_run=False)
        results.append(real_result)
        _print_result(real_result)

    rotated = sum(1 for r in results if r.status == "rotated")
    _print_summary(results, rotated_label="rotated", rotated_count=rotated)

    if generate_undo_script and rotated:
        timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        undo_path = Path.cwd() / f"undo_rotations_{timestamp}.sh"
        write_undo_script(results, undo_path)
        output.info(f"Undo script written: {undo_path.name}")
        output.console.print(f"  Run with: [cyan]./{undo_path.name}[/cyan]")

    return results
