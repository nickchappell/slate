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
- Configurable via the `model` key in `config.toml` (see Configuration,
  below) — takes any Hugging Face repo ID that mlx-vlm/huggingface_hub can
  resolve, not just the models listed here.
- Reasonable fallback alternatives if quality/speed needs shift later:
  - **Smaller/faster, purpose-built for short captions:**
    `vikhyatk/moondream2` — this is the canonical Moondream2 repo, not a
    confirmed mlx-community quantized build; verify an MLX-compatible
    revision/quant exists before switching to it.
  - **Larger, better fine-grained accuracy:**
    `mlx-community/Qwen2.5-VL-7B-Instruct-4bit` — ~5GB, slower.

**Key finding:** VLM inference time depends on the *input JPEG size fed to the
model*, not the source video resolution — the model resizes internally to its
native processing resolution regardless of whether the source was 6K RAW or
1080p. This means **6K ProResRAW does not slow down the captioning step** —
it only affects the frame-extraction step (see below).

## Model Caching

`slate` does not implement its own model cache — `mlx-vlm` resolves model
IDs like `mlx-community/Qwen2-VL-2B-Instruct-4bit` through `huggingface_hub`,
which has its own standard, well-established local cache convention. No
bespoke caching logic is needed; this section just documents the behavior
so it isn't a surprise at first run.

- **Default location:** `~/.cache/huggingface/hub/`, content-addressed per
  repo (e.g. `models--mlx-community--Qwen2-VL-2B-Instruct-4bit/`, with
  `blobs/`/`refs/`/`snapshots/` inside). Shared machine-wide with any other
  HF-based tool (LM Studio, other mlx-vlm scripts, `transformers`, etc.) —
  downloading a model once benefits every tool that references the same
  repo ID, rather than `slate` keeping a private copy.
- **Relevant environment variables** (standard `huggingface_hub` behavior,
  not `slate`-specific):
  - `HF_HOME` — base dir for all HF state (default `~/.cache/huggingface`).
  - `HF_HUB_CACHE` — the model/repo cache specifically (default
    `$HF_HOME/hub`). Users can override this to point at a different disk
    (e.g. an external SSD) if they'd rather not put multi-GB weights on
    their boot volume.
  - `HF_HUB_OFFLINE=1` — skips the network check for newer revisions and
    uses the cache as-is; useful once a model is already downloaded, since
    it removes a network dependency from what is otherwise a fully local
    pipeline.
- **One-time exception to "no cloud dependency":** the project's stated
  design goal (see Overview) is no cloud dependency, but the *first* run
  with a given model requires network access to download its weights
  (~1.5GB for the chosen 2B-4bit model). Every run after that is fully
  local. Worth surfacing in the CLI (e.g. a log line on first download) so
  it reads as an expected one-time step, not a violation of the "no cloud"
  premise.
- **Disk usage compounds across models:** switching models (e.g. trying
  the Moondream2 or Qwen2.5-VL-7B fallback) does not clean up the
  previously-downloaded one — each lives as a separate cache entry
  (~1.5GB–5GB apiece). Not a problem worth solving in `slate` itself (the
  standard `huggingface-cli delete-cache` / `huggingface_hub` cache
  management tools already cover this), just worth knowing about if
  experimenting with multiple models during development.

## Frame Extraction Strategy

Operates on a single **selected source file** — whichever file the File
Pairing & Source Selection Logic (below) decided to use for a given clip.
Decode-method-agnostic: it doesn't matter whether the source file is a
`.MOV`, `.MP4`, or something else — the same three-step ladder applies to
whatever file it's handed.

**Step 1 — try `ffmpeg` directly.** This succeeds for H.264/H.265 (the MP4
proxy) and regular ProRes (via VideoToolbox), and fails for ProRes RAW and
camera-manufacturer RAW formats ffmpeg has no decoder for (X-OCN, N-RAW,
Canon RAW/Cinema RAW Light, etc. — see the caveat on plain ProRes below).
Detection is **attempt, not predict**: run the real extraction command and
check its exit status / whether it produced output, rather than
maintaining a static list of "known good" codecs — that list would need
constant upkeep as new camera RAW formats show up, where a live
attempt-and-fallback doesn't.
- Seek before decode (`-ss` before `-i`) for speed
- Downscale during extraction, not after (avoid full-res intermediate)
- Consider sampling 2–3 frames across the clip (not just frame 0) for more
  robust captions on clips with significant visual change (e.g. a pan)

```bash
ffmpeg -ss 00:00:01 -i input.mp4 -vframes 1 -vf scale=896:-1 frame.jpg
```

**Optional hardware-accelerated variant for regular ProRes (not RAW):**
```bash
ffmpeg -hwaccel videotoolbox -i input.mov -vf scale=896:-1 -vframes 1 frame.jpg
```

> **Note on "ProRes" vs. "ProRes RAW":** per the Platform Constraint
> section above, **regular ProRes decodes fine via ffmpeg/VideoToolbox —
> only ProRes RAW does not.** If Step 1 should be skipped for plain ProRes
> too, that's a change from what's documented earlier in this spec and
> needs to be confirmed explicitly, not assumed from a passing mention.

**Step 2 — if `ffmpeg` fails, fall back to `qlmanage`**, which uses
Apple's native frameworks (AVFoundation, or a vendor's own installed
QuickLook plugin — see Extensibility note below) and so can decode formats
ffmpeg can't, including ProRes RAW and other manufacturer RAW formats:

```bash
qlmanage -t -s 1024 -o /tmp A017_C015_0806GQ.mov
```

`qlmanage -t` always outputs **PNG**, regardless of the requested output name.
Convert to JPEG afterward with `sips` (no extra dependency):

```bash
sips -s format jpeg /tmp/A017_C015_0806GQ.mov.png --out /tmp/frame.jpg
```

**Step 3 — if `qlmanage` also fails** (no output produced, e.g. no
QuickLook generator registered for that file's type): this clip cannot be
processed. **Print an error naming the file, mark its group as errored in
`mappings.json`** (see the `status` field in Phase 1's schema, below),
**and move on to the next file** — this must not halt the rest of the
batch, consistent with how partial failures are already handled elsewhere
in this doc (Phase 2 pre-flight, the MOV/MP4 pair-deletion edge case).

**Extensibility note:** `qlmanage` does no decoding of its own — it just
triggers whatever QuickLook generator/plugin macOS has registered for a
file's type, which for ProRes RAW happens to be Apple's own AVFoundation.
The same fallback path works unmodified for other camera-manufacturer RAW
formats ffmpeg can't read (e.g. RED `.r3d`, Blackmagic `.braw`, or the
X-OCN/N-RAW/Canon RAW formats mentioned above), *if* the user has the
relevant vendor's QuickLook plugin installed — `slate` wouldn't need any
format-specific code, just a file routed through the same `qlmanage`/`sips`
path. The current Source Footage Format section above still scopes this
project to a single camera's `.MOV`/`.MP4` pairs; this note just explains
why Step 2/Step 3 generalize cleanly if that scope ever widens. Two
caveats carry over from the ProRes RAW case: `qlmanage -t` has no seek
control (always grabs a fixed/poster frame, unlike ffmpeg's `-ss`), and
thumbnail fidelity depends entirely on the vendor's plugin implementation,
not on `slate`.

## MOV/MP4 Pairing Logic (File Pairing & Source Selection)

Decides, for each clip, which **one** file gets handed to the Frame
Extraction Strategy above as "the selected source file." This is a
selection step only — it doesn't itself decode anything.

1. **Group files by shared base filename (stem).** For each file
   encountered, check whether another file with the same stem but a
   different extension also exists.
2. **If only one file exists in the group** → that file is the selected
   source file. No verification needed; go straight to Frame Extraction
   Strategy.
3. **If a pair exists** (e.g. `.MOV` + `.MP4`) → verify they're the same
   recording before trusting either as a stand-in for the other:
   - Compare `duration` via `ffprobe` (±0.1s tolerance), or — if duration
     isn't reliably available — compare frame count (`nb_frames`) with a
     ±1-frame tolerance (container-reported frame counts are sometimes
     estimated/rounded by the muxer, so an exact match isn't guaranteed
     even for genuinely identical footage). This reads container metadata
     only — it does not require decoding ProRes RAW, so it's cheap even on
     the RAW file.
   - **If verified as matching** → select the **smaller file by size**
     (`os.path.getsize`, not a hardcoded extension check) as the source
     file. For this camera's footage that's the MP4 proxy in practice, but
     phrasing the rule as "smaller file" rather than "the MP4" means the
     same logic doesn't need rewriting if a future pairing has different
     extensions.
   - If ffprobe can't read stream info from one file at all (e.g. can't
     open the RAW file's container), fall back to trusting the filename
     match — log a warning, don't hard-fail — in practice the camera pairs
     these consistently.
4. **Both files get renamed together** regardless of which was selected as
   the source — even though only one was used for captioning, both the RAW
   and proxy must stay in sync under the same new name so they continue to
   travel together (e.g. for later grading).

**Open design gap — verified mismatch:** step 3 above defines what happens
when duration/frame-count *can't* be checked (fall back to filename trust)
and when they're checked and *match* (pick the smaller file). It does not
yet define what happens when they're checked and **don't** match — i.e.
ffprobe successfully reads both files, but duration and frame count both
fall outside their tolerances. That's a real signal the two files aren't
actually the same recording (stale proxy, mismatched rename, etc.), not
just "can't verify." Needs a decision: hard stop for that group, warning +
treat as two independent single-file groups, or something else — not yet
specified.

**Edge case to decide:** what happens if one file of a pair is later deleted
between dry-run and rename-only phases (e.g. someone deletes the MP4 to save
space)? This should be a distinct case from "the whole clip is missing" —
recommend treating it as a warning + skip, not a hard stop for the whole batch.

## Filename Assembly

Controls how the final `new_stem` (the value written to `mappings.json` and
ultimately used for the rename, per the MOV/MP4 Pairing Logic above) is
built from the original filename stem, the generated caption, and optional
user-supplied text. This logic runs during Phase 1 (`--dry-run`) and Phase
3 (`--process-and-rename`) — anywhere a caption is generated.

**Caption position — `--prepend-generated-name` / `--append-generated-name`**
(mutually exclusive; **default: append**, matching the existing example
elsewhere in this doc):
- `--append-generated-name` (default): `<original_stem> <caption>`
- `--prepend-generated-name`: `<caption> <original_stem>`
- Passing both is a usage error — fail fast with a clear message rather
  than silently picking one.

**User-supplied wrapping — `--prefix TEXT` / `--suffix TEXT`** (independent
of each other and of caption position; **default: empty for both**). Use
case: text known ahead of time that isn't worth having the VLM infer, e.g.
a shoot's city/state/country when every clip in the batch shares one
location. These wrap the *entire* assembled name from the step above —
prefix goes at the very front, suffix at the very end, regardless of
whether the caption itself was prepended or appended:

```
<prefix> <original_stem> <caption> <suffix>     (append, default)
<prefix> <caption> <original_stem> <suffix>     (prepend)
```

**Spacing rule:** segments are joined with a single literal space, and any
segment that's empty (unset prefix/suffix) is **omitted entirely** rather
than contributing a stray double space — this is what keeps the pieces
from bumping together (`name1name2name3`) while also not leaving
leading/trailing whitespace in the filename when `--prefix`/`--suffix`
aren't used. `--prefix`/`--suffix` values are trimmed of leading/trailing
whitespace before joining, so accidental extra spaces in user input don't
produce doubled spaces in the result.

**Worked example**, extending the pairing-logic example above with
`--prefix "Boston, MA"`:
```
Boston, MA A017_C010_0806GC AMBIENCE-SEASIDE - Long Wharf - Boston, MA [waves crashing on rocky shore]
```

**Length limit and truncation — `max_file_name_length` (config only, no
CLI flag):** set via `config.toml` (see Configuration, below); default is
255, matching APFS's per-path-component limit. Applies to the fully
assembled `new_stem` from the steps above — i.e. after caption
prepend/append *and* prefix/suffix are applied, since prefix/suffix are
exactly what make hitting this limit more likely with a long reel name +
location + verbose caption combined. Not applied to `new_stem` +
extension — the `.MOV`/`.MP4` extensions are equal length (4 characters)
in this dataset, so scoping the limit to the shared stem doesn't produce
different results between the pair.

**Defined behavior:** if the assembled `new_stem` is longer than
`max_file_name_length`, truncate characters off the end until it is **one
character shorter than** `max_file_name_length` — not equal to it. This
is deliberate headroom, not an off-by-one bug: implement it literally as
specified (`stem[: max_file_name_length - 1]` when `len(stem) >
max_file_name_length`, otherwise unchanged).

**Known caveat, not yet resolved:** `max_file_name_length` and the
truncation rule count *characters*, but APFS's actual 255 limit is
*UTF-8 bytes*. For reel names/locations/captions containing multi-byte
characters (accented letters, non-Latin scripts, emoji), a
character-count truncation could still produce a name that exceeds the
real filesystem limit. Character-counting was chosen for simplicity; byte
aware truncation is a possible future refinement if this proves to matter
in practice.

## Workflow Modes: Dry-Run → Rename, or Combined

Rationale: decouple the slow/expensive step (decode + inference) from the
destructive step (renaming real footage), with a human review checkpoint
in between. A third, combined mode is available for trusted/repeat runs
that skips the review checkpoint — see Phase 3 below.

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
    "status": "ok",
    "original_files": [
      "A017_C010_0806GC AMBIENCE-SEASIDE - Long Wharf - Boston, MA.MOV",
      "A017_C010_0806GC AMBIENCE-SEASIDE - Long Wharf - Boston, MA.MP4"
    ],
    "new_stem": "A017_C010_0806GC AMBIENCE-SEASIDE - Long Wharf - Boston, MA [waves crashing on rocky shore]",
    "preview_jpeg": "review/A017_C010_0806GC ... [waves crashing on rocky shore].jpg",
    "source_used_for_caption": "...MP4"
  },
  {
    "status": "error",
    "original_files": [
      "A017_C020_0806XX.MOV"
    ],
    "error": "ffmpeg and qlmanage both failed to decode a frame"
  }
]
```

Every group has a `status` of `"ok"` or `"error"`. An `"error"` group is
what Step 3 of Frame Extraction Strategy writes when neither `ffmpeg` nor
`qlmanage` could produce a frame for that file — it has no `new_stem` or
`preview_jpeg` (nothing was generated) and carries an `error` string
instead, so it's visible in the mapping file rather than silently dropped.

5. **The mapping file is meant to be hand-edited** between phases — this is
   the actual point of the two-phase design, so bad captions can be corrected
   before anything is renamed. Document this explicitly as the intended
   workflow.

### Phase 2: `--rename-only --rename-mappings=mappings.json`

1. Skip extraction/captioning entirely — load the JSON directly.
2. **Pre-flight check before renaming anything:**
   - Skip `"error"` groups entirely — there's no `new_stem` to rename to.
     Report their count (e.g. "3 groups skipped due to earlier extraction
     errors") so they stay visible without blocking the rest of the batch.
   - Confirm every file in every remaining (`"ok"`) group still exists on
     disk (may have moved or been deleted since dry-run).
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

### Phase 3: `--process-and-rename`

Runs Phase 1's extraction/captioning and Phase 2's renaming in a single
invocation, skipping the pause for hand-editing `mappings.json`.

**Tradeoff, stated explicitly:** this bypasses the human caption-review
checkpoint that is the stated rationale for the two-phase split. It's
intended for batches where the prompt/model quality has already been
validated (e.g. via `--dry-run` on a sample of the footage set), not as
the default way to run the tool. Not recommended for a camera dump you
haven't captioned with this model/prompt before.

To keep the risk bounded, Phase 3 must **not** skip the safety mechanics
from Phase 2 — only the manual review pause:
1. Run extraction → captioning as in Phase 1, writing `mappings.json` and
   the preview JPEGs to the review folder (unchanged, for audit purposes
   even though nothing pauses on them).
2. Immediately run Phase 2's pre-flight checks (missing files, name
   collisions), confirmation prompt (with `--yes`/`-y` to skip, same as
   Phase 2), incremental rename logging, and the
   `mappings.applied.<timestamp>.json` audit trail — all unchanged from
   Phase 2.
3. The only thing removed relative to running Phase 1 then Phase 2
   back-to-back is the opportunity to edit `mappings.json` between them.

### Undo Script: on by default, `--skip-generate-undo-script` to disable

Applies to both Phase 2 and Phase 3 — anywhere a rename batch is actually
executed. Complements the `mappings.applied.<timestamp>.json` audit trail
with a directly-runnable reversal: a plain shell script requires no
re-parsing of JSON and no reimplementation of rename logic to undo a
batch, which matters if the JSON structure ever changes or `slate` itself
is unavailable.

1. **Generated by default** after every successful rename batch in Phase 2
   or Phase 3 — no flag needed. Pass `--skip-generate-undo-script` to
   suppress it (e.g. for scripted/CI-like runs that manage their own
   reversal strategy). Defaulting to on matches the audit trail's stated
   purpose ("so a batch can be reversed later if needed") — a safety net
   only works if it doesn't depend on being remembered.
2. Written as `mappings.applied.<timestamp>.undo.sh` alongside the
   corresponding `mappings.applied.<timestamp>.json` — the shared
   timestamp keeps the two files correlated.
3. **Only include renames that actually succeeded**, per the incremental
   rename log from step 4 of Phase 2 — a batch that crashes partway
   through should produce an undo script that correctly reverses only
   what was actually applied, not the full planned batch.
4. Content is intentionally simple — one `mv` per file, reversed:
   ```bash
   #!/usr/bin/env bash
   set -euo pipefail
   mv -n -- "new_name.MOV" "old_name.MOV"
   mv -n -- "new_name.MP4" "old_name.MP4"
   ```
   - **Quote every path** (e.g. via Python's `shlex.quote`) — filenames in
     this dataset routinely contain spaces and commas (see the
     `mappings.json` example above), so naive unquoted `mv` lines would
     break or, worse, misparse as extra arguments.
   - Use `mv -n` (no-clobber) rather than plain `mv`: if a file matching
     the old name already exists at undo time (e.g. the user manually
     recreated it), fail that line loudly rather than silently
     overwriting it.
   - Write the file with executable permissions (`chmod +x`) so it's
     immediately runnable.
5. Undo operates per-file, not per-group — each original file (both the
   `.MOV` and `.MP4` of a pair) gets its own `mv` line, since that's the
   level renames are actually logged at in step 4 of Phase 2.

## Known Design Gaps to Resolve Before Building

These were identified as open questions, not yet decided:

1. **Re-running `--dry-run` on the same folder** — overwrite, append, or
   error? Matters for iterating on prompt wording across multiple attempts.
2. **Caption collisions** — two clips generating the same `new_stem`. Needs
   either an auto-disambiguation suffix (`_2`, `_3`) or a hard stop requiring
   manual resolution.
3. **Partial pair on rename** — one file of a MOV/MP4 pair missing at
   rename-only time. Decide: hard stop, warning + skip, or partial rename.
4. ~~**Filename length limits**~~ — resolved via `max_file_name_length` in
   Filename Assembly, above (default 255, truncates to one character under
   the limit). Narrower open question remains: that config value counts
   characters, not the UTF-8 bytes APFS actually limits to — see the
   "Known caveat" note in that section.
5. **Dry-run diffing** — no current way to see what changed between two
   `--dry-run` attempts without manually diffing JSON files or the review
   folder.
6. **`--process-and-rename` confirmation** — since this mode has no review
   checkpoint at all, should its confirmation prompt require something
   stronger than Phase 2's (e.g. echoing the count of clips and requiring
   `--yes` to be explicit, never inferred), to avoid it becoming the
   accidental default habit for a destructive, unreviewed rename?

## Configuration

**Location:** `~/.config/slate/config.toml`, hardcoded as the default in
the Python code — deliberately not computed from `$XDG_CONFIG_HOME` or the
full XDG Base Directory spec, since only one override mechanism is needed
here and adding more would be unused complexity.

**Resolution order:**
1. `SLATE_CONFIG` env var, if set — used as the config file path verbatim.
2. Otherwise, `~/.config/slate/config.toml`.

If `SLATE_CONFIG` is set but points to a path that doesn't exist, that's a
hard error, not a silent fallback to the default location — the user set
it explicitly, so silently ignoring it would hide what's probably a typo.
By contrast, the *default* location not existing is fine: `slate` just
runs on built-in defaults, no error, no requirement that a config file
exists at all.

**Precedence** when a value could come from more than one place: **CLI
flags > config file > built-in defaults.** The config file exists purely
to avoid retyping the same flags across runs; it never becomes mandatory.

**Format:** TOML — matches `pyproject.toml`, human-editable with comments,
unlike `mappings.json`/`mappings.applied.*.json` which are machine-written
and meant to be diffed/greppable rather than hand-authored from scratch.

**Example:** see `config.example.toml` at the repo root — persists some of
the defaults discussed in Filename Assembly, the Undo Script section, and
Model / Inference (model selection) so they don't need to be re-passed
every run. Copy it to `~/.config/slate/config.toml` (or wherever
`SLATE_CONFIG` points) to use
it as a starting point.

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
├── config.example.toml    # sample config; see "Configuration" above
├── src/
│   └── slate/
│       ├── __init__.py
│       ├── cli.py
│       ├── config.py      # config file resolution/parsing (Configuration, above)
│       └── filenames.py   # filename assembly + truncation (Filename Assembly, above)
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
slate --input-dir ~/Footage --rename-only --rename-mappings=mappings.json
slate --input-dir ~/Footage --process-and-rename
# undo script is written by default; opt out with:
slate --input-dir ~/Footage --process-and-rename --skip-generate-undo-script
# caption prepended instead of appended, with a known location as prefix:
slate --input-dir ~/Footage --dry-run --prepend-generated-name --prefix "Boston, MA"
```

Suggested CLI libraries: `typer` (CLI framework, `--help`, arg parsing),
`rich` (progress bars/output formatting for long batch jobs over many clips).

## Naming

**Decided: Slate** (`slate`) — short, clean CLI command name, nods to the
clapperboard/slate used to mark takes on set.

Other candidates considered and set aside: Shot List, Clip Notes, Dailies,
Roll Call, Rewrap, Recap, Reel Mark.

## Next Steps

1. Resolve the six open design gaps above.
2. Scaffold the `uv` project structure.
3. Build Phase 1 (`--dry-run`) first — extraction, MOV/MP4 pairing logic,
   captioning, filename assembly (caption position, prefix/suffix), mapping
   file output.
4. Build Phase 2 (`--rename-only`) — pre-flight checks, confirmation,
   incremental logging, audit trail, undo script generation (default on,
   `--skip-generate-undo-script` to disable).
5. Build Phase 3 (`--process-and-rename`) by composing Phases 1 and 2 —
   should be a thin wrapper, not new logic, once both phases exist.
6. Test against a real sample folder of footage before running on a full
   camera dump.
