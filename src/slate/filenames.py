from __future__ import annotations

CAPTION_LENGTH_CAP = 70  # characters; see "Caption length cap" in PROJECT_SPEC.md

# A single filename path component can never contain '/' (read as a path
# separator); NUL terminates a C-style string at the OS/syscall level.
_FILESYSTEM_UNSAFE_CHARS = "/\0"


def normalize_caption(raw: str) -> str:
    # See "Caption normalization" in PROJECT_SPEC.md -- applied in this order:
    # 1. Replace filesystem-unsafe characters with a space, before
    #    whitespace collapsing so any space just introduced here is cleaned
    #    up along with everything else.
    text = "".join(
        " " if ch in _FILESYSTEM_UNSAFE_CHARS else ch for ch in raw
    )

    # 2. Collapse whitespace/newlines to single spaces, trim ends.
    text = " ".join(text.split())

    # 3. Strip a single pair of surrounding quote characters, if present.
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()

    # 4. Lowercase.
    text = text.lower()

    # 5. Strip trailing sentence-ending punctuation (trailing only).
    text = text.rstrip(".,!?")

    return text


def truncate_caption(caption: str, max_length: int = CAPTION_LENGTH_CAP) -> str:
    # Cut at the last whitespace boundary at or before max_length, never
    # mid-word. No ellipsis or other marker is appended.
    if len(caption) <= max_length:
        return caption
    truncated = caption[:max_length]
    if " " in truncated:
        truncated = truncated.rsplit(" ", 1)[0]
    return truncated


def assemble_stem(
    *,
    original_stem: str,
    caption: str,
    prefix: str = "",
    suffix: str = "",
    prepend_caption: bool = False,
    max_length: int = 255,
) -> str:
    # See "Filename Assembly" in PROJECT_SPEC.md. On overflow, truncation
    # always shortens `caption` specifically -- never `original_stem`,
    # `prefix`, or `suffix` -- since the caption is the one AI-generated,
    # least-essential segment, regardless of prepend/append position.
    prefix = prefix.strip()
    suffix = suffix.strip()

    def build(caption_text: str) -> str:
        segments = (
            [prefix, caption_text, original_stem, suffix]
            if prepend_caption
            else [prefix, original_stem, caption_text, suffix]
        )
        return " ".join(segment for segment in segments if segment)

    stem = build(caption)
    limit = max_length - 1
    if len(stem) <= limit:
        return stem

    overflow = len(stem) - limit
    caption_budget = max(len(caption) - overflow, 0)
    shortened_caption = truncate_caption(caption, caption_budget) if caption_budget else ""
    stem = build(shortened_caption)

    # truncate_caption backs off to a word boundary (can leave a few
    # characters of slack), or the non-caption portions alone already
    # exceed max_length (see the "Edge case" note in PROJECT_SPEC.md) --
    # either way, hard-cut as the final guarantee of the max_length invariant.
    if len(stem) > limit:
        stem = stem[:limit]
    return stem
