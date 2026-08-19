# Slate — Project Spec

## Overview

A CLI tool that scans a directory of camera footage, generates short AI-written
descriptions of each clip's content using a local vision-language model, and
appends those descriptions to the filenames — so generic camera-generated names
like `A017_C015_0806GQ.mov` become self-describing.

Personal-use tool, Apple Silicon only, no cloud dependency.

## Platform Constraint

**Apple Silicon Mac only.** This is a hard requirement, not a limitation to
work around:
- MLX (and mlx-vlm) only runs on Apple Silicon.
- ProResRAW decoding relies on `qlmanage`/AVFoundation, which are macOS-only.
- The tool should check `platform.machine() == 'arm64'` at startup and fail
  with a clear error rather than a cryptic MLX import failure.

## Source Footage Format

The camera writes two files per recording, sharing the same base filename:
- `<name>.MOV` — ProResRAW, full resolution (e.g. 6K)
- `<name>.MP4` — H.264/H.265 in-camera proxy, lower resolution, same content

ffmpeg **cannot decode ProResRAW** (no native decoder exists, hardware or
software). Regular ProRes is fine via ffmpeg/VideoToolbox, but ProResRAW is
not. This is why the MP4 proxy matters: it's ffmpeg-readable and full
resolution buys no accuracy benefit for a VLM (see next section), so the
proxy should be strongly preferred as the captioning source when present.

## Model / Inference

**Library:** [mlx-vlm](https://github.com/Blaizzy/mlx-vlm) — MLX-native
vision-language model inference on Apple Silicon.

**Chosen model:** `mlx-community/Qwen2-VL-2B-Instruct-4bit`
- ~1.5GB, fast inference (well under 1s/frame on Apple Silicon)
- Good general scene/object recognition for short captions
- Reasonable fallback alternatives if quality/speed needs shift later:
  - **Moondream2** — smaller/faster, purpose-built for short captions
  - **Qwen2.5-VL-7B-Instruct-4bit** — better fine-grained accuracy, ~5GB, slower

**Key finding:** VLM inference time depends on the *input JPEG size fed to the
model*, not the source video resolution — the model resizes internally to its
native processing resolution regardless of whether the source was 6K RAW or
1080p. This means **6K ProResRAW does not slow down the captioning step** —
it only affects the frame-extraction step (see below).

## Frame Extraction Strategy

**Case 1: MP4 proxy exists (preferred path)**
Use `ffmpeg` directly on the MP4. Standard frame extraction, ideally:
- Seek before decode (`-ss` before `-i`) for speed
- Downscale during extraction, not after (avoid full-res intermediate)
- Consider sampling 2–3 frames across the clip (not just frame 0) for more
  robust captions on clips with significant visual change (e.g. a pan)

```bash
ffmpeg -ss 00:00:01 -i input.mp4 -vframes 1 -vf scale=896:-1 frame.jpg
```

**Case 2: Only the MOV (ProResRAW) exists, or verification fails**
ffmpeg cannot decode the frame. Fall back to `qlmanage`, which uses Apple's
native frameworks (and therefore *can* decode ProResRAW):

```bash
qlmanage -t -s 1024 -o /tmp A017_C015_0806GQ.mov
```

`qlmanage -t` always outputs **PNG**, regardless of the requested output name.
Convert to JPEG afterward with `sips` (no extra dependency):

```bash
sips -s format jpeg /tmp/A017_C015_0806GQ.mov.png --out /tmp/frame.jpg
```

**Optional hardware-accelerated path for regular ProRes (not RAW):**
```bash
ffmpeg -hwaccel videotoolbox -i input.mov -vf scale=896:-1 -vframes 1 frame.jpg
```
Not applicable to ProResRAW specifically — included for completeness in case
non-RAW ProRes sources ever appear in the footage set.

## MOV/MP4 Pairing Logic

1. **Group files by shared base filename (stem).**
2. **If only one file exists in the group** → use whatever it is, routing
   through the appropriate extraction path above.
3. **If both `.MOV` and `.MP4` exist** → verify they're the same recording
   before trusting the MP4 as a stand-in:
   - Use `ffprobe` to compare `duration` (±0.1s tolerance) and, if available,
     `nb_frames` / frame rate. This reads container metadata only — it does
     not require decoding ProResRAW, so it's cheap even on the MOV file.
   - If ffprobe can't read stream info from the MOV at all, fall back to
     trusting the filename match (log a warning, don't hard-fail) — in
     practice the camera pairs these consistently.
4. **Prefer the MP4** as the captioning source once verified.
5. **Both files get renamed together** — even though only the MP4 was used
   for captioning, both the RAW and proxy must stay in sync under the same
   new name so they continue to travel together (e.g. for later grading).

**Edge case to decide:** what happens if one file of a pair is later deleted
between dry-run and rename-only phases (e.g. someone deletes the MP4 to save
space)? This should be a distinct case from "the whole clip is missing" —
recommend treating it as a warning + skip, not a hard stop for the whole batch.

## Two-Phase Workflow: Dry-Run → Rename

Rationale: decouple the slow/expensive step (decode + inference) from the
destructive step (renaming real footage), with a human review checkpoint
in between.

### Phase 1: `--dry-run`

1. Run the full pipeline (extraction → captioning) as normal.
2. Do **not** rename source files.
3. Write the captioned JPEG to a review folder, using the **proposed new
   filename** — so captions can be visually sanity-checked against the frame.
4. Write a mapping file, `mappings.json` (JSON preferred over YAML — no extra
   dependency, easily diffable/greppable). Structure is a **list of groups**,
   not a flat old→new dict, since a MOV/MP4 pair maps to one shared new stem:

```json
[
  {
    "original_files": [
      "A017_C010_0806GC AMBIENCE-SEASIDE - Long Wharf - Boston, MA.MOV",
      "A017_C010_0806GC AMBIENCE-SEASIDE - Long Wharf - Boston, MA.MP4"
    ],
    "new_stem": "A017_C010_0806GC AMBIENCE-SEASIDE - Long Wharf - Boston, MA [waves crashing on rocky shore]",
    "preview_jpeg": "review/A017_C010_0806GC ... [waves crashing on rocky shore].jpg",
    "source_used_for_caption": "...MP4"
  }
]
```

5. **The mapping file is meant to be hand-edited** between phases — this is
   the actual point of the two-phase design, so bad captions can be corrected
   before anything is renamed. Document this explicitly as the intended
   workflow.

### Phase 2: `--rename-only --rename-mappings=mappings.json`

1. Skip extraction/captioning entirely — load the JSON directly.
2. **Pre-flight check before renaming anything:**
   - Confirm every file in every group still exists on disk (may have moved
     or been deleted since dry-run).
   - Check for new-name collisions.
   - Report all problems up front, rather than failing partway through.
3. Prompt for confirmation before executing (e.g. "42 rename operations, 3
   files missing since dry-run — continue? [y/N]"), with a `--yes`/`-y` flag
   to skip the prompt for scripted use.
4. **Log renames incrementally as they happen** (not just at the end) so a
   mid-batch crash (disk full, permissions, locked file) leaves a clear
   record of what already succeeded.
5. On completion, write an audit trail — e.g. rename `mappings.json` to
   `mappings.applied.<timestamp>.json` — so a batch can be reversed later
   if needed.

## Known Design Gaps to Resolve Before Building

These were identified as open questions, not yet decided:

1. **Re-running `--dry-run` on the same folder** — overwrite, append, or
   error? Matters for iterating on prompt wording across multiple attempts.
2. **Caption collisions** — two clips generating the same `new_stem`. Needs
   either an auto-disambiguation suffix (`_2`, `_3`) or a hard stop requiring
   manual resolution.
3. **Partial pair on rename** — one file of a MOV/MP4 pair missing at
   rename-only time. Decide: hard stop, warning + skip, or partial rename.
4. **Filename length limits** — long camera reel names + location + verbose
   caption could hit macOS/APFS's 255-byte filename component cap. Needs
   truncation logic or a fallback strategy.
5. **Dry-run diffing** — no current way to see what changed between two
   `--dry-run` attempts without manually diffing JSON files or the review
   folder.

## Language / Packaging

**Python**, packaged with `uv`.

- `mlx-vlm` is a Python package — no reason to fight the ecosystem.
- Rest of the pipeline (subprocess orchestration for `ffmpeg`/`qlmanage`/`sips`,
  file walking, JSON I/O) is squarely in Python's comfort zone.
- **Packaging approach: proper installable CLI via `uv tool install`**, not
  just a single PEP 723 script — this will be run repeatedly against new
  camera dumps, so it should feel like a real installed command.

```
slate/
├── pyproject.toml
├── CLAUDE.md              # project conventions for Claude Code sessions
├── PROJECT_SPEC.md         # this file
├── src/
│   └── slate/
│       ├── __init__.py
│       └── cli.py
```

```toml
[project]
name = "slate"
version = "0.1.0"
dependencies = ["mlx-vlm", "typer", "rich"]

[project.scripts]
slate = "slate.cli:app"
```

```bash
uv tool install .
# then callable anywhere as:
slate --input-dir ~/Footage --dry-run
```

Suggested CLI libraries: `typer` (CLI framework, `--help`, arg parsing),
`rich` (progress bars/output formatting for long batch jobs over many clips).

## Naming

**Decided: Slate** (`slate`) — short, clean CLI command name, nods to the
clapperboard/slate used to mark takes on set.

Other candidates considered and set aside: Shot List, Clip Notes, Dailies,
Roll Call, Rewrap, Recap, Reel Mark.

## Next Steps

1. Resolve the five open design gaps above.
2. Scaffold the `uv` project structure.
3. Build Phase 1 (`--dry-run`) first — extraction, MOV/MP4 pairing logic,
   captioning, mapping file output.
4. Build Phase 2 (`--rename-only`) — pre-flight checks, confirmation,
   incremental logging, audit trail.
5. Test against a real sample folder of footage before running on a full
   camera dump.
