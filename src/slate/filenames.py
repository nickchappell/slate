from __future__ import annotations


def truncate_stem(stem: str, max_length: int) -> str:
    # Deliberately one character *under* max_length, not equal to it — see
    # "Filename Assembly" in PROJECT_SPEC.md.
    if len(stem) <= max_length:
        return stem
    return stem[: max_length - 1]
