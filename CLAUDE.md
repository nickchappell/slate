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
  auto-fix), `make format` / `make format-check` (`uv run ruff format .`).
  `make fix` runs `lint-fix` + `format` in one shot.
- `make check` runs lint + format-check + lock-check + test together
- `make coverage` runs the unit tests under `pytest-cov` (term + `htmlcov/`
  + `coverage.xml`); `make coverage-report` regenerates those reports from
  the last run's data. `make audit` scans dependencies for known
  vulnerabilities via `pip-audit`.
- `make update` upgrades all dependencies and refreshes `uv.lock`;
  `make outdated` lists what's behind; `make lock-check` verifies `uv.lock`
  is in sync with `pyproject.toml` without modifying it
- `make install-tool` / `make reinstall-tool` install `slate` onto `PATH`
  as a `uv` tool (`--compile-bytecode`, so the first run skips the
  bytecode-compile step); `make uninstall-tool` removes it
- Release process (each requires a clean working tree; `make help` prints
  a runnable example for every target):
  - `make bump-version PART=patch|minor|major` (default `patch`) — bumps
    the version via `uv version --bump`, then commits `pyproject.toml`/
    `uv.lock` and creates a `vX.Y.Z` git tag. Doesn't push. Signing is
    inherited from git config (`commit.gpgsign`/`tag.gpgsign`), not
    controlled by the Makefile.
  - `make github-release` — creates a GitHub release for the current
    version's tag via `gh release create --verify-tag --generate-notes`.
    Checks `gh` is installed and authenticated (`gh auth status`) first,
    and deliberately fails if the tag hasn't been pushed to `origin` yet
    (`--verify-tag`) rather than letting `gh` cut a new tag against the
    wrong commit.
  - `make create-release PART=...` — `bump-version`, then `git push` +
    `git push origin <tag>`, then `github-release`, end to end.

## Architecture

Two-phase workflow, split so the slow/expensive step (decode + VLM
inference) is decoupled from the destructive step (renaming real footage),
with a human review checkpoint in between:

- **`--dry-run`** — discovers files, pairs MOV/MP4 by shared stem, extracts
  a frame, captions it, and writes `review/rename_mappings.json` (a JSON
  object — `app_version` + a list of `groups` — living inside `review/`
  alongside the preview JPEGs it describes, not renaming anything).
  Re-running `--dry-run` is safe/incremental: groups already present
  (matched by `original_files`) are skipped and carried over unchanged,
  regardless of prior `status`.
- **`--rename-only --rename-mappings=review/rename_mappings.json`** — first
  checks the file's `app_version` against the running app's (refuses to
  proceed on a major-version difference — see `mappings.
  major_version_mismatch`), then reconciles the mapping against any preview
  JPEGs a human renamed in `review/` (see `review_sync.py`), then re-checks
  every file still exists, confirms, and applies the renames from the
  (possibly hand-edited, in JSON and/or by renaming JPEGs) mapping file.
  Writes an audit trail (`review/applied_renames_<timestamp>.json`, renamed
  in place from `rename_mappings.json`) and, by default, an undo script
  (`undo_renames_<timestamp>.sh`, written one level up from `review/`).
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
  standard cache) + captioning. `mlx_vlm`/`huggingface_hub` are imported
  lazily (module-level `None` placeholders populated by `_ensure_*_deps()`)
  so non-captioning runs don't pay the ~0.9s import — see "Startup Time" in
  `PROJECT_SPEC.md`; don't "tidy" those placeholders into normal imports
- `mappings.py` — `rename_mappings.json` read/write + disambiguation
  (`_2`/`_3`... suffixes on output-name collisions) + `app_version`
  stamping/major-version-mismatch check
- `output.py` — centralized `rich`-based colorized console output
- `pairing.py` — MOV/MP4 pairing: verifies same-stem files via `ffprobe`
  duration/frame-count before trusting either as a stand-in for the other;
  picks the smaller file as the captioning source
- `preflight.py` — startup platform/binary checks, all run and reported
  together rather than failing at the first problem
- `rename.py` — rename plan/execution, audit trail, undo script generation
- `review_sync.py` — reconciles `rename_mappings.json` against human renames
  of preview JPEGs in `review/`, via each JPEG's SHA-256 (a plain rename
  doesn't touch file bytes, so the hash survives it); runs at the start of
  Phase 2

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
- pytest runs with `--strict-markers` / `--strict-config` (see
  `[tool.pytest.ini_options]`): a `@pytest.mark.<x>` that isn't registered
  in the `markers` list is an error, not a silent skip. Register new
  markers there. Coverage config (`source`, `branch`, `exclude_also`) also
  lives in `pyproject.toml` under `[tool.coverage.*]`, so `make coverage`
  and a bare `coverage report` agree.

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

## Deliberate Non-Goals

Decisions made and settled — don't re-propose these without the owner
raising it first.

- **No CI / no GitHub Actions.** The reason is cost, and it is decisive:
  this project can only be tested on GitHub's macOS runners (the `mlx-vlm`
  hard dep ships macOS-arm64-only wheels, so `uv sync` — and therefore any
  test job — fails outright on the cheap/free Linux runners), and macOS
  runner minutes bill at **10x** the Linux rate. The owner is not willing
  to take on that GHA billing exposure for a personal-use tool. This is
  not an oversight or a "todo later" — do not add a `.github/workflows/`
  file, and do not propose one. `make check` is the gate instead
  (lint + format-check + lock-check + unit tests; the unit suite is
  hermetic — no real `ffmpeg`/`mlx-vlm`/network — so it covers everything
  except `tests/integration`).
- **Not published to PyPI.** Personal-use tool, installed via
  `uv tool install .` / `make install-tool` from a checkout. Hence no
  `classifiers`/`keywords`/`[project.urls]` in `pyproject.toml` (PyPI
  discovery metadata only). `readme`/`license`/`license-files` *are* set so
  `uv build` produces a complete wheel, but that's for local/offline use,
  not a release channel.
- **Runtime deps carry lower bounds only** (`mlx-vlm>=…`, etc.), not upper
  pins. `uv.lock` is the reproducibility mechanism for `uv sync`; the
  floors exist solely because `uv tool install .` resolves from
  `pyproject.toml` rather than the lock. Bump the floors to the locked
  versions when `make update` moves them meaningfully.
- **`[tool.uv] required-version` is pinned to one minor** (`>=0.12,<0.13`),
  kept in lockstep with the `uv_build` pin in `[build-system]`. Bump both
  together.

## Planned Improvements

Ideas not yet implemented. Each should get a full write-up in
`PROJECT_SPEC.md` (and a mention in README.md's "Technical Decisions and
Opinions") when it lands.

- **Multi-frame input for VLM inference.** Today `extraction.py` extracts a
  single frame and `inference.py` captions it with `num_images=1`, so the
  caption is only as good as that one grab (motion blur, a frame on a cut,
  a subject facing away all poison it). Sample several frames spread across
  the clip instead. Two ways to feed them:
  - **Grid/montage** — tile the frames into one image. Robust (works with
    any single-image model) and fixed at one image's worth of tokens, but
    downsamples every frame (a 3x3 grid ~= 1/3 linear resolution per cell,
    which hurts for reading slate markings / signage) and needs a prompt
    that says "these are frames from one clip, describe the scene, not the
    layout."
  - **Native multi-image** — `apply_chat_template(..., num_images=n)` +
    `image=[...]`. Keeps every frame full-resolution; higher quality when
    the installed `mlx-vlm` + model support it reliably. Qwen2-VL also has
    real video-input support worth evaluating here.

  Prototype both and compare caption quality on real footage in
  `tests/fixtures/footage/` before committing to one.
