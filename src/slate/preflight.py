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
    ]
    return [message for message in checks if message is not None]
