# slate

Video image classifier: a Python CLI tool that captions and renames
generically-named raw camera video files (e.g. `A017_C015_0806GQ.MOV`) using
a local vision-language model -- no cloud dependency after the model weights
are downloaded once.

Personal-use tool, **Apple Silicon Macs only**. See `PROJECT_SPEC.md` for the
full design.

## Getting Started

```bash
# from a clone of this repo
uv tool install .

slate --input-dir ~/Movies/Footage --dry-run
```

This scans the directory, pairs up `.MOV`/`.MP4` files from the same
recording, generates a short caption for each clip with a local VLM, and
writes a `mappings.json` + a `review/` folder of captioned preview JPEGs --
without touching any of your original files. Once you've eyeballed the
captions, apply them:

```bash
slate --input-dir ~/Movies/Footage --rename-only --rename-mappings=mappings.json
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

- **First run** with a given model requires network access to download its
  weights. This is the one exception to "no cloud dependency" -- every run
  after that is fully local.
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
generates a caption, and writes `mappings.json` + a `review/` folder of
captioned preview JPEGs. **Never renames or modifies your source files.**

```bash
slate --input-dir ~/Movies/Footage --dry-run
```

`mappings.json` is meant to be hand-edited -- fix a bad caption, or delete an
entry to force it to be reprocessed on the next `--dry-run`. Re-running
`--dry-run` on the same folder is safe and incremental: groups already
present in `mappings.json` are skipped (printed as `SKIP`) and carried over
unchanged, so hand edits survive a re-run.

Instead of a whole directory, you can operate on an explicit list of files
-- nothing else in the directory is touched or even looked at:

```bash
slate --input-files clip1.MOV clip1.MP4 clip2.MP4 --dry-run
```

### Phase 2 -- `--rename-only`

Applies a previously-reviewed (and optionally hand-edited) `mappings.json` to
disk:

```bash
slate --input-dir ~/Movies/Footage --rename-only --rename-mappings=mappings.json
```

Before renaming anything, this checks that every file still exists (files
may have moved or been deleted since the dry-run), warns and skips any group
with a missing pairing partner, and checks for destination-name collisions --
reporting every problem up front rather than failing partway through a
batch. You'll be prompted to confirm before anything is renamed, unless
`--yes`/`-y` is passed.

After a successful run, `mappings.json` is renamed to
`mappings.applied.<timestamp>.json` as an audit trail, and an undo script
(`mappings.applied.<timestamp>.undo.sh`) is written alongside it by default
-- a plain, directly-runnable shell script that reverses every rename that
actually succeeded.

### Phase 3 -- `--process-and-rename`

Runs Phase 1 and Phase 2 back-to-back in one invocation, skipping the pause
for hand-editing `mappings.json` in between:

```bash
slate --input-dir ~/Movies/Footage --process-and-rename
```

This bypasses the human caption-review checkpoint that's the whole point of
the two-phase split, so it's meant for batches where you've already
validated the prompt/model on a sample of the footage -- not as the default
way to run the tool on footage you haven't captioned with this model/prompt
before. Because there's no review step, its confirmation prompt (still
skippable with `--yes`/`-y`) shows a sample of the actual generated captions,
not just an operation count.

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
     `mappings.json` and the rest of the batch continues -- one undecodable
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

This is already covered by the repo's `.gitignore` (which excludes all
`*.MOV`/`*.mp4` files anywhere in the tree), so anything placed there never
gets committed regardless of size. Then run:

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
