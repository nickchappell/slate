from __future__ import annotations

import re
import string
from dataclasses import dataclass
from functools import lru_cache

# mlx_vlm (and its transformers/mlx.core dependency tree) costs ~0.9s to
# import -- see "Startup Time" in PROJECT_SPEC.md. Nothing at module scope
# needs it, and several code paths never caption at all (--version, --help,
# --rename-only, a failed preflight), so the heavy imports are deferred:
# _ensure_*_deps() populates the module-level names below on first use, and
# every function that needs them calls it first. huggingface_hub gets the
# same treatment (it's the cheaper piece, but still ~50ms and only needed
# once we're actually resolving a model).

# See "Model / Inference" -> "Caption prompt" in PROJECT_SPEC.md: word-count
# instructions alone don't reliably bound output length, so a fixed
# generation-time token cap is the actual backstop, independent of prompt
# wording. Not currently a config option.
MAX_CAPTION_TOKENS = 25

# Budget for the --add-metadata/--metadata-backfill SHORT/LONG/KEYWORDS (or
# LONG/KEYWORDS) prompt -- see "Caption generation" in
# spec/metadata-embedding.md. 140 is a reasoned estimate (SHORT ~10 tokens +
# LONG ~40 + KEYWORDS ~40 + label/formatting overhead), not a measured
# value -- needs empirical tuning once real footage/model access is
# available; tune against tests/fixtures/footage/ before treating this as
# final.
MAX_CAPTION_TOKENS_WITH_METADATA = 140

# Mirrors mlx_vlm.utils.get_model_path's default allow_patterns. Kept in
# sync by hand since mlx_vlm doesn't export this list as a public constant --
# used here so our own snapshot_download call fetches the same file set
# mlx_vlm's internal resolution would have.
_MODEL_ALLOW_PATTERNS = [
    "*.json",
    "*.jsonl",
    "*.safetensors",
    "*.py",
    "*.model",
    "*.tiktoken",
    "*.txt",
    "*.jinja",
]

# Lazily populated by _ensure_hub_deps() / _ensure_mlx_deps(). Declared here
# (rather than imported inside each function) so tests can monkeypatch them
# and so the loaders stay a cheap idempotent check on the happy path.
snapshot_download = None
LocalEntryNotFoundError = None
vlm_load = None
vlm_generate = None
apply_chat_template = None
load_config = None


def _ensure_hub_deps() -> None:
    global snapshot_download, LocalEntryNotFoundError
    if snapshot_download is None:
        from huggingface_hub import snapshot_download as _snapshot_download

        snapshot_download = _snapshot_download
    if LocalEntryNotFoundError is None:
        from huggingface_hub.errors import (
            LocalEntryNotFoundError as _LocalEntryNotFoundError,
        )

        LocalEntryNotFoundError = _LocalEntryNotFoundError


def _ensure_mlx_deps() -> None:
    global vlm_load, vlm_generate, apply_chat_template, load_config
    if vlm_load is not None:
        return
    from mlx_vlm import generate as _vlm_generate
    from mlx_vlm import load as _vlm_load
    from mlx_vlm.prompt_utils import apply_chat_template as _apply_chat_template
    from mlx_vlm.utils import load_config as _load_config

    vlm_load = _vlm_load
    vlm_generate = _vlm_generate
    apply_chat_template = _apply_chat_template
    load_config = _load_config


def _resolve_model_path(model_repo: str, *, check_for_updates: bool) -> str:
    _ensure_hub_deps()

    # huggingface_hub's default snapshot_download() hits the Hub on every
    # call to check for a newer revision, even when the model is already
    # fully cached locally -- see "Model Caching" in PROJECT_SPEC.md. Check
    # the local cache first and use it as-is with no network round trip;
    # only fall through to a real (network) resolution if it isn't cached
    # yet, or if the caller explicitly asked to check for updates.
    if not check_for_updates:
        try:
            return snapshot_download(
                repo_id=model_repo,
                local_files_only=True,
                allow_patterns=_MODEL_ALLOW_PATTERNS,
            )
        except LocalEntryNotFoundError:
            pass  # not cached yet -- fall through to a real download

    return snapshot_download(
        repo_id=model_repo,
        local_files_only=False,
        allow_patterns=_MODEL_ALLOW_PATTERNS,
    )


def check_for_model_updates(model_repo: str) -> tuple[bool, str]:
    """Explicitly checks the Hub for a newer revision of `model_repo`,
    downloading it if one exists. Returns (updated, local_path) -- `updated`
    is True if this was a first-time download or a newer snapshot was
    fetched, False if the cache was already current."""
    _ensure_hub_deps()

    try:
        before_path = snapshot_download(
            repo_id=model_repo,
            local_files_only=True,
            allow_patterns=_MODEL_ALLOW_PATTERNS,
        )
    except LocalEntryNotFoundError:
        before_path = None

    after_path = _resolve_model_path(model_repo, check_for_updates=True)
    return before_path != after_path, after_path


@lru_cache(maxsize=1)
def _load_model(model_repo: str, check_for_updates: bool = False):
    _ensure_mlx_deps()

    model_path = _resolve_model_path(model_repo, check_for_updates=check_for_updates)
    # Handing mlx_vlm.load() an already-resolved local directory (rather
    # than the bare repo id) makes it skip its own snapshot_download call
    # entirely -- see mlx_vlm.utils.get_model_path, which only resolves via
    # the network when the given path doesn't already exist on disk. This is
    # what actually avoids a second freshness-check network call per run.
    model, processor = vlm_load(model_path)
    config = load_config(model_path)
    return model, processor, config


def generate_caption(
    image_paths: list[str],
    prompt: str,
    model_repo: str,
    *,
    check_for_updates: bool = False,
    max_tokens: int = MAX_CAPTION_TOKENS,
) -> str:
    _ensure_mlx_deps()

    model, processor, config = _load_model(model_repo, check_for_updates)
    formatted_prompt = apply_chat_template(
        processor, config, prompt, num_images=len(image_paths)
    )
    result = vlm_generate(
        model,
        processor,
        formatted_prompt,
        image=image_paths,
        max_tokens=max_tokens,
        temperature=0.0,
        verbose=False,
    )
    return result.text


@dataclass
class CaptionSections:
    short: str | None
    long: str | None
    keywords: list[str] | None


# Small, hardcoded, dependency-free -- see "Explicitly rejected approach" in
# spec/metadata-embedding.md: real NLP (spaCy/nltk) was rejected as a heavy
# dependency that cuts against inference.py's lazy-import discipline. This
# is a fallback safety net only, not the primary KEYWORDS mechanism.
_STOPWORDS = {
    "a",
    "an",
    "the",
    "and",
    "or",
    "of",
    "in",
    "on",
    "at",
    "with",
    "this",
    "that",
    "these",
    "those",
    "is",
    "are",
    "was",
    "were",
    "to",
    "for",
    "as",
    "it",
    "its",
    "from",
    "by",
    "over",
    "near",
    "across",
    "through",
    "into",
    "up",
    "down",
    "out",
    "off",
    "above",
    "below",
    "between",
}

_SECTION_MARKERS = ("SHORT:", "LONG:", "KEYWORDS:")


def _split_sections(raw_text: str) -> dict[str, str]:
    """Splits raw_text on SHORT:/LONG:/KEYWORDS: markers, wherever present,
    regardless of order. Returns {marker_name_lowercase: section_text}."""
    # Find each marker's position, then slice from one marker to the next.
    # Matched case-insensitively -- the quantized model doesn't reliably
    # keep the prompt's uppercase labels (observed emitting "short:"/
    # "long:"/"keywords:"), and a missed marker here means its whole
    # section falls through as unparsed text (see cli.py's SHORT fallback,
    # which is exactly what a missed marker used to leak into filenames).
    # Uppercasing before searching doesn't shift any index, since every
    # marker is plain ASCII.
    upper_text = raw_text.upper()
    positions: list[tuple[int, str]] = []
    for marker in _SECTION_MARKERS:
        idx = upper_text.find(marker)
        if idx != -1:
            positions.append((idx, marker))
    positions.sort()

    sections: dict[str, str] = {}
    for i, (start, marker) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(raw_text)
        text = raw_text[start + len(marker) : end].strip()
        sections[marker[:-1].lower()] = text
    return sections


# The model sometimes echoes the prompt's own <placeholder> instruction
# text back instead of filling it in -- verbatim ("<3-6 words, for a
# filename>" -> "3-6 words"), truncated to a leading fragment, or
# paraphrased with digits swapped in for spelled-out numbers (LONG's "one
# to two sentences" -> "1-2 sentences"). Left unfiltered, this used to
# flow straight into the filename/metadata as if it were real content.
# Kept in sync by hand with config.METADATA_PROMPT /
# config.METADATA_BACKFILL_PROMPT's placeholder text -- there's no shared
# constant because the prompt wraps these across lines for readability,
# while matching here works against the whitespace-collapsed form.
_SHORT_PLACEHOLDER = "3-6 words, for a filename"
_LONG_PLACEHOLDER = "one to two sentences"
_KEYWORDS_PLACEHOLDER = (
    "6-10 comma-separated single words or short phrases naming subjects, "
    "actions, and setting -- no articles, no full sentences"
)
_SECTION_PLACEHOLDERS = {
    "short": _SHORT_PLACEHOLDER,
    "long": _LONG_PLACEHOLDER,
    "keywords": _KEYWORDS_PLACEHOLDER,
}

# A paraphrased count echo is lexically unrelated to the placeholder text
# above (no shared substring to match against), so it needs its own
# pattern: real caption content never legitimately opens by describing a
# word/sentence count the way the prompt's own instructions do.
_COUNT_ECHO_RE = re.compile(
    r"^(?:\d+\s*(?:-|to)\s*\d+|one to two|a few)\s+(?:words?|sentences?)\b",
    re.IGNORECASE,
)


def _looks_like_placeholder_echo(section_name: str, text: str) -> bool:
    """True if `text` looks like the model echoed the prompt's own
    <placeholder> instruction for `section_name` instead of producing
    real content -- exact echo, a truncated prefix of it, or a
    count-paraphrase (see _COUNT_ECHO_RE)."""
    normalized = " ".join(text.strip().strip("<>").split()).lower()
    if not normalized:
        return False
    if _SECTION_PLACEHOLDERS[section_name].lower().startswith(normalized):
        return True
    return bool(_COUNT_ECHO_RE.match(normalized))


def _looks_like_keyword_list(text: str) -> bool:
    # A malformed KEYWORDS section (e.g. a full sentence) is the most likely
    # of the three to break format on a quantized model -- see spec's
    # "Parsing & fallback." Heuristic: short, comma-separated, no
    # sentence-ending punctuation.
    if not text or "," not in text:
        return False
    return not any(text.rstrip().endswith(p) for p in (".", "!", "?"))


def _strip_surrounding_quotes(text: str) -> str:
    # Strips a single matching pair of surrounding quote characters, if
    # present, then re-trims in case there was whitespace inside them --
    # same idea as filenames.normalize_caption()'s whole-string quote
    # strip.
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


def _clean_keyword(raw: str) -> str:
    # The model sometimes wraps each keyword in its own quote marks (e.g.
    # `KEYWORDS: "train", "urban setting"`) even though the prompt asks
    # for a bare comma list -- strip that per-entry.
    return _strip_surrounding_quotes(raw)


def _strip_whole_list_quotes(text: str) -> str:
    # The model sometimes wraps the *entire* KEYWORDS list in a single
    # pair of quotes (e.g. `KEYWORDS: "train, urban setting"`) rather than
    # quoting each keyword. Splitting that on commas before stripping
    # leaves a stray quote stuck to the first/last keyword (verbatim
    # `\"Historic Building` / `Exterior\"` in JSON output) since neither
    # is individually a matched quote pair -- see the real-world example
    # this was reported against.
    #
    # Distinguishing this from legitimate per-keyword quoting (which
    # _clean_keyword handles per-entry, after splitting) matters: a
    # per-keyword-quoted list also happens to start and end with a quote
    # character overall, purely because its first/last *keyword* does.
    # The signal used here: in the whole-list case, the first token
    # (before the first comma) opens with a quote it doesn't itself
    # close, and the last token closes with a quote it doesn't itself
    # open. A quoted first/last keyword is balanced on its own -- leave
    # those alone entirely and let _clean_keyword handle them per-entry.
    if len(text) < 2 or text[0] not in "\"'" or text[-1] != text[0]:
        return text
    quote = text[0]
    segments = text.split(",")
    first = segments[0].strip()
    last = segments[-1].strip()
    first_is_balanced = len(first) >= 2 and first[0] == quote and first[-1] == quote
    last_is_balanced = len(last) >= 2 and last[0] == quote and last[-1] == quote
    if first_is_balanced or last_is_balanced:
        return text
    return text[1:-1].strip()


def derive_short_from_long(long_caption: str, max_words: int = 6) -> str:
    """Fallback for when SHORT is missing but LONG parsed successfully
    (e.g. the model skipped SHORT outright): the first max_words words of
    LONG, not the full raw multi-section response -- see cli.py's caller.
    Deliberately cheap/mechanical, same spirit as
    _derive_keywords_from_long."""
    return " ".join(long_caption.split()[:max_words])


def derive_short_from_keywords(keywords: list[str], max_keywords: int = 4) -> str:
    """Last-resort fallback for when both SHORT and LONG are missing/
    placeholder-echoes but KEYWORDS parsed successfully: join the first
    max_keywords keywords, not the full raw multi-section response -- see
    cli.py's caller."""
    return " ".join(keywords[:max_keywords])


def _derive_keywords_from_long(long_caption: str, max_keywords: int = 10) -> list[str]:
    """Fallback derivation when KEYWORDS is missing/malformed: tokenize,
    lowercase, strip punctuation, drop stopwords, dedupe (preserving order),
    cap at max_keywords. Not true noun-phrase extraction -- deliberately a
    cheap safety net, not the primary mechanism (see spec)."""
    words = (
        long_caption.lower()
        .translate(str.maketrans("", "", string.punctuation))
        .split()
    )
    keywords: list[str] = []
    seen: set[str] = set()
    for word in words:
        if word in _STOPWORDS or word in seen:
            continue
        seen.add(word)
        keywords.append(word)
        if len(keywords) >= max_keywords:
            break
    return keywords


def parse_caption_sections(raw_text: str) -> CaptionSections:
    """Parses a SHORT:/LONG:/KEYWORDS: (or LONG:/KEYWORDS: only, for the
    backfill prompt) response. Any section may be absent. A section whose
    text is just the model echoing its own <placeholder> instruction back
    (see _looks_like_placeholder_echo) is treated the same as an absent
    section, not as real content. A malformed or missing KEYWORDS section
    falls back to _derive_keywords_from_long() against whatever LONG text
    was parsed."""
    sections = _split_sections(raw_text)

    raw_short = sections.get("short")
    short = (
        raw_short
        if raw_short and not _looks_like_placeholder_echo("short", raw_short)
        else None
    )

    raw_long = sections.get("long")
    long_text = (
        raw_long
        if raw_long and not _looks_like_placeholder_echo("long", raw_long)
        else None
    )

    keywords: list[str] | None
    raw_keywords = sections.get("keywords")
    if raw_keywords:
        raw_keywords = _strip_whole_list_quotes(raw_keywords)
    if (
        raw_keywords
        and not _looks_like_placeholder_echo("keywords", raw_keywords)
        and _looks_like_keyword_list(raw_keywords)
    ):
        keywords = [_clean_keyword(kw) for kw in raw_keywords.split(",") if kw.strip()]
        keywords = [kw for kw in keywords if kw]
    elif long_text:
        keywords = _derive_keywords_from_long(long_text)
    else:
        keywords = None

    return CaptionSections(short=short, long=long_text, keywords=keywords)
