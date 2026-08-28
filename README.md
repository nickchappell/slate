# slate

Video image classifier: a Python CLI tool that captions and renames
generically-named raw camera video files (e.g. `A017_C015_0806GQ.MOV`) using
a local vision-language model -- no cloud dependency after the model weights
are downloaded once.

Personal-use tool. See `PROJECT_SPEC.md` for the full design.

> [!IMPORTANT]
> **Apple Silicon Macs only.** `mlx-vlm` doesn't run anywhere else, and
> ProRes RAW frame extraction depends on macOS-only frameworks. `slate`
> checks this at startup and refuses to run elsewhere rather than failing
> with a cryptic import error.

## Table of Contents

- [Getting Started](#getting-started)
- [Installation](#installation)
  - [Prerequisites](#prerequisites)
  - [Installing `slate` onto your `$PATH`](#installing-slate-onto-your-path)
  - [Model weights (first-run download)](#model-weights-first-run-download)
- [Usage](#usage)
  - [Phase 1 -- `--dry-run`](#phase-1------dry-run)
  - [Phase 2 -- `--rename-only`](#phase-2------rename-only)
  - [Phase 3 -- `--process-and-rename`](#phase-3------process-and-rename)
  - [Other flags](#other-flags)
- [Under the Hood](#under-the-hood)
- [Development](#development)
  - [Cutting a release](#cutting-a-release)
- [Tests](#tests)
- [Linting & Formatting](#linting--formatting)
- [Technical Decisions and Opinions](#technical-decisions-and-opinions)

## Getting Started

```bash
# from a clone of this repo
uv tool install .

slate --input-dir ~/Movies/Footage --dry-run
```

This scans the directory, generates a short caption for each clip with a local VLM, and
writes a `rename_mappings.json` + a `review/` folder of captioned preview JPEGs --
without touching any of your original files.

If any files exist as pairs for the same footage but are just different formats (like a raw
video file paired with a smaller lower res `.mp4` file for proxy/preview), both files are
matched based on length/frame count (with +/- 0.1% tolerances to account for codec/container
differences) and treated the same for renaming.

Once you've eyeballed the captions, apply them:

```bash
slate --input-dir ~/Movies/Footage --rename-only --rename-mappings=rename_mappings.json
```

Or skip the review step entirely once you trust the model/prompt for a given
batch:

```bash
slate --input-dir ~/Movies/Footage --process-and-rename
```

## Installation

### Prerequisites

- **Apple Silicon Mac.** This is a hard requirement, checked at startup, not
  just a recommendation: [`mlx-vlm`](https://github.com/Blaizzy/mlx-vlm) only
  runs on Apple Silicon, and ProRes RAW decoding relies on macOS-only
  frameworks. `slate` will refuse to run on Intel Macs or other platforms
  with a clear error rather than a cryptic import failure.
- **`ffmpeg` and `ffprobe`** on `PATH` -- not bundled with macOS. Install via
  Homebrew:
  ```bash
  brew install ffmpeg
  ```
- **`qlmanage` and `sips`** on `PATH` -- standard macOS system binaries
  (`/usr/bin/qlmanage`, `/usr/bin/sips`), present on any normal install.
  `slate` checks for them anyway as a defensive guard against unusual
  environments (minimal/managed images, stripped-down runners).
- **[`uv`](https://docs.astral.sh/uv/)** for installing/running the tool.

All of the above are checked once at the start of every `slate` invocation
(see "Preflight Checks" in `PROJECT_SPEC.md`); every problem is reported
together, not one-at-a-time.

### Installing `slate` onto your `$PATH`

From a clone of this repository:

```bash
uv tool install .
```

This installs `slate` as a standalone CLI command available in any shell,
isolated in its own managed environment -- you don't need to activate a venv
to run it afterward. To pick up changes after editing the source:

```bash
uv tool install --reinstall .
```

### Model weights (first-run download)

`slate` uses [`mlx-vlm`](https://github.com/Blaizzy/mlx-vlm) to run
`mlx-community/Qwen2-VL-2B-Instruct-4bit` (~1.5GB) locally. Model resolution
goes through `huggingface_hub`'s standard cache, not anything `slate`-specific:

> [!NOTE]
> **First run** with a given model requires network access to download its
> weights. This is the one exception to "no cloud dependency" -- every run
> after that is fully local.

- **Cached at** `~/.cache/huggingface/hub/`, shared with any other tool that
  uses the same Hugging Face repo ID (LM Studio, other `mlx-vlm` scripts,
  `transformers`, etc.) -- `slate` doesn't keep a private copy.
- Override the cache location with the standard `HF_HOME` (default
  `~/.cache/huggingface`) or `HF_HUB_CACHE` (default `$HF_HOME/hub`)
  environment variables, e.g. to put multi-GB weights on an external disk.
- Set `HF_HUB_OFFLINE=1` once a model is cached to skip the network
  freshness check entirely.
- Switching models (via `--model` or the config file) downloads and caches
  each one separately -- disk usage compounds across models tried. Use
  `huggingface-cli delete-cache` to clean up if needed.

## Usage

`slate` has three mutually-exclusive modes, exactly one of which is required
per invocation:

### Phase 1 -- `--dry-run`

Scans for footage, pairs `.MOV`/`.MP4` files, extracts a frame per clip,
generates a caption, and writes `rename_mappings.json` + a `review/` folder of
captioned preview JPEGs. **Never renames or modifies your source files.**

```bash
slate --input-dir ~/Movies/Footage --dry-run
```

`rename_mappings.json` is meant to be hand-edited -- fix a bad caption, or delete an
entry to force it to be reprocessed on the next `--dry-run`. Re-running
`--dry-run` on the same folder is safe and incremental: groups already
present in `rename_mappings.json` are skipped (printed as `SKIP`) and carried over
unchanged, so hand edits survive a re-run.

Instead of a whole directory, you can operate on an explicit list of files
-- nothing else in the directory is touched or even looked at:

```bash
slate --input-files clip1.MOV clip1.MP4 clip2.MP4 --dry-run
```

### Phase 2 -- `--rename-only`

Applies a previously-reviewed (and optionally hand-edited) `rename_mappings.json` to
disk:

```bash
slate --input-dir ~/Movies/Footage --rename-only --rename-mappings=rename_mappings.json
```

Before renaming anything, this checks that every file still exists (files
may have moved or been deleted since the dry-run), warns and skips any group
with a missing pairing partner, and checks for destination-name collisions --
reporting every problem up front rather than failing partway through a
batch. You'll be prompted to confirm before anything is renamed, unless
`--yes`/`-y` is passed.

After a successful run, `rename_mappings.json` is renamed into
`review/applied_renames_<timestamp>.json` as an audit trail (a sibling of
the preview JPEGs), and an undo script
(`undo_renames_<timestamp>.sh`) is written at the top level by
default -- a plain, directly-runnable shell script (`./undo_renames_<timestamp>.sh`)
that reverses every rename that actually succeeded. The shared timestamp
correlates the two files even though they live in different places.

### Phase 3 -- `--process-and-rename`

Runs Phase 1 and Phase 2 back-to-back in one invocation, skipping the pause
for hand-editing `rename_mappings.json` in between:

```bash
slate --input-dir ~/Movies/Footage --process-and-rename
```

> [!WARNING]
> This bypasses the human caption-review checkpoint that's the whole point
> of the two-phase split, so it's meant for batches where you've already
> validated the prompt/model on a sample of the footage -- **not** as the
> default way to run the tool on footage you haven't captioned with this
> model/prompt before. Because there's no review step, its confirmation
> prompt (still skippable with `--yes`/`-y`) shows a sample of the actual
> generated captions, not just an operation count.

### Other flags

| Flag | Effect |
| --- | --- |
| `--model REPO_ID` | Override the VLM used for captioning (any Hugging Face repo ID `mlx-vlm` supports). |
| `--prepend-generated-name` / `--append-generated-name` | Caption before or after the original filename (mutually exclusive; default: append). |
| `--prefix TEXT` / `--suffix TEXT` | Wrap the whole assembled name, e.g. a shoot's location known ahead of time. |
| `--skip-generate-undo-script` | Suppress the undo script written after Phase 2/3 (on by default). |
| `--yes` / `-y` | Skip the confirmation prompt in Phase 2/3. |

All of the above (except `--model`'s CLI/config resolution) also have a
config-file equivalent -- see "Configuration" in `PROJECT_SPEC.md`. Two
values (`max_file_name_length`, `prompt`) are config-only, with no CLI flag,
since they're not the kind of thing worth retyping per invocation. Copy
`config.example.toml` to `~/.config/slate/config.toml` (or point the
`SLATE_CONFIG` environment variable at a copy kept elsewhere) to set
defaults so you don't have to repeat flags across runs -- **CLI flags always
win over the config file**, which always wins over built-in defaults.

## Under the Hood

`slate` is orchestration around a handful of external tools -- it doesn't
implement video decoding, image processing, or caption generation itself:

1. **Pairing (`ffprobe`).** Files sharing a stem (e.g. `A017_C010.MOV` /
   `A017_C010.MP4`) are verified as the same recording by comparing
   container-reported duration (falling back to frame count) via `ffprobe`,
   before either file is trusted as a stand-in for the other. The smaller
   file (the H.264/H.265 proxy, in practice) is selected as the captioning
   source, since 6K ProRes RAW buys no captioning-accuracy benefit -- the VLM
   resizes internally regardless of source resolution -- and only slows down
   frame extraction.
2. **Frame extraction, with a fallback ladder.** A single frame is pulled
   from a fixed timestamp per clip:
   - **First**, plain `ffmpeg` -- works for H.264/H.265 and regular ProRes
     (via VideoToolbox), but **cannot decode ProRes RAW** (no decoder exists
     for it, hardware or software).
   - **If that fails**, `qlmanage` (macOS's QuickLook thumbnail generator,
     backed by AVFoundation) -- this is what actually handles ProRes RAW and
     other camera-manufacturer RAW formats. `qlmanage` always emits PNG
     regardless of the requested filename, so the result is converted to
     JPEG with `sips`.
   - **If both fail**, the clip is marked as an `"error"` group in
     `rename_mappings.json` and the rest of the batch continues -- one undecodable
     clip never halts a run.
3. **Captioning (`mlx-vlm` + Qwen2-VL-2B).** The extracted frame is sent to a
   local vision-language model with a prompt engineered to produce a short,
   lowercase phrase rather than a full descriptive sentence (instruction-
   tuned VLMs default to verbose, scene-setting output unless explicitly
   steered away from it). A fixed generation-time token cap backstops the
   prompt's word-count request, since no prompt wording reliably bounds
   output length on its own.
4. **Normalization and assembly.** Raw model output is defensively cleaned up
   (filesystem-unsafe characters stripped, whitespace collapsed, surrounding
   quotes/punctuation trimmed, lowercased, truncated to 70 characters at a
   word boundary) and then assembled into the final filename alongside the
   original stem and any `--prefix`/`--suffix`, truncating the caption
   specifically (never the original filename) if the result would exceed
   the configured length limit. A batch-end disambiguation pass appends
   `_2`, `_3`, ... to any two clips whose *final* filenames would otherwise
   collide.
5. **Renaming.** Both files of a verified pair are always renamed together,
   even though only one was used for captioning, so RAW and proxy keep
   traveling together. Renames are logged incrementally as they happen (not
   just at the end), so a mid-batch crash still leaves a clear, undo-able
   record of what actually succeeded.

## Development

A `Makefile` wraps the `uv`/`ruff`/`pytest`/`gh` commands used below into
short, memorable targets. Run `make help` for the full list -- it prints a
runnable example under every target.

```bash
make install          # uv sync -- install project + dev dependencies
make run ARGS='--dry-run --input-dir footage'   # uv run slate ...
make lint              # uv run ruff check .
make format            # uv run ruff format .
make test              # unit tests only (see "Tests," below)
make test-integration  # integration tests only (see "Tests," below)
make check             # lint + format-check + test, in one shot
```

`make check` is the pre-commit sanity check -- run it before considering a
change done, same bar as CI would apply.

### Cutting a release

Three targets automate versioning and publishing, built on `uv version` and
the `gh` CLI:

> [!IMPORTANT]
> `create-release` pushes your current branch and the new tag to `origin`
> and creates a public GitHub release -- these are visible, shared-state
> actions, not local/reversible ones. Make sure you actually want to ship
> before running it.

```bash
make bump-version PART=patch   # or minor / major -- default is patch
make github-release            # create a GitHub release for the current tag
make create-release PART=minor # bump-version, push, then github-release
```

- `bump-version` refuses to run on a dirty working tree. It bumps the
  version in `pyproject.toml` (via `uv version --bump`), commits
  `pyproject.toml`/`uv.lock`, and creates a `vX.Y.Z` git tag -- but doesn't
  push. Whether the commit/tag end up GPG-signed depends entirely on your
  own `git config` (`commit.gpgsign`/`tag.gpgsign`), not on the Makefile.
- `github-release` checks that `gh` is installed and authenticated
  (`gh auth status`), then runs `gh release create --verify-tag
  --generate-notes` for the current version's tag. `--verify-tag`
  deliberately makes it fail if that tag hasn't been pushed to `origin`
  yet, rather than letting `gh` create a new tag against the wrong commit.
- `create-release` is the end-to-end version: `bump-version`, then
  `git push` (branch + tag), then `github-release`.

## Tests

The suite is split into two layers -- fast mocked unit tests, and slower
integration tests that exercise real `ffmpeg`/`qlmanage`/`mlx-vlm` against
real footage.

**To run unit tests only:**

```bash
uv run pytest tests/ --ignore=tests/integration
```

Fully mocked -- no `ffmpeg`, no real video files, no model download, and no
network access. Every subprocess call (`ffmpeg`/`ffprobe`/`qlmanage`/`sips`)
and every `mlx-vlm` call is monkeypatched. Runs in well under a second and is
safe to run anywhere, including CI. This is also what a plain `uv run pytest`
effectively reduces to as long as `tests/fixtures/footage/` is empty, since
the integration tests auto-skip in that case -- but the command above is the
explicit, unambiguous way to ask for unit tests specifically.

**To run integration/fixture-based tests:**

First, drop real `.MOV`/`.MP4` clips (pairs or singles -- a couple of
representative samples is enough, not a full camera dump) into:

```
tests/fixtures/footage/
```

> [!TIP]
> This is already covered by the repo's `.gitignore` (which excludes all
> `*.MOV`/`*.mp4` files anywhere in the tree), so anything placed there
> never gets committed regardless of size.

Then run:

```bash
uv run pytest -m integration
```

This exercises the real pipeline end-to-end: actual `ffmpeg`/`qlmanage`
frame extraction and actual `mlx-vlm` inference against whatever's in
`tests/fixtures/footage/` (triggering the one-time model download on first
run -- see "Model weights," above). If that directory is empty, these tests
report as `skipped` rather than failing, so an empty checkout or a
non-Apple-Silicon machine won't break anything.

**To run everything** (unit tests, plus integration tests if fixtures are
present):

```bash
uv run pytest
```

## Linting & Formatting

[`ruff`](https://docs.astral.sh/ruff/) handles both linting and formatting --
one fast tool instead of a separate flake8/isort/black stack. Config lives in
`[tool.ruff]`/`[tool.ruff.lint]` in `pyproject.toml` (88-character line
length, matching `.editorconfig`; pycodestyle, pyflakes, isort, pyupgrade,
and flake8-bugbear rules enabled).

```bash
# check for lint issues (imports, unused code, style, common bug patterns)
uv run ruff check .

# apply auto-fixes for whatever's safely fixable
uv run ruff check --fix .

# reformat the codebase
uv run ruff format .

# check formatting without changing anything (what CI would run)
uv run ruff format --check .
```

## Technical Decisions and Opinions

`PROJECT_SPEC.md` has the full reasoning behind every decision below; this
section is a map of the load-bearing ones, for anyone extending the app who
wants to know why something works the way it does before changing it.

**Platform.** Apple Silicon is a hard requirement, not a soft preference --
`mlx-vlm` only runs on Apple Silicon, and ProRes RAW decoding depends on
macOS-only frameworks (`qlmanage`/AVFoundation). Preflight checks fail fast
with one specific message per problem (wrong OS, wrong CPU, missing
`ffmpeg`/`ffprobe`/`qlmanage`/`sips`), all reported together in one pass,
rather than surfacing a cryptic import error or a `subprocess` failure
halfway through a batch.

**MOV/MP4 pairing.** A same-stem pair is verified as the same recording via
`ffprobe` duration (falling back to frame count) before either file is
trusted as a stand-in for the other -- filename matching alone isn't enough.
The **smaller file** (not "the MP4," specifically) is picked as the
captioning source, since 6K ProRes RAW buys no captioning-accuracy benefit
-- the VLM resizes internally regardless of input resolution -- and only
slows down frame extraction. A verified mismatch (durations disagree beyond
tolerance) is written as an `"error"` group rather than guessed at
automatically; that's for a human to reconcile.

**Frame extraction is attempt-not-predict.** Rather than maintaining a
static list of "known good" codecs, `slate` just tries `ffmpeg` and checks
whether it worked. If it fails (ProRes RAW and other camera-RAW formats have
no `ffmpeg` decoder), it falls back to `qlmanage` -- macOS's QuickLook
thumbnailer, backed by AVFoundation -- converting its PNG output to JPEG with
`sips`. This ladder generalizes to other RAW formats (RED `.r3d`,
Blackmagic `.braw`, etc.) for free, as long as the user has the relevant
vendor's QuickLook plugin installed, since `qlmanage` does no decoding of
its own. One frame per clip, at a fixed timestamp -- multi-frame sampling
was considered and dropped as unjustified complexity (unclear `mlx-vlm`
multi-image support, and no clear way to merge multiple per-frame captions)
until real footage actually shows single-frame captions missing meaningful
content on panning/motion-heavy clips.

**Caption generation and normalization.** Instruction-tuned VLMs default to
verbose, scene-setting prose unless explicitly steered away from it -- a
bare word-count request in the prompt isn't reliable on its own, so the
default prompt pairs a format constraint with a one-shot example, backstopped
by a fixed max-token cap at generation time (words/tokens/characters are
three different units; the cap is what actually bounds output length, not
the prompt wording). Raw output is then defensively normalized (unsafe
characters stripped, whitespace collapsed, quotes/trailing punctuation
trimmed, lowercased, capped at 70 characters). Only `/` and the NUL byte are
filtered -- not `:`, since `slate` renames via `os.rename` directly, never
through Finder, so the classic Mac `:`/`/` translation quirk doesn't apply
here.

**Model caching defers entirely to `huggingface_hub`'s standard cache** --
no bespoke logic in `slate` itself. Every mode resolves the model from the
local cache with no network call once it's downloaded once; `--model-update-check`
is the one explicit, opt-in way to check the Hub for a newer revision. This
keeps "no cloud dependency after first run" true without a freshness check
silently running (and silently adding network latency) on every invocation.

**Filename assembly truncates the caption, never the original stem or
`--prefix`/`--suffix`** -- those are the parts a truncation rule must never
touch, since the whole point of prepend/append/prefix/suffix is that the
user chose that content deliberately. `max_file_name_length` (default 255,
matching APFS's per-path-component limit) counts *characters*, not the
UTF-8 *bytes* APFS actually limits on -- a known, accepted caveat for
multi-byte captions/locations, not yet worth the complexity of byte-aware
truncation.

**Two-phase workflow (`--dry-run` then `--rename-only`), plus a combined
`--process-and-rename`.** The slow/expensive step (decode + inference) is
deliberately decoupled from the destructive step (renaming real footage),
with a human review checkpoint in between -- editing `new_stem` by hand in
`rename_mappings.json` is the actual point of the split, not an
afterthought. Re-running `--dry-run` on the same folder is safe: a group is
"already there" if its `original_files` set-matches an existing entry,
*regardless of that entry's `status`*, so hand-edited captions survive a
re-run untouched and old errors aren't silently retried. `--process-and-rename`
exists for batches where the prompt/model has already been validated; it
keeps every Phase 2 safety mechanic (pre-flight checks, incremental
logging, audit trail, undo script) and only removes the pause -- its
confirmation prompt shows a sample of actual generated captions rather than
just a count, since there's no review checkpoint to lean on.

**Rename safety.** Phase 2 re-checks that every file still exists immediately
before renaming (files may have moved or been deleted since the dry-run),
distinguishing a whole group missing (skipped, reported in aggregate) from a
partial pair missing (warned individually, surviving file left untouched --
a pair's `original_files` must never silently split). Renames are logged
incrementally as they happen, not just at the end, so a mid-batch crash
still leaves a clear, undo-able record of what actually succeeded. The undo
script is generated by default (`--skip-generate-undo-script` to opt out)
specifically because a safety net that depends on being remembered isn't
one; it's kept as a plain, directly-runnable shell script (quoted paths,
`mv -n`) rather than requiring `slate` itself to still exist/work to reverse
a batch.

**Configuration precedence is CLI flags > config file > built-in defaults,
always.** The config file (`~/.config/slate/config.toml`, or `$SLATE_CONFIG`)
exists purely to avoid retyping flags across runs; it's never required and
never becomes authoritative over an explicit flag. `max_file_name_length`
and `prompt` are config-only with no CLI equivalent, since neither is
something worth retyping per invocation.

**`argparse` over `typer`/`click`.** The original plan favored a modern CLI
framework, but `--input-files FILE [FILE ...]` (space-separated multi-value
input, not repeated `--input-files` flags) needs `nargs="+"` on a named
option, which `click` (and by extension `typer`) doesn't support for
options -- only for positional arguments. `argparse` handles it natively,
at the cost of writing `--help` formatting (including the colorized usage
examples block) by hand instead of getting it for free.
