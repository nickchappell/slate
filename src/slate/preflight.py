from __future__ import annotations

import platform
import shutil

# See "Preflight Checks" in PROJECT_SPEC.md -- run once at the start of every
# invocation, before any inference or renaming. All checks run regardless of
# earlier failures, so every problem is reported in one pass.


def _check_macos() -> str | None:
    if platform.system() != "Darwin":
        return f"slate requires macOS; detected platform: {platform.system()}"
    return None


def _check_apple_silicon() -> str | None:
    if platform.machine() != "arm64":
        return "slate requires Apple Silicon; MLX does not support Intel Macs."
    return None


def _check_binary(name: str, hint: str) -> str | None:
    if shutil.which(name) is None:
        return f"required tool '{name}' not found on PATH. {hint}"
    return None


_BENTO4_HINT = "Install it with: brew install bento4"


def run_metadata_tool_checks() -> list[str]:
    """Required only when --add-metadata or --metadata-backfill is active
    -- unlike run_preflight_checks() below, callers must gate this on
    those flags rather than running it unconditionally. Bento4
    (mp4dump/mp4extract/mp4edit) backs the mebx-track repair
    (metadata.py's _repair_mislabeled_data_tracks) that
    _ffmpeg_write_keys_family()'s fallback remux depends on to avoid
    permanently mislabeling a vendor timed-metadata track (e.g. Kino's GPS
    mebx track). This used to be an advisory, skippable dependency, but
    the damage it prevents can't be fixed by a later re-run once it
    happens: the fallback remux overwrites the file, and the correct
    hdlr/stsd atoms the repair needs only exist in the pre-remux bytes,
    which are gone by the time a later invocation could try again -- see
    spec/metadata-write-corruption.md. Hence a hard requirement, same
    severity as run_preflight_checks()'s binaries, just conditional on
    these two flags instead of unconditional."""
    checks = [
        _check_binary("mp4dump", _BENTO4_HINT),
        _check_binary("mp4extract", _BENTO4_HINT),
        _check_binary("mp4edit", _BENTO4_HINT),
    ]
    return [message for message in checks if message is not None]


def run_preflight_checks() -> list[str]:
    checks = [
        _check_macos(),
        _check_apple_silicon(),
        _check_binary("ffmpeg", "Install it with: brew install ffmpeg"),
        _check_binary(
            "ffprobe", "Ships alongside ffmpeg -- install with: brew install ffmpeg"
        ),
        _check_binary(
            "qlmanage",
            "This is a standard macOS system binary; its absence suggests an "
            "unusual environment (minimal/managed image, stripped-down runner).",
        ),
        _check_binary(
            "sips",
            "This is a standard macOS system binary; its absence suggests an "
            "unusual environment (minimal/managed image, stripped-down runner).",
        ),
        _check_binary("exiftool", "Install it with: brew install exiftool"),
    ]
    return [message for message in checks if message is not None]
