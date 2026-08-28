# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## Project Overview

**slate** is a personal-use, Apple Silicon-only CLI that scans a directory of
camera footage, captions each clip with a local vision-language model
(`mlx-vlm`), and renames generically-named camera files (e.g.
`A017_C015_0806GQ.mov`) to include that caption. No cloud dependency after
the model weights are first downloaded. Full design rationale for every
decision below lives in `PROJECT_SPEC.md`; `README.md`'s "Technical
Decisions and Opinions" section is a shorter map of the same material.

## Setup

- Requires **macOS on Apple Silicon** — `mlx-vlm` doesn't run anywhere else,
  and ProRes RAW frame extraction depends on macOS-only frameworks. Preflight
  checks in `preflight.py` enforce this (and the presence of `ffmpeg`,
  `ffprobe`, `qlmanage`, `sips`) at the start of every invocation.
- `uv sync` (or `make install`) installs project + dev dependencies into
  `.venv`.
- Config file is optional: `~/.config/slate/config.toml` (or `$SLATE_CONFIG`).
  See `config.example.toml` for a starting point.

## Commands

A `Makefile` wraps the common `uv` invocations — run `make help` for the
full list. Notable targets:

- Install deps: `make install` (`uv sync`)
- Run the CLI: `make run ARGS='--dry-run --input-dir footage'`
- Test: `make test` (unit tests, excludes `tests/integration`), `make
  test-integration` (real `ffmpeg`/`qlmanage`/`mlx-vlm` against
  `tests/fixtures/footage/`, auto-skipped when empty)
- Lint: `make lint` (`uv run ruff check .`, add `-fix` target variant to
  auto-fix), `make format` / `make format-check` (`uv run ruff format .`)
- `make check` runs lint + format-check + test together
- `make update` upgrades all dependencies and refreshes `uv.lock`;
  `make outdated` lists what's behind

## Architecture

Two-phase workflow, split so the slow/expensive step (decode + VLM
inference) is decoupled from the destructive step (renaming real footage),
with a human review checkpoint in between:

- **`--dry-run`** — discovers files, pairs MOV/MP4 by shared stem, extracts
  a frame, captions it, and writes `rename_mappings.json` (a JSON list of
  groups, not renaming anything). Re-running `--dry-run` is safe/incremental:
  groups already present (matched by `original_files`) are skipped and
  carried over unchanged, regardless of prior `status`.
- **`--rename-only --rename-mappings=rename_mappings.json`** — re-checks
  every file still exists, confirms, then applies the renames from a
  (possibly hand-edited) mapping file. Writes an audit trail
  (`review/applied_renames_<timestamp>.json`) and, by default, an undo
  script (`undo_renames_<timestamp>.sh`).
- **`--process-and-rename`** — runs both phases in one invocation, skipping
  the hand-edit pause. Keeps every Phase 2 safety mechanic; its confirmation
  prompt shows a sample of actual generated captions instead of just a count,
  since there's no review checkpoint to lean on.
- **`--model-update-check`** — the sole explicit way to check the Hub for a
  newer model revision; every other mode resolves the model from the local
  `huggingface_hub` cache with no network call.

Module layout under `src/slate/`:

- `cli.py` — argument parsing (`argparse`, not `click`/`typer` — see
  "Language / Packaging" in `PROJECT_SPEC.md` for why) + phase orchestration
- `config.py` — config file resolution/parsing; precedence is CLI flags >
  config file > built-in defaults
- `extraction.py` — frame extraction fallback ladder: `ffmpeg` first, then
  `qlmanage`+`sips` for RAW formats ffmpeg can't decode (attempt, not
  predict — no static codec allowlist)
- `filenames.py` — filename assembly (prepend/append caption,
  prefix/suffix) + `max_file_name_length` truncation (truncates the
  caption only, never the original stem or prefix/suffix)
- `inference.py` — model resolution/caching (defers to `huggingface_hub`'s
  standard cache) + captioning
- `mappings.py` — `rename_mappings.json` read/write + disambiguation
  (`_2`/`_3`... suffixes on output-name collisions)
- `output.py` — centralized `rich`-based colorized console output
- `pairing.py` — MOV/MP4 pairing: verifies same-stem files via `ffprobe`
  duration/frame-count before trusting either as a stand-in for the other;
  picks the smaller file as the captioning source
- `preflight.py` — startup platform/binary checks, all run and reported
  together rather than failing at the first problem
- `rename.py` — rename plan/execution, audit trail, undo script generation

## Conventions

- Formatting and style are enforced by `ruff` (config in `pyproject.toml`'s
  `[tool.ruff]`/`[tool.ruff.lint]`), not hand-applied -- run `uv run ruff
  format .` rather than manually matching existing style, and `uv run ruff
  check .` before considering a change done.
- `--rename-mappings` (and other `=`-joinable flags) are written
  `--flag=value` in usage examples and docs throughout the codebase — match
  that style rather than the space-separated form.
- All CLI status output goes through `output.console.print` (rich markup),
  not bare `print()`.

## Notes

- First run with a given model requires network access to download its
  weights (~1.5GB for the default `mlx-community/Qwen2-VL-2B-Instruct-4bit`);
  every run after that is fully local.
- `tests/integration` exercises real `ffmpeg`/`qlmanage`/`mlx-vlm` against
  footage in `tests/fixtures/footage/` and is auto-skipped when that
  directory is empty — don't expect it to run meaningfully without real
  sample footage in place.
- `PROJECT_SPEC.md` is the source of truth for design rationale; when
  behavior changes, check whether it (and README.md's "Technical Decisions
  and Opinions" section) need updating too.
