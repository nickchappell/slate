from __future__ import annotations

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
        max_tokens=MAX_CAPTION_TOKENS,
        temperature=0.0,
        verbose=False,
    )
    return result.text
