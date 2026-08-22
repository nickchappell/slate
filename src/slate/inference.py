from __future__ import annotations

from functools import lru_cache

from mlx_vlm import generate as vlm_generate
from mlx_vlm import load as vlm_load
from mlx_vlm.prompt_utils import apply_chat_template
from mlx_vlm.utils import load_config

# See "Model / Inference" -> "Caption prompt" in PROJECT_SPEC.md: word-count
# instructions alone don't reliably bound output length, so a fixed
# generation-time token cap is the actual backstop, independent of prompt
# wording. Not currently a config option.
MAX_CAPTION_TOKENS = 25


@lru_cache(maxsize=1)
def _load_model(model_repo: str):
    model, processor = vlm_load(model_repo)
    config = load_config(model_repo)
    return model, processor, config


def generate_caption(image_path: str, prompt: str, model_repo: str) -> str:
    model, processor, config = _load_model(model_repo)
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
