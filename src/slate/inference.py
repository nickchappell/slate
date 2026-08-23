from __future__ import annotations

from functools import lru_cache

from huggingface_hub import snapshot_download
from huggingface_hub.errors import LocalEntryNotFoundError
from mlx_vlm import generate as vlm_generate
from mlx_vlm import load as vlm_load
from mlx_vlm.prompt_utils import apply_chat_template
from mlx_vlm.utils import load_config

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


def _resolve_model_path(model_repo: str, *, check_for_updates: bool) -> str:
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
    image_path: str,
    prompt: str,
    model_repo: str,
    *,
    check_for_updates: bool = False,
) -> str:
    model, processor, config = _load_model(model_repo, check_for_updates)
    formatted_prompt = apply_chat_template(processor, config, prompt, num_images=1)
    result = vlm_generate(
        model,
        processor,
        formatted_prompt,
        image=image_path,
        max_tokens=MAX_CAPTION_TOKENS,
        temperature=0.0,
        verbose=False,
    )
    return result.text
