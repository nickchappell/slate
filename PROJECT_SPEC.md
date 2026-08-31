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
  with a clear error rather than a cryptic MLX import failure. See
  Preflight Checks, below, for the full concrete startup check this implies.

## Preflight Checks

Run once, at the very start of **every** invocation — `--dry-run`,
`--rename-only`, and `--process-and-rename` alike — before any inference or
file renaming happens. Rationale: fail fast with one specific, actionable
error message per problem, rather than surfacing a confusing downstream
failure (a cryptic MLX import error, or a `FileNotFoundError` from a
`subprocess.run` call halfway through a batch) after the tool has already
started doing real work.

**Run every check and report all failures together**, rather than stopping
at the first one — same "report all problems up front" principle already
used for Phase 2's pre-flight (missing files, name collisions). Knowing
about a missing `ffmpeg` *and* a missing `qlmanage` in one pass is more
useful than fixing one, re-running, and discovering the next.

1. **Running on macOS** — `platform.system() == "Darwin"`. Checked first,
   since every other check below is meaningless on a non-Mac platform.
2. **Apple Silicon** — `platform.machine() == "arm64"`, per the Platform
   Constraint section above. An Intel Mac passes check 1 but fails here —
   worth a distinct error message ("slate requires Apple Silicon; MLX does
   not support Intel Macs") rather than reusing check 1's wording.
3. **`ffmpeg` on `PATH`** — `shutil.which("ffmpeg")` is not `None`.
   Third-party (typically Homebrew-installed), so its absence is the most
   likely of the four tool checks to actually trigger. Error message
   should suggest the fix (e.g. `brew install ffmpeg`).
4. **`ffprobe` on `PATH`** — `shutil.which("ffprobe")` is not `None`.
   Ships alongside `ffmpeg` in the same Homebrew package, but checked
   independently since it's a separate binary — a partial/corrupted
   install could plausibly have one without the other.
5. **`qlmanage` on `PATH`** — `shutil.which("qlmanage")` is not `None`.
   A macOS system binary (`/usr/bin/qlmanage` under normal circumstances),
   so this should always pass on a stock install; the check exists as a
   defensive guard against unusual environments (a minimal/managed
   corporate image, a stripped-down CI-style macOS runner) where it's been
   removed or `PATH` doesn't include the usual system directories.
6. **`sips` on `PATH`** — `shutil.which("sips")` is not `None`. Same
   rationale as `qlmanage`: a standard macOS system binary, checked
   defensively rather than because it's expected to actually fail.

**Scope note:** these checks only confirm the four binaries exist and are
executable — they say nothing about whether `ffmpeg`/`qlmanage` can decode
any *particular* clip's codec. That's a separate, per-file concern already
covered by the attempt-and-fallback ladder in Frame Extraction Strategy
(above): a clip-specific decode failure is expected, recoverable, and
handled by marking that one group `"error"` in `rename_mappings.json`, not by
failing the whole run. Preflight checks catch the case where the tool
can't function *at all*; the extraction ladder catches the case where one
clip can't be decoded even though the tools are present and working.

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
- Configurable via the `model` key in `config.toml`, or overridden per-run
  with `--model REPO_ID` (see Configuration, below, for precedence) — takes
  any Hugging Face repo ID that mlx-vlm/huggingface_hub can resolve, not
  just the models listed here.
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

**Caption prompt.** Instruction-tuned VLMs (Qwen2-VL included) default to
full descriptive sentences with scene-setting preamble ("In this image, we
can see...") unless explicitly steered away from it — a bare word-count
request ("in 5 to 8 words") is not enough on its own; it needs to be paired
with an explicit format constraint and a one-shot example to actually get
compliance. Default prompt text:

```
Describe the main subject and action in this frame in 5 to 8 words.
Respond with only a short lowercase phrase — no full sentence, no
punctuation, no preamble like "the image shows" or "this is a picture of".

Example output: waves crashing on rocky shore
```

- **Configurable via the `prompt` key in `config.toml`** (see
  Configuration, below) — **config-only, no CLI flag**, same rationale as
  `max_file_name_length`: a multi-line prompt isn't something worth
  retyping as a command-line argument on every invocation.
- **Word count is a soft target, not a guarantee** — no prompt wording
  makes an LLM/VLM reliably count words. The actual hard backstop is a
  generation-time **max token cap** (~20–25 tokens) on the model call
  itself, independent of the prompt text, so a model that ignores the
  instruction still can't produce a full paragraph. This is a fixed
  implementation detail, not currently exposed as a config option.
- This prompt text is the thing "iterating on prompt wording across
  multiple attempts" (mentioned when the dry-run re-run behavior was
  first speced out) refers to in practice — expect to revise it against
  real footage before settling on a final default.

**Caption normalization.** Raw model output is defensively normalized
before it ever reaches Filename Assembly — the prompt's formatting
requests (lowercase, no punctuation, one short phrase) are a soft target,
not a guarantee, same reasoning as the max-token backstop above. Applied
in this order:
1. **Replace filesystem-unsafe characters with a space:** `/` (a single
   filename path component can never contain one — the OS reads it as a
   path separator, which would turn an otherwise-valid rename into a hard
   `os.rename` failure) and the NUL byte `\0` (terminates a C-style string
   at the OS/syscall level). Extremely unlikely from real model output,
   but cheap to guard against so one degenerate caption can't break a
   rename mid-batch — consistent with this doc's general stance of
   isolating a single bad clip to its own `"error"` group rather than
   letting it disrupt the run. Done first, before whitespace collapsing
   below, so any space just introduced here gets cleaned up along with
   everything else.
2. Strip leading/trailing whitespace, and collapse any internal
   newlines/repeated whitespace to a single space — the prompt asks for
   one phrase, but nothing stops the model from wrapping output across
   lines.
3. Strip a single pair of surrounding quote characters (`"`/`'`), if
   present — models occasionally wrap their answer in quotes despite not
   being asked to.
4. Lowercase the entire string.
5. Strip trailing sentence-ending punctuation (`.`, `,`, `!`, `?`) —
   trailing only; punctuation elsewhere in the phrase is left alone.

**Explicitly out of scope:** the legacy Mac OS `:`/`/` display-translation
quirk (Finder shows a literal `:` typed by a user as-is, but historically
translated `/`↔`:` for compatibility with old HFS path syntax). `slate`
renames files directly via `os.rename`, never through Finder, so a literal
`:` in a caption is not a real risk — it's an ordinary, valid byte in an
APFS filename at the syscall level. Only `/` and NUL are actually
forbidden there, which is why they're the only two characters filtered in
step 1, above.

**Caption length cap.** After normalization above, the caption is
truncated to a maximum of **70 characters**, cutting at the last
whitespace boundary at or before position 70 (never mid-word), with no
ellipsis or other marker appended. This is a fixed cap, independent of
and applied *before* `max_file_name_length` (Filename Assembly, below) —
it exists so a caption that slips past the generation-time token backstop
(tokens ≠ characters, and a token cutoff can land mid-word) can't
dominate the whole filename budget on its own. Not currently a config
option — a fixed implementation detail, same as the max-token cap above.

**No bracket-wrapping.** Earlier worked examples throughout this doc
showed captions wrapped in `[brackets]` (e.g. `[waves crashing on rocky
shore]`) — that was illustrative formatting only, never a decided
behavior, and has been corrected throughout. The normalized caption is
inserted as **plain text**, relying entirely on Filename Assembly's
existing spacing rule (single-space joins, empty segments omitted) to
separate it from the rest of the assembled name.

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

## Startup Time

Importing `mlx_vlm` pulls in `transformers`, `mlx.core`, `httpx`, and the
rest of that dependency tree — about **0.9 s** of pure import cost on an
M-series machine, before a single frame is decoded. `slate`'s own modules
account for a few milliseconds combined, so compiling them (Cython, mypyc,
Nuitka) buys nothing; the heavyweight packages are already compiled C
extensions with nothing left to squeeze.

The fix is to **not import what a given run won't use**. `inference.py`
keeps the heavy names (`mlx_vlm.load` / `.generate` /
`prompt_utils.apply_chat_template` / `utils.load_config`, plus
`huggingface_hub.snapshot_download`) as module-level `None` placeholders and
populates them on first use via `_ensure_mlx_deps()` / `_ensure_hub_deps()`.
`cli.py` imports `inference` for its function names only — resolving those
names is cheap until something actually calls one. Result:

- `--version`, `--help`, a failed preflight, a usage error — no heavy
  import at all (~0.1 s end to end instead of ~1 s).
- `--rename-only` (Phase 2) never captions, so it never touches `mlx_vlm`
  or `huggingface_hub`.
- `--dry-run` / `--process-and-rename` still pay the cost, but only at the
  first caption — after preflight, config resolution, discovery, pairing,
  and the `slate … started` / file-list output have already printed, so
  the run *looks* responsive instead of stalling silently on launch.

Placeholders are declared at module scope (rather than imported inside each
function) specifically so tests can monkeypatch them without importing the
real packages — the unit suite runs without `mlx_vlm` loaded.

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
- **Single frame per clip, at a fixed timestamp** (`-ss 00:00:01` in the
  example below) — one `ffmpeg`/`qlmanage` extraction, one VLM call, one
  caption. An earlier version of this doc considered sampling 2–3 frames
  across the clip for more robust captions on clips with significant
  visual change (e.g. a pan), but that was dropped without being built:
  it either depends on `mlx-vlm`'s Qwen2-VL-2B wrapper reliably accepting
  multi-image input in one call (unconfirmed — Qwen2-VL's architecture
  supports it, but whether this specific quantized build + `mlx-vlm`'s
  API exposes it cleanly hasn't been tested), or requires captioning each
  sampled frame separately and then deciding how to pick/merge 2–3
  different resulting captions (unresolved, and 2–3x the inference cost
  per clip). Neither is worth building speculatively for a personal-use
  tool before real footage shows the single-frame approach actually
  producing bad captions on panning/motion-heavy clips.

```bash
ffmpeg -ss 00:00:01 -i input.mp4 -vframes 1 -vf scale=896:-1 frame.jpg
```

**Optional hardware-accelerated variant for regular ProRes (not RAW):**
```bash
ffmpeg -hwaccel videotoolbox -i input.mov -vf scale=896:-1 -vframes 1 frame.jpg
```

**Future design idea, not built — composite frame grid.** If single-frame
captions turn out to miss real motion/content changes within a clip
(panning shots being the obvious case), the simplest fix is *not* the
multi-image-API or multi-caption-merge approaches ruled out above — it's
extracting 2–3 frames as before, but **stitching them into one composite
image** (e.g. side-by-side or in a grid, via `ffmpeg`'s `montage`/`hstack`
filters or Pillow) and sending that single composite to the VLM. Still one
image, one VLM call, one caption, no merge logic, no dependency on
multi-image API support — because from the model's perspective it's just
a picture with several panels in it. Noted here as a future option, not
something to build until a single fixed-timestamp frame is actually shown
to produce bad captions on real footage.

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
`rename_mappings.json`** (see the `status` field in Phase 1's schema, below),
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

## Input Selection: `--input-dir` vs. `--input-files`

Two mutually exclusive ways to tell `slate` which files to operate on for a
given invocation. Applies to `--dry-run` and `--process-and-rename`
(anything that does file discovery); `--rename-only` doesn't discover files
at all — it operates purely on the `rename_mappings.json` it's given.

- **`--input-dir DIR`** (directory-scan mode): walks `DIR` for camera
  footage files, grouping by shared stem per the Pairing Logic below.
- **`--input-files FILE [FILE ...]`** (explicit-list mode): operates only on the
  exact filenames passed on the command line, e.g.:
  ```
  slate --input-files clip1.MOV clip1.MP4 clip2.MP4 clip3.MOV
  ```
  Accepts one or more files, in any order; mixing complete pairs and
  singles in the same invocation is fine, since stem-grouping (Pairing
  Logic step 1, below) still applies to whatever's in the list.

**Defined behavior — strictly scoped to the passed set, nothing
auto-discovered.** If a file's sibling exists on disk but wasn't included
in `--input-files`, `slate` does not go looking for it. E.g. passing only
`clip1.MOV` (and not `clip1.MP4`, even though it sits right next to it)
means clip1 is treated as a **single-file group** (Pairing Logic step 2) —
not a verified pair — so the MP4 is left completely untouched: not
compared, not renamed, not even considered for the `os.path.getsize`
check. This is deliberate, not an oversight: `--input-files` means "operate on
exactly this list, nothing implied," which matters both for
reproducibility (the same explicit list always touches the same files,
regardless of what else is sitting in the directory) and for deliberately
excluding one file from a batch without having to move it elsewhere first.

- **Mutually exclusive** with `--input-dir` — passing both is a usage
  error, same pattern as `--prepend-generated-name`/`--append-generated-name`.
- **Exactly one of the two is required** — specifying neither is also a
  usage error, not a default-to-cwd fallback.
- Paths may be relative or absolute, resolved the same as any other
  CLI-supplied path.

## Input Validation

Runs on the resolved input set (from `--input-dir` or `--input-files`) at
the very start of Phase 1, before pairing, frame extraction, or inference —
so junk never reaches the slow/expensive steps.

Two layers:

1. **Name filter (directory scans only).** `discover_input_dir` skips any
   entry whose name starts with `._`. Those are **AppleDouble sidecars**:
   macOS writes one next to every real file when a filesystem can't hold
   resource forks / extended attributes natively (exFAT, FAT, SMB) — e.g.
   after a QuickLook rotate on an exFAT camera card. They carry the video
   extension (`._A017_C015.MOV`) but are a few KB of metadata, and their
   stem (`._A017_C015`) doesn't match the real file's, so without this
   filter they sail through as lone single-file groups. `._` has exactly
   one meaning, so this one case is filtered by name rather than probed.

2. **`ffprobe` gate (both input modes).** `validate_media_files` runs one
   `ffprobe -show_format -show_streams` per file — a container header read,
   no frame decode, low-single-digit milliseconds against seconds per clip
   for extraction + VLM — in a small thread pool. A file is kept only if
   `ffprobe` can read it *and* it reports at least one `codec_type ==
   "video"` stream. Anything else is dropped with a per-file
   `output.warn` ("ffprobe could not read it" / "no video stream") and the
   run continues on what's left. This is the general net: AppleDouble
   files passed explicitly via `--input-files` (which bypass layer 1),
   zero-byte or truncated copies, and files whose extension lies about
   their contents.

Same spirit as Frame Extraction Strategy below — *attempt, not predict*:
run the real tool and check the result rather than maintaining a codec
allowlist. Known minor redundancy: a verified 2-file pair gets re-probed
in `verify_pair`; not worth threading the first probe's result through for
a personal-scale tool.

## MOV/MP4 Pairing Logic (File Pairing & Source Selection)

Decides, for each clip, which **one** file gets handed to the Frame
Extraction Strategy above as "the selected source file." This is a
selection step only — it doesn't itself decode anything.

1. **Group files by shared base filename (stem).** For each file
   encountered, check whether another file with the same stem but a
   different extension also exists — scoped entirely to the files provided
   for this run (the `--input-dir` scan or the explicit `--input-files` list; see
   Input Selection, above). `slate` never looks for a sibling outside that
   set — a file not passed in (`--input-files`) or not present under `DIR`
   (`--input-dir`) simply doesn't exist as far as this run is concerned.
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
   - **If verified as *not* matching** — ffprobe reads both files
     successfully, but duration and frame count both fall outside their
     tolerances — treat the group as **failed, not resolvable
     automatically**: write it to `rename_mappings.json` as an `"error"` group
     (same mechanism as Frame Extraction Strategy Step 3), with an `error`
     message that names the specific mismatch (e.g. "same-stem files
     `.MOV`/`.MP4` do not appear to be the same recording — durations
     differ by 4.2s" ), and move on to the next file. This is deliberately
     *not* auto-resolved (e.g. by guessing which file is "correct" or
     silently picking one) — a mismatch this specific means something is
     wrong with the pairing itself (stale proxy, mismatched rename from a
     prior batch, etc.), and that's for the user to reconcile by hand, not
     for `slate` to paper over.
4. **Both files get renamed together** regardless of which was selected as
   the source — even though only one was used for captioning, both the RAW
   and proxy must stay in sync under the same new name so they continue to
   travel together (e.g. for later grading). Does not apply to `"error"`
   groups from the mismatch case above — nothing gets renamed for those
   until the user resolves the mismatch and re-runs.

**Resolved — partial pair on rename:** if one file of a verified pair is
deleted between the dry-run and rename phases (e.g. the MP4 proxy removed
to save space), that's handled as **warning + skip** at Phase 2's
pre-flight check (see Phase 2 step 2, below) — a specific warning is
printed naming the group and the missing file, the surviving file is left
untouched, and the rest of the batch continues unaffected. Kept distinct
from "the whole clip is missing," which is skipped silently-but-reported
rather than warned about individually.

## Filename Assembly

Controls how the final `new_stem` (the value written to `rename_mappings.json` and
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
Boston, MA A017_C010_0806GC AMBIENCE-SEASIDE - Long Wharf - Boston, MA waves crashing on rocky shore
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
`max_file_name_length`, truncate the **caption portion specifically** —
never the original stem, and never `--prefix`/`--suffix` — regardless of
whether the caption was prepended or appended. Truncating blindly off the
end of the whole string (an earlier version of this rule) is **wrong**
for `--prepend-generated-name`: with that order (`<caption> <original_stem>
<suffix>`), trimming the tail eats into the original camera filename and
the suffix instead of the caption — exactly the parts that must never be
touched. Since assembly already knows exactly where the caption sits
within `new_stem` (it built the string), it shrinks from the end of the
caption's own span until the whole assembled stem is **one character
shorter than** `max_file_name_length` — not equal to it; deliberate
headroom, not an off-by-one bug.

This composes with the caption's fixed 70-character cap (Model /
Inference, above): that cap already keeps most captions well under
budget, so this path should rarely trigger in practice. It exists as a
correctness guarantee for the rare long-original-stem-plus-prefix-plus-
suffix combination, not as the primary length control.

- **Edge case:** if truncating the caption down to nothing (0 characters,
  omitted entirely per the spacing rule) still leaves the remaining
  `prefix`/`original_stem`/`suffix` combination longer than
  `max_file_name_length`, the *non-caption* portions alone already
  exceed the limit — a pathological input (an extremely long original
  camera filename, or an unusually long `--prefix`/`--suffix`), not
  something normal use is expected to hit. `slate` should surface this
  plainly (e.g. a warning that the assembled name still exceeds
  `max_file_name_length` even with no caption) rather than silently
  truncating into the stem/prefix/suffix as a hidden fallback.

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

1. Determine file groups (Input Selection + Input Validation + MOV/MP4
   Pairing Logic, above). Input Validation runs first: drop `._` AppleDouble
   sidecars and anything `ffprobe` can't read as video, with a warning per
   dropped file, before pairing.
2. **Re-run behavior — skip groups already present in an existing
   `rename_mappings.json`.** If a `rename_mappings.json` already exists at the output
   location from an earlier `--dry-run`, load it *before* doing any
   extraction/captioning work, and check each newly-determined group
   against it:
   - A group counts as "already there" if its `original_files` — the
     pair, or the single file — **set-matches** (order-independent) an
     existing entry's `original_files`.
   - This check applies **regardless of that existing entry's `status`**
     — an old `"error"` entry still counts as already processed, and is
     *not* automatically retried on the next run. If the underlying
     problem (e.g. a missing `ffmpeg`) has since been fixed, force a
     retry by deleting that entry from `rename_mappings.json` before re-running
     — re-running `--dry-run` does not diff error causes, just file sets.
   - For every matched group: **skip extraction/captioning entirely** (no
     `ffmpeg`/`qlmanage` call, no VLM inference), print a line noting the
     skip (e.g. `SKIP (already in rename_mappings.json): A017_C010_0806GC
     AMBIENCE-SEASIDE - Long Wharf - Boston, MA.MOV / .MP4`), and carry
     that entry into the output **unchanged** — its existing
     `new_stem`/`preview_jpeg`/`error` are preserved verbatim, not
     regenerated. This is what makes re-running `--dry-run` safe for
     hand-edited mapping files: since the skip check only looks at
     `original_files`, a caption a user has already hand-corrected (see
     step 8, below) survives a re-run untouched rather than being
     silently overwritten.
   - Any group with no match (new footage added since the last run, or no
     `rename_mappings.json` existed yet) proceeds to extraction/captioning as
     normal.
   - This resolves re-running `--dry-run` on the same folder as neither a
     hard overwrite-from-scratch nor a hard error: the output is the
     **union** of carried-over (skipped) groups and newly-processed ones.
3. Run extraction → captioning (Frame Extraction Strategy, above) for
   every group that wasn't skipped in step 2.
4. **Disambiguation pass — run once, after every `"ok"` group for this
   invocation has a computed `new_stem`** (the full set: carried-over
   groups from step 2 plus newly-processed ones from step 3, not just the
   latter). This camera's filenames are already unique on import
   (numeric/datecode-based — `IMG001`, `IMG002`, ...), so a collision here
   means two *different* clips' assembled names happened to land on the
   same output, not that the source names collided.
   - **Only triggers on a true output collision** — compares the
     **complete final file name** (`new_stem` + original extension, e.g.
     `....MOV`) across every file about to be written by every `"ok"`
     group, not `new_stem` alone. For this camera's pairs (which always
     share the same two extensions) that's equivalent to comparing
     `new_stem` directly, but scoping the check to the full file name
     keeps it correct if a mixed batch of single files with different
     extensions is ever in play.
   - Groups are processed in a **stable, deterministic order** (e.g.
     sorted by original filename) so re-running the same batch always
     assigns the same suffixes: the first group to claim a given final
     name keeps it unsuffixed; every later group that collides with it
     gets `_2`, `_3`, ... appended, in the order encountered.
   - The suffix is appended directly to `new_stem`. Both files of a pair
     get the identical, now-disambiguated stem (so they still travel
     together under Pairing Logic step 4) — e.g.
     `... waves crashing on rocky shore_2.MOV`.
   - **Interaction with `max_file_name_length`:** per-group truncation
     (Filename Assembly, above) already happened before this pass runs.
     If truncation itself is what caused two originally-different long
     names to collide (both clipped to the same value), this pass still
     catches and disambiguates them — a useful side effect, not just a
     guard against genuine duplicate captions. If appending a suffix
     pushes a name back over `max_file_name_length`, re-truncate the
     **base** portion (never the suffix) so the disambiguating suffix is
     always preserved — uniqueness takes priority over hitting the exact
     truncation length.
5. Do **not** rename source files.
6. Write the captioned JPEG to a review folder, using the **proposed new
   filename** — so captions can be visually sanity-checked against the frame.
   **Implementation note:** the extracted frame is held under a per-group
   tmp name (unique by construction — one per source stem) until *every*
   entry in the batch has run through Disambiguation (step 4) and gotten
   its final `new_stem`, then renamed into place. Two clips processed in
   the same run can coincidentally assemble to the identical name before
   disambiguation separates them (e.g. stem `"a"` + caption `"b c"` and
   stem `"a b"` + caption `"c"` both assemble to `"a b c"`) — writing
   straight to `<new_stem>.jpg` as each entry is processed would let the
   second one silently overwrite the first's preview file on disk.
7. Write a mapping file, `review/rename_mappings.json` — living inside
   `review/` itself, alongside the preview JPEGs it describes, not as a
   sibling of that folder (JSON preferred over YAML — no extra dependency,
   easily diffable/greppable). Top level is an object, not a bare list, so
   the mapping file's own format version can be stamped alongside the
   groups it contains (see "Mapping File Version Check," below):

```json
{
  "app_version": "0.2.2",
  "groups": [
    {
      "status": "ok",
      "original_files": [
        "A017_C010_0806GC AMBIENCE-SEASIDE - Long Wharf - Boston, MA.MOV",
        "A017_C010_0806GC AMBIENCE-SEASIDE - Long Wharf - Boston, MA.MP4"
      ],
      "new_stem": "A017_C010_0806GC AMBIENCE-SEASIDE - Long Wharf - Boston, MA waves crashing on rocky shore",
      "preview_jpeg": "A017_C010_0806GC ... waves crashing on rocky shore.jpg",
      "preview_jpeg_sha256": "b2c1...e4f0",
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
}
```

Every group has a `status` of `"ok"` or `"error"`. An `"error"` group has
no `new_stem` or `preview_jpeg` (nothing was generated) and carries an
`error` string instead, so it's visible in the mapping file rather than
silently dropped. Two distinct things produce an `"error"` group:
- Frame Extraction Strategy Step 3 — neither `ffmpeg` nor `qlmanage` could
  produce a frame for the selected source file.
- File Pairing & Source Selection Logic's verified-mismatch case — a
  same-stem `.MOV`/`.MP4` pair exists, but duration and frame count both
  disagree beyond tolerance, so `slate` can't tell which file (if either)
  is trustworthy. Left for the user to reconcile by hand rather than
  guessed at automatically.

8. **The mapping file is meant to be hand-edited** between phases — this is
   the actual point of the two-phase design, so bad captions can be corrected
   before anything is renamed. Document this explicitly as the intended
   workflow. (Step 2, above, is what makes this safe across a re-run: hand
   edits to an entry survive as long as `original_files` for that group is
   unchanged.)
   - **Reviewing by renaming the preview JPEG is the other supported path**,
     and the more intuitive one for a human: since step 6 already names the
     JPEG after the proposed `new_stem`, correcting a caption can mean
     renaming `review/waves crashing on rocky shore.jpg` to `review/sunset
     over the harbor.jpg` in Finder (or any file manager/batch-rename tool)
     instead of hand-editing JSON text. `preview_jpeg_sha256` (the frame's
     SHA-256, computed once right after step 6 writes the file) is what
     makes this reconcilable despite happening out of band of the script —
     a plain rename never touches file bytes, so the hash is a durable link
     back to the JSON entry regardless of what the file is named when
     `--rename-only` next runs (Phase 2 step 0, below). The two review
     paths compose: an entry a human never touched (JSON or JPEG) carries
     both forward unchanged; if only the JPEG was renamed, the JPEG's name
     wins as the source of truth for `new_stem`; if only the JSON's
     `new_stem` was hand-edited, the JPEG's hash still matches its
     (unrenamed) file, so there's nothing to reconcile and the hand-edit
     stands.
9. **Print a run-level summary** to the console once `rename_mappings.json` has
   been written, so re-running `--dry-run` on a folder gives an
   at-a-glance answer to "what did this run actually do?" without having
   to scroll back through individual `SKIP` lines or open the JSON:
   ```
   Summary:
     14 groups total
     9 newly processed
     4 skipped (already in rename_mappings.json)
     2 disambiguated (suffix appended to avoid a name collision)
     1 error (0 new, 1 carried over from a previous run)
   ```
   - **Newly processed** — groups that went through extraction/captioning
     this run (step 3, above).
   - **Skipped** — groups carried over unchanged from step 2, above.
   - **Disambiguated** — groups whose `new_stem` got a `_2`/`_3`/... suffix
     from the Disambiguation pass (step 4, above); called out separately
     since it's the one case where the written `new_stem` differs from
     what Filename Assembly alone would have produced.
   - **Errors** — split into newly-erred-this-run vs. carried over from an
     earlier run (same distinction as the `SKIP` case), since a fresh
     error is more likely to need immediate attention than one already
     seen and left unresolved.
   - **Scope note:** this is a count-level summary only — it does not
     attempt to show *what specifically changed* for any individual group
     (e.g. old caption text vs. new, for a group that was manually deleted
     from `rename_mappings.json` and reprocessed). That finer-grained diff was
     considered and deliberately left out of scope: between this summary
     and the `SKIP` lines already printed per group, the common case
     (adding new footage to a folder) is fully covered without it.

### Mapping File Version Check

Applies every time a mapping file is loaded — Phase 1 step 2's carried-over
lookup and Phase 2 step 0's `--rename-mappings` load both go through it,
before anything else happens.

`review/rename_mappings.json`'s `app_version` field (step 7, above) is
stamped from the currently-*running* `slate`'s own version
(`importlib.metadata.version("slate")`, which reads what the installed
package's build backend recorded from `pyproject.toml` — not a value the
caller supplies) every time the file is written, in both Phase 1 and
Phase 2. On load, that stamped version is compared against the running
version:

- **Different major version** (the first `X` in `X.Y.Z`) — refuse to
  proceed. The mapping file's *format* is what's actually at risk across a
  major bump, not just its data, so blindly trusting it could misinterpret
  fields that changed meaning or silently drop ones that were removed.
  Fail loudly with a colorized (`output.fatal`, red) message naming both
  versions and suggesting a fresh `--dry-run`, then exit non-zero —
  matching the existing preflight-check failure pattern
  (`_run_preflight_or_exit`), not a soft warning that's easy to miss.
- **Same major version** (minor/patch may differ freely) — proceed
  normally; no message.
- **No `app_version` at all** — a file that predates this field, or one
  hand-created without it — treated as compatible, not as an
  automatic mismatch. There's no version to compare against, and
  retroactively failing every already-existing mapping file the moment
  this check shipped would be worse than the problem it's meant to catch.
  (`load_mappings` itself also still reads the older bare-list file format
  — `[{...}, {...}]` instead of `{"app_version": ..., "groups": [...]}` —
  for the same reason.)

This is deliberately coarse (major version only, not minor/patch) since
`slate`'s own versioning already reserves major bumps for breaking changes
(see the release process in `CLAUDE.md`) — a minor/patch difference is
expected to keep reading old mapping files fine.

### Phase 2: `--rename-only --rename-mappings=review/rename_mappings.json`

0. **Sync `new_stem` from any renamed preview JPEGs first** (`review_sync.
   sync_from_review`, Phase 1 step 8's other review path) — before anything
   else in this phase runs:
   - Hash every `.jpg` in `review/` and match it against each `"ok"`
     entry's recorded `preview_jpeg_sha256`. A match whose current filename
     differs from the entry's `new_stem` means a human renamed it — update
     `new_stem`/`preview_jpeg` from the on-disk name. Entries whose JPEG
     hash isn't found in `review/` at all are left untouched but excluded
     from this run's rename plan and reported (e.g. `WARNING: skipping
     rename for "...": preview JPEG no longer found in review/ (deleted?)`)
     — deleting a preview JPEG is how a human says "don't rename this one,"
     without needing to hand-edit the JSON.
   - Entries with no recorded `preview_jpeg_sha256` (mapping files written
     before this field existed) are skipped by sync entirely — same
     behavior as today, hand-editing the JSON is still fully supported.
   - If two entries' preview JPEGs are byte-identical (rare — e.g. two
     genuinely identical frames), a hash match is ambiguous: warn and leave
     both untouched rather than guess which one a renamed file belongs to.
   - If the sync step changed anything, `rename_mappings.json` is
     rewritten immediately (before the rest of Phase 2 runs) so the audit
     trail written at the end of this phase reflects what was actually
     applied, not the pre-sync state.
   - Two humans-renamed JPEGs landing on the same target name doesn't need
     special handling here — it's caught by the ordinary destination
     collision check in step 2 below, same as any other collision.
1. Skip extraction/captioning entirely — load the JSON directly.
2. **Pre-flight check before renaming anything:**
   - Skip `"error"` groups entirely — there's no `new_stem` to rename to.
     Report their count (e.g. "3 groups skipped due to earlier extraction
     errors") so they stay visible without blocking the rest of the batch.
   - Confirm every file in every remaining (`"ok"`) group still exists on
     disk (may have moved or been deleted since dry-run). Two distinct
     outcomes, handled differently:
     - **Whole group missing** (every file in the group is gone) — skip
       the group, report it (e.g. "1 group skipped: no files found on
       disk for ..."), and continue with the rest of the batch.
     - **Partial pair missing** — a verified pair from dry-run (e.g.
       `.MOV` + `.MP4`) where exactly one file still exists and the other
       has been deleted since (commonly the MP4 proxy, removed to save
       space) — **warning + skip, not a hard stop.** Print a specific
       warning naming the group and the missing file, e.g.:
       ```
       WARNING: skipping rename for "A017_C010_0806GC ...": paired file
       A017_C010_0806GC ....MP4 no longer exists on disk (deleted since
       dry-run?) — resolve manually and re-run.
       ```
       Leave the surviving file untouched (do **not** rename it alone) —
       a pair's `original_files` must never silently split into "one
       renamed, one not." Continue processing the rest of the batch;
       this must not halt on its own.
   - Check for new-name collisions.
   - Report all problems up front, rather than failing partway through.
3. Prompt for confirmation before executing (e.g. "42 rename operations, 3
   files missing since dry-run — continue? [y/N]"), with a `--yes`/`-y` flag
   to skip the prompt for scripted use.
4. **Log renames incrementally as they happen** (not just at the end) so a
   mid-batch crash (disk full, permissions, locked file) leaves a clear
   record of what already succeeded.
5. On completion, write an audit trail — rename `rename_mappings.json` in
   place, to `applied_renames_<timestamp>.json` within the same `review/`
   directory it already lived in (alongside the preview JPEGs, since both
   are per-run artifacts of the same batch) — so a batch can be reversed
   later if needed.

### Phase 3: `--process-and-rename`

Runs Phase 1's extraction/captioning and Phase 2's renaming in a single
invocation, skipping the pause for hand-editing `rename_mappings.json`.

**Tradeoff, stated explicitly:** this bypasses the human caption-review
checkpoint that is the stated rationale for the two-phase split. It's
intended for batches where the prompt/model quality has already been
validated (e.g. via `--dry-run` on a sample of the footage set), not as
the default way to run the tool. Not recommended for a camera dump you
haven't captioned with this model/prompt before.

To keep the risk bounded, Phase 3 must **not** skip the safety mechanics
from Phase 2 — only the manual review pause:
1. Run extraction → captioning as in Phase 1, writing `rename_mappings.json` and
   the preview JPEGs to the review folder (unchanged, for audit purposes
   even though nothing pauses on them).
2. Immediately run Phase 2's pre-flight checks (missing files, name
   collisions) and incremental rename logging / audit trail — all
   unchanged from Phase 2. **The confirmation prompt itself is stronger
   than Phase 2's**, to reflect that Phase 3 has no review checkpoint at
   all (see "Stronger confirmation prompt," below).
3. The only thing removed relative to running Phase 1 then Phase 2
   back-to-back is the opportunity to edit `rename_mappings.json` between them.

**Stronger confirmation prompt.** Phase 2's plain "N rename operations —
continue? [y/N]" is enough there because a human has already reviewed
captions against the preview JPEGs from a prior `--dry-run` before Phase 2
ever runs. Phase 3 skips that review entirely, so its prompt echoes a
**sample of the actual captions just generated** — not just a count —
before asking to continue:

```
Phase 3 (--process-and-rename): no review checkpoint — captions below
have not been manually reviewed.

42 rename operations pending (39 newly captioned, 3 carried over from a
previous run).

Sample of newly generated captions:
  A017_C010_0806GC ...  ->  ... waves crashing on rocky shore
  A017_C011_0806GD ...  ->  ... seagulls flying over pier pilings
  A017_C012_0806GE ...  ->  ... sunset light on the water

Continue with 42 renames? [y/N]
```

- **Sample source and size:** up to 3 groups, drawn only from the
  **newly-processed** set (this run-level breakdown is the same one
  computed for Phase 1's run-level summary, above) — never from
  skipped/carried-over groups, since those were already captioned (and
  presumably reviewed) in an earlier run. Selected deterministically (the
  first 3 in sorted order, matching the ordering already used for the
  Disambiguation pass), not randomly, so the same batch always shows the
  same sample.
- **Still skippable the same way as Phase 2** — `--yes`/`-y` skips this
  prompt too. No separate flag: the risk difference is handled by making
  the prompt itself show more before asking, not by making it harder to
  bypass. If the whole batch has 3 or fewer newly-processed groups, the
  sample is just all of them (not padded or omitted).

### Undo Script: on by default, `--skip-generate-undo-script` to disable

Applies to both Phase 2 and Phase 3 — anywhere a rename batch is actually
executed. Complements the `applied_renames_<timestamp>.json` audit trail
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
2. Written as `undo_renames_<timestamp>.sh` at the top level — one
   directory above `review/` (where `rename_mappings.json` and its
   corresponding `applied_renames_<timestamp>.json` live) — kept easy to
   find and run directly (`./undo_renames_<timestamp>.sh`) rather than
   buried with preview JPEGs. The shared timestamp is what correlates the
   two files, not directory placement.
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
     `rename_mappings.json` example above), so naive unquoted `mv` lines would
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

1. ~~**Re-running `--dry-run` on the same folder**~~ — resolved via the
   re-run/skip behavior in Phase 1 step 2, above: groups whose
   `original_files` already appear in an existing `rename_mappings.json` are
   skipped and carried over unchanged (printed as `SKIP`), regardless of
   prior `status`; only new, previously-unseen groups get (re-)processed.
   Net effect is neither overwrite-from-scratch nor a hard error, but an
   incremental append.
2. ~~**Caption collisions**~~ — resolved via the Disambiguation pass in
   Phase 1 step 4, above: a batch-end pass over every `"ok"` group's
   complete final file name, appending `_2`/`_3`/... in deterministic
   (sorted) order to any group after the first that collides. Only
   triggers on a genuine output collision, since this camera's source
   filenames are already unique on import.
3. ~~**Partial pair on rename**~~ — resolved as warning + skip in Phase 2
   step 2, above: a specific warning naming the group and the missing
   file is printed, the surviving file is left un-renamed, and the rest
   of the batch continues. Distinct from a whole group being missing
   (skipped with a simpler batch-level report, not an individual warning).
4. ~~**Filename length limits**~~ — resolved via `max_file_name_length` in
   Filename Assembly, above (default 255, truncates to one character under
   the limit). Narrower open question remains: that config value counts
   characters, not the UTF-8 bytes APFS actually limits to — see the
   "Known caveat" note in that section.
5. ~~**Dry-run diffing**~~ — resolved via the run-level summary printed at
   the end of Phase 1, step 9, above (counts of newly-processed / skipped
   / disambiguated / errors-new-vs-carried-over). Deliberately scoped to
   counts only — a finer-grained per-group before/after diff (e.g. old
   caption text vs. new, for a manually force-reprocessed group) was
   considered and left out as unneeded beyond what the summary and the
   existing per-group `SKIP` lines already cover.
6. ~~**`--process-and-rename` confirmation**~~ — resolved via the
   "Stronger confirmation prompt" in Phase 3, above: the prompt echoes a
   deterministic sample of up to 3 newly-generated captions (never from
   skipped/carried-over groups) alongside the operation count, before
   asking to continue. Still skippable with `--yes`/`-y`, same flag and
   semantics as Phase 2 — the extra safety comes from showing more, not
   from making it harder to bypass.

## Future Improvements

Not yet built; identified after the phases above were implemented and
shipping.

1. **Content-hash-based idempotency across separate batches.** Today,
   idempotent re-running of `--dry-run` (design gap 1, above) only holds
   *within* a single not-yet-applied `rename_mappings.json`: Phase 2
   archives that file to `review/applied_renames_<timestamp>.json` on
   success (`rename.py`'s `write_audit_trail`), and nothing ever reads
   those archives back. `discover_input_dir` also has no filter for
   "generic camera name" -- it lists every `.mov`/`.mp4` in the directory
   by extension alone. So if `--input-dir` is pointed at the same folder
   across two separate `--dry-run`+`--rename-only` cycles run at different
   times, and files renamed by the first cycle are still sitting in that
   directory, slate has no record they were already captioned and will
   caption and rename them again, stacking a second caption onto the
   already-renamed name.

   The `preview_jpeg_sha256` hashing that exists today (`review_sync.py`)
   doesn't help here: it hashes the *extracted preview JPEG*, not the
   source video file, and is only ever checked against entries from the
   *current* run's `rename_mappings.json` -- reconciling a human's JPEG
   rename in `review/` before Phase 2 applies. It's archived away (and
   never read again) along with the rest of that file's contents once
   Phase 2 succeeds.

   A real fix would hash the source video files themselves (or a fast
   proxy -- size + mtime, or a partial hash of the first N bytes, to avoid
   reading full ProRes RAW files start-to-finish) and check that against a
   record persisted across runs -- e.g. scanning `review/
   applied_renames_*.json` archives, or maintaining a running index --
   before deciding whether to (re)caption a file. Not built; today's
   workaround is scoping `--input-dir`/`--input-files` to only the new
   files per run, or moving already-processed footage out of the watched
   directory between runs.

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
`model` follows this same precedence via `--model REPO_ID`: CLI flag wins
if passed, otherwise the config file's `model` key, otherwise the built-in
default (`mlx-community/Qwen2-VL-2B-Instruct-4bit`).

**Known asymmetry:** `max_file_name_length` and `prompt` are the two config
values with no corresponding CLI flag (see Filename Assembly and Model /
Inference, above, respectively) — everything else in `config.toml`
(`prepend_generated_name`/`prefix`/`suffix`, `model`,
`generate_undo_script`) has a matching flag it can be overridden with.

**Format:** TOML — matches `pyproject.toml`, human-editable with comments,
unlike `rename_mappings.json`/`applied_renames_*.json` which are machine-written
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
│       ├── cli.py         # argument parsing + phase orchestration
│       ├── config.py      # config file resolution/parsing (Configuration, above)
│       ├── extraction.py  # frame extraction fallback ladder (Frame Extraction Strategy, above)
│       ├── filenames.py   # filename assembly + truncation (Filename Assembly, above)
│       ├── inference.py   # model resolution/caching + captioning (Model Caching, above)
│       ├── mappings.py    # rename_mappings.json read/write + disambiguation
│       ├── output.py      # centralized colorized/emoji console output
│       ├── pairing.py     # MOV/MP4 pairing logic (File Pairing & Source Selection, above)
│       ├── preflight.py   # startup platform/binary checks (Preflight Checks, above)
│       └── rename.py      # rename plan/execution, audit trail, undo script
```

```toml
[project]
name = "slate"
version = "0.1.0"
dependencies = ["mlx-vlm", "rich", "jinja2"]

[project.scripts]
slate = "slate.cli:main"
```

```bash
uv tool install .
# then callable anywhere as:
slate --input-dir ~/Footage --dry-run
slate --input-dir ~/Footage --rename-only --rename-mappings=review/rename_mappings.json
slate --input-dir ~/Footage --process-and-rename
# undo script is written by default; opt out with:
slate --input-dir ~/Footage --process-and-rename --skip-generate-undo-script
# caption prepended instead of appended, with a known location as prefix:
slate --input-dir ~/Footage --dry-run --prepend-generated-name --prefix "Boston, MA"
# operate on exactly these files, ignoring anything else in the directory
# (see "Input Selection" above — --input-dir and --input-files are mutually exclusive):
slate --input-files clip1.MOV clip1.MP4 clip2.MP4 --dry-run
# override the configured/default model for one run:
slate --input-dir ~/Footage --dry-run --model mlx-community/Qwen2.5-VL-7B-Instruct-4bit
```

Suggested CLI libraries: `typer` (CLI framework, `--help`, arg parsing),
`rich` (progress bars/output formatting for long batch jobs over many clips).

## Naming

**Decided: Slate** (`slate`) — short, clean CLI command name, nods to the
clapperboard/slate used to mark takes on set.

Other candidates considered and set aside: Shot List, Clip Notes, Dailies,
Roll Call, Rewrap, Recap, Reel Mark.

## Next Steps

1. ~~Resolve the six open design gaps above~~ — all six resolved. One
   remaining gap not in this numbered list: the VLM prompt/expected
   caption format is still unspecified (see the note where this list was
   first raised).
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
