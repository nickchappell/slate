from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_ENV_VAR = "SLATE_CONFIG"
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "slate" / "config.toml"

DEFAULT_MODEL = "mlx-community/Qwen2-VL-2B-Instruct-4bit"
DEFAULT_MAX_FILE_NAME_LENGTH = 255  # APFS per-path-component limit, in characters

DEFAULT_PROMPT = """\
Describe the main subject and action in this frame in 5 to 8 words.
Respond with only a short lowercase phrase — no full sentence, no
punctuation, no preamble like "the image shows" or "this is a picture of".

Example output: waves crashing on rocky shore
"""


@dataclass(frozen=True)
class Config:
    prepend_generated_name: bool = False
    prefix: str = ""
    suffix: str = ""
    generate_undo_script: bool = True
    model: str = DEFAULT_MODEL
    prompt: str = DEFAULT_PROMPT
    max_file_name_length: int = DEFAULT_MAX_FILE_NAME_LENGTH


def resolve_config_path() -> Path:
    override = os.environ.get(CONFIG_ENV_VAR)
    if override is None:
        return DEFAULT_CONFIG_PATH

    path = Path(override).expanduser()
    if not path.is_file():
        raise FileNotFoundError(
            f"{CONFIG_ENV_VAR} is set to {path}, but no file exists there."
        )
    return path


def load_config(path: Path | None = None) -> Config:
    if path is None:
        path = resolve_config_path()

    if not path.is_file():
        return Config()

    with path.open("rb") as f:
        data = tomllib.load(f)

    table = data.get("defaults", {})
    base = Config()
    return Config(
        prepend_generated_name=table.get(
            "prepend_generated_name", base.prepend_generated_name
        ),
        prefix=table.get("prefix", base.prefix),
        suffix=table.get("suffix", base.suffix),
        generate_undo_script=table.get(
            "generate_undo_script", base.generate_undo_script
        ),
        model=table.get("model", base.model),
        prompt=table.get("prompt", base.prompt),
        max_file_name_length=table.get(
            "max_file_name_length", base.max_file_name_length
        ),
    )
