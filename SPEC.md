# Metadata Embedding — Planning Doc (not yet implemented)

Working spec for a not-yet-built feature: writing slate's generated captions
into real, searchable QuickTime/XMP metadata on the renamed footage files,
not just into the filename. Captured from a design conversation on
2026-09-13 so the work can be picked up from a different machine. Nothing
described here exists in the codebase yet — no schema changes, no new
module, no new dependency. Once built, fold the relevant parts of this doc
into `PROJECT_SPEC.md` proper (and README's "Technical Decisions and
Opinions" section) per the convention in `CLAUDE.md`'s Notes section, and
delete this file.

## Goal

Right now the VLM-generated caption only ever ends up in the renamed
filename. The idea: also embed it as structured metadata, so it's
Spotlight-searchable, shows up in Photos/QuickTime Player/DAM tools, and
survives independently of the filename.

## Background: what metadata containers exist in MOV/MP4

Established during this conversation, not something to re-derive:

- **Classic `udta` atoms** — four-character codes, most prefixed with `©`:
  `©nam` (title), `©des` (description), `©cmt` (comment), `©key`
  (keywords), `©inf` (information), `©aut` (author). Same family iTunes
  uses for music tags.
- **`com.apple.quicktime.*` keys** — a newer, extensible mechanism: a
  `meta` atom holding a `keys` atom (ordered key strings) + an `ilst` atom
  (values by index). This is a real, documented part of Apple's QuickTime
  File Format spec (the same `ilst`-style design MP4/M4A use for iTunes
  tags), not an ffmpeg invention — `ffprobe`/ffmpeg's `mov` demuxer just
  walks it generically. Apple's own vocabulary within it includes
  `com.apple.quicktime.title`, `.description`, `.keywords`,
  `.information`, `.make`, `.model`, `.software`, `.creationdate`,
  `.location.ISO6709`, etc. Because the container is generic/extensible,
  other vendors (GoPro, DJI, etc.) write their own reverse-DNS-namespaced
  keys into the same mechanism alongside or instead of Apple's.
- **XMP** — an embedded XMP packet (`moov/uuid` box), same mechanism Adobe
  uses for photos: `dc:title`, `dc:description`, `dc:subject` (a proper
  multi-value keyword list — the one field of the three families that's a
  true list rather than a joined string). This is the schema most DAM
  tools (Adobe Bridge, Lightroom, Premiere) actually search/filter against.

**Tools known to consume this metadata:** exiftool (best/most complete
parser — closest thing to a reference implementation), mediainfo,
ffprobe/ffmpeg (generic pass-through), macOS Spotlight (the built-in
AVFoundation movie importer populates `kMDItemLatitude`/`kMDItemLongitude`/
`kMDItemContentCreationDate`/etc. from these atoms — this is why Finder
search/Spotlight work on camera clips with no sidecar file), Photos.app
(Moments/Places on import), QuickTime Player's Movie Inspector, and
AVFoundation-based pro apps (Final Cut Pro's Info inspector, Compressor,
DaVinci Resolve's Metadata panel, Premiere).

## Caption generation: SHORT / LONG / KEYWORDS in one VLM call

Extend `inference.py`'s `generate_caption()` to produce three outputs from
**one** `vlm_generate` call, not three separate calls.

**Why one call is sufficient:** vision encoding (the expensive part,
especially since slate already feeds multiple sampled frames per clip via
`num_images=len(image_paths)`) happens once during prefill regardless of
how many labeled sections the decoder is asked to produce afterward. Text
generation after that is just autoregressive decoding from the same
context — more output sections cost more decode tokens, which is cheap
next to vision encoding, not a second image pass. Three separate calls
would pay the vision-encoding cost three times per clip, multiplied across
a whole batch run.

**Prompt sketch:**
```
Analyze this clip and respond in exactly this format:
SHORT: <3-6 words, for a filename>
LONG: <one to two sentences>
KEYWORDS: <6-10 comma-separated single words or short phrases naming
subjects, actions, and setting — no articles, no full sentences>
```

**Token budget:** `MAX_CAPTION_TOKENS = 25` (currently sized for one short
caption only — see the comment above it in `inference.py`, which already
notes this constant exists as a generation-time backstop because
word-count instructions in the prompt alone don't reliably bound length)
needs to grow to roughly 120–150 to cover all three sections, then be
tuned empirically.

**Parsing & fallback:** split `result.text` on the `SHORT:`/`LONG:`/
`KEYWORDS:` markers. `KEYWORDS` is the least-constrained/newest ask of the
three and the most likely to break format on a 4-bit-quantized 2B model —
if that section is missing or doesn't look like a comma list, fall back to
a cheap, **dependency-free** derivation from `LONG`: tokenize, lowercase,
strip punctuation, drop a small hardcoded English stopword list, dedupe.
This is not true noun-phrase extraction (deliberately — see below), just a
safety net for the rare malformed case.

**Explicitly rejected approach:** deriving KEYWORDS via real NLP
(spaCy/nltk POS tagging for proper noun-phrase extraction) instead of
asking the VLM directly. Rejected because (a) it's a heavy, slow-importing
dependency that cuts against this codebase's existing lazy-import
discipline (see `inference.py`'s `_ensure_*_deps()` pattern, motivated by
the ~0.9s `mlx_vlm` import cost — "Startup Time" in `PROJECT_SPEC.md`),
and (b) the model asked directly can name concepts it actually *saw* that
never appear as literal words in its own LONG sentence (e.g. "recreation,"
"watercraft"), which a mechanical word-strip of the caption text can never
recover. Keep the stopword-strip path as a fallback only, not the primary
mechanism.

**Needs empirical validation before committing:** whether the quantized
model reliably holds three format constraints in one generation, or
degrades enough that KEYWORDS needs its own follow-up call despite the
extra vision-encode cost. Test against real clips in
`tests/fixtures/footage/` and look at the `KEYWORDS:` section's failure
rate specifically before deciding.

## Field mapping

Worked example, fake caption for a clip of a kayak at sunset:

| Generation | Classic atom | `com.apple.quicktime.*` key | XMP | Example |
|---|---|---|---|---|
| **SHORT** | *(not embedded — filename only)* | *(not embedded)* | *(not embedded)* | `Red_Kayak_At_Sunset` (feeds into `new_stem` via `assemble_stem()`) |
| **LONG** | `©des` | `com.apple.quicktime.description` | `dc:description` | `A red kayak drifts across a calm lake as the sun sets behind distant hills.` |
| **KEYWORDS** | `©key` | `com.apple.quicktime.keywords` | `dc:subject` | `kayak, lake, sunset, red, calm, recreation, hills, water` |
| **Title** | `©nam` | `com.apple.quicktime.title` | `dc:title` | *(see below — not the raw SHORT text)* |

**Title is a special case, decided in this conversation:** Title is
**not** SHORT written verbatim, and is **not** stored as its own JSON
field. Title is always the file's actual final `new_stem` — the whole
assembled filename stem, camera code included (e.g.
`A017_C015_0806GQ red kayak at sunset by the dock`) — computed fresh at
the moment of embedding, which happens right after the real file has
already been renamed to that name. Rationale: the filename is the
human-edited source of truth (the user routinely hand-adds extra words by
renaming the preview JPEG during review), and deriving Title live from
`new_stem` rather than tracking a separate stored value makes drift
structurally impossible — there's nothing to keep in sync because it isn't
tracked separately. **Decided explicitly: full stem verbatim, not just the
caption portion re-extracted from it** — i.e. the camera code stays in the
Title text, not stripped out.

**Not from caption text — separate provenance namespace:** mint slate's
own reverse-DNS keys in the same extensible `keys`/`ilst` mechanism
(idiomatic — this is exactly what GoPro/DJI do for their own vendor data):
- `com.slate.original-filename` — pre-rename stem/pair
- `com.slate.app-version` — mirrors `app_version` already stamped into
  `rename_mappings.json`
- `com.slate.caption-model` — model id + revision
- `com.slate.generated-at` — ISO 8601 timestamp of the captioning run

**Deliberately do not write:** `com.apple.quicktime.software` (means "the
software that produced this file's *content*" — camera firmware/NLE, not
a metadata post-processor like slate; overwriting it would misrepresent
real provenance), or any of `.make`/`.model`/`.creationdate`/`.location.*`
(genuine camera-sourced values — read-only from slate's perspective).

## Writing mechanism: shell out to `exiftool`, not a Python library

Decided: `subprocess` calls to the `exiftool` CLI, not a pure-Python
metadata library, and not repurposing ffmpeg.

- **`mutagen`** (pure-Python) covers the classic `©`-atom family well but
  has no support for `com.apple.quicktime.*` keys or XMP at all.
- **`ffmpeg -c copy -metadata key=value`** avoids a new dependency (ffmpeg
  is already required) but needs a full container remux, has
  limited/version-dependent support for the `com.apple.quicktime.*`
  namespace specifically, and doesn't write XMP.
- **`exiftool`** is purpose-built for exactly this: writes all three
  families in one invocation, is the de facto reference implementation
  other tools are validated against, and is well-documented. New binary
  dependency, but the correct tool for the job.

**Non-destructive, but not a zero-copy in-place patch — matters for large
ProRes RAW files:** exiftool never touches `mdat` (the actual audio/video
sample data) — no decode, no re-encode, no quality loss, no altered
timestamps/packetization. But MOV/MP4 doesn't offer JPEG-style slack space
for patching metadata in place; adding these fields grows the `moov` atom,
so exiftool typically writes a whole new output file (copying `mdat`'s
bytes across unchanged, updating chunk-offset tables if they shifted) and
swaps it in over the original, keeping a `<file>_original` backup by
default. Practical implications:
- Use `-overwrite_original` — otherwise every clip gets a full-size backup
  twin, roughly doubling the footage directory's disk usage across a batch
  run. That tradeoff should be surfaced explicitly in whatever
  confirmation prompt gates this step (mirroring how Phase 2's rename
  already surfaces its own risk before acting).
- I/O time is proportional to whole-file size (sequential copy, not
  CPU-bound re-encoding) — cheap relative to a re-encode, but not
  instantaneous the way a JPEG EXIF patch is. **Benchmark against a real
  large file in `tests/fixtures/footage/` before committing to this as a
  batch feature** — this is an empirical question specific to whatever
  ProRes RAW file sizes are actually in play, not something to assume from
  general knowledge.

**New dependency housekeeping, if this proceeds:**
- **Decided:** add to `preflight.py`'s binary checks unconditionally,
  alongside `ffmpeg`/`ffprobe`/`qlmanage`/`sips` (Homebrew: `brew install
  exiftool`) — required for every invocation regardless of whether
  `--add-metadata`/`--metadata-backfill` is used, matching the existing
  flat/unconditional shape of `run_preflight_checks()` (no mode-awareness
  needed there). Trade-off accepted deliberately: it's a hard requirement
  for installation even for users who never touch metadata ops, but it's
  a one-line `brew install` and keeps preflight simple/uniform rather than
  needing to thread flag/mode info into it.
- Add to README's required-tools list, matching how `make` was added
  there (see recent commit `1cac43b`).
- Plain `subprocess.run([...])` per file is fine at slate's batch scale
  (dozens of clips, not thousands) — same pattern as `extraction.py`'s
  ffmpeg calls. `PyExifTool`'s "stay open" mode (one persistent process,
  commands over stdin) is an optimization to reach for only if per-call
  process-startup overhead actually shows up as real cost, not a starting
  point.

## `rename_mappings.json` schema changes

New fields on `MappingEntry` (`mappings.py`), `status == "ok"` only:

- `short_caption: str | None` — the model-generated SHORT text. Currently
  implicit (just fed into `assemble_stem()` and discarded); making it an
  explicit field exposes it for direct JSON editing as an alternative to
  renaming the preview JPEG.
- `long_caption: str | None` — model-generated LONG text → embedded as
  Description at rename time. JSON-only editing (no filename equivalent).
- `keywords: list[str] | None` — model-generated KEYWORDS → embedded as
  Keywords/`dc:subject`. JSON-only editing.

**No new field for Title** — see above, it's always derived live from
`new_stem`, never stored.

## Reconciliation & precedence: JPEG rename vs. JSON edit

Two ways to edit the short caption before Phase 2 applies renames, and
they can disagree:

- **Renaming the preview JPEG** — freeform edit. `review_sync.py` already
  reconciles this via the JPEG's SHA-256 (a plain rename doesn't touch
  file bytes, so the hash survives it) and takes the whole new stem
  verbatim — it has no way to know which part of the new name is "caption"
  vs. "original stem."
- **Editing `short_caption` directly in the JSON** — structured edit.
  Phase 2 should re-run `assemble_stem()` with the edited text, so
  `original_stem`/prefix/suffix stay correctly separated and `new_stem` is
  rebuilt properly rather than freeform.

**Precedence when both diverge from Phase 1's original output for the
same group: the JPEG rename wins.** It's the more direct, unambiguous "this
is exactly what I want it called" signal, and it's already the existing
mechanism's contract. A `short_caption` JSON edit only drives `new_stem`
when the JPEG wasn't separately touched.

## CLI flags

Decided in this conversation:

- **`--add-metadata`** — opt-in modifier for the main rename flow.
  Combinable with `--dry-run`, `--rename-only`, and `--process-and-rename`.
  **Off by default**, specifically so existing scripts that already call
  `slate` keep today's rename-only behavior unchanged — metadata never
  gets written just because this feature exists in the codebase.
  - `--dry-run --add-metadata` — captioning uses the three-section
    SHORT/LONG/KEYWORDS prompt instead of SHORT-only, and
    `short_caption`/`long_caption`/`keywords` get populated in
    `rename_mappings.json` for review. Still fully non-destructive, same
    contract as plain `--dry-run`.
  - `--rename-only --rename-mappings=... --add-metadata` — applies the
    rename *and* writes the reviewed metadata via exiftool.
    `--add-metadata` must be passed again here, explicitly — it is
    **not** inferred from the mapping file already containing
    `long_caption`/`keywords`, precisely so an existing automated
    `--rename-only` invocation never starts writing metadata just because
    a human (or later tooling) added those fields to the JSON.
  - `--process-and-rename --add-metadata` — same three-section prompt and
    embed, in the single combined invocation.
  - **Constraint carried over from the existing design:** `--rename-only`
    (Phase 2) deliberately never touches `mlx_vlm` (see CLAUDE.md's module
    layout note: "`--rename-only` (Phase 2) never captions, so it never
    touches `mlx_vlm`"). So `--rename-only --add-metadata` must **not**
    fall back to invoking the VLM on the spot if `long_caption`/`keywords`
    are missing from an "ok" group in the loaded mapping file (e.g.
    because it was produced by a plain `--dry-run` without
    `--add-metadata`). That has to be a hard error telling the user to
    re-run `--dry-run --add-metadata` first — never a silent,
    on-the-fly generation that would break the existing invariant.

- **`--metadata-backfill`** — standalone mode for already-renamed files
  (see "Backfill mode" below), entirely separate from the rename flow.
  Reuses `--dry-run` rather than defining a metadata-specific dry-run flag:
  - `--metadata-backfill --dry-run [--input-dir=... | --input-files=...]`
    — generate step: writes `review/metadata_changes.json` + preview
    JPEGs, no writes to the real files.
  - `--metadata-backfill --metadata-mappings=review/metadata_changes.json`
    (no `--dry-run`) — apply step: writes metadata via exiftool.

**Mutual exclusivity:**
- `--metadata-backfill` × (`--rename-only`, `--process-and-rename`,
  `--add-metadata`) — hard error. Backfill mode never renames anything,
  and its metadata writing isn't optional/toggleable the way
  `--add-metadata` is, so combining the two is meaningless, not just
  redundant. Only rename files, or only backfill metadata — never both in
  one invocation.
- `--dry-run` is shared/reused by both axes — it means "generate step, no
  writes" either way, disambiguated entirely by whether
  `--metadata-backfill` is also present.

**Implementation note:** this isn't a simple pairwise conflict
`argparse`'s `add_mutually_exclusive_group()` handles cleanly on its own
(`--dry-run` needs to combine freely with either axis, while
`--metadata-backfill` conflicts with three specific other flags) — likely
needs manual post-parse validation in `cli.py` (`parser.error(...)`)
alongside whatever argparse grouping covers the simpler pairs.

### Flag-safety review

Raised after the flags above were first drafted — the opt-in/off-by-default
core is right (existing scripts calling `slate` shouldn't get new behavior
or new requirements just because this feature exists), but four follow-on
questions came out of reviewing it critically:

1. **Silent non-write footgun — decided.** Because `--add-metadata` must
   be re-passed at apply time, it's easy to generate `long_caption`/
   `keywords` via `--dry-run --add-metadata`, then later run `--rename-only
   --rename-mappings=...` *without* the flag by mistake. Resolved: if a
   loaded mapping file has `long_caption`/`keywords` present on one or more
   "ok" groups but `--add-metadata` wasn't passed at apply time, warn
   loudly about it (via `output.py`, not a silent no-op) — the rename
   still proceeds, but the warning makes clear metadata was generated for
   this batch and is *not* being written this run.
2. **Hard error vs. warn-and-skip when required fields are missing —
   decided.** The earlier call (`--rename-only --add-metadata` hard-aborts
   the whole batch if `long_caption`/`keywords` are missing from an "ok"
   group) is replaced: follow the project's existing precedent for a
   comparable partial-failure case (the MOV/MP4 pair-deletion edge case is
   "warning + skip," not abort). So: warn, skip metadata-writing for the
   affected groups specifically, and let the rename proceed for all groups
   (including the affected ones — only the metadata step is skipped, not
   the rename). This still preserves the "`--rename-only` never touches
   `mlx_vlm`" invariant (skipping isn't touching it), without making an
   otherwise-legitimate rename batch newly blockable by a metadata
   misconfiguration.
3. **`exiftool` preflight requirement — decided.** Add it to
   `preflight.py`'s binary checks unconditionally, same flat/unconditional
   shape as the existing checks, required for every invocation regardless
   of whether metadata flags are used. Traded off deliberately: it's a new
   hard requirement even for users who never touch `--add-metadata`/
   `--metadata-backfill`, accepted because it's a one-line `brew install`
   and keeps `preflight.py` simple rather than needing mode-awareness
   threaded into it. (Full detail under "Writing mechanism" above.)
4. **Config-file default for `--add-metadata` — not yet decided, not
   urgent.** Config precedence is CLI flags > config file > defaults
   (`config.py`). A persistent `add_metadata = true` in `config.toml`
   would remove the per-invocation friction for a user who always wants
   metadata, but reintroduces the "behavior changes without an explicit
   flag on this invocation" risk that made the flag opt-in in the first
   place, for anything that relies on config defaults rather than passing
   flags explicitly. Flagged as a real question, not resolved.

## Where this runs in the pipeline

- Everything below is what `--add-metadata` turns on — none of it runs
  without that flag. **Phase 2** (`--rename-only`) is where this all
  belongs: `review_sync.py`
  gets extended to reconcile `short_caption` under the precedence rule
  above (it already runs at the start of Phase 2 today), and the actual
  exiftool embed call slots into `rename.py`'s existing per-file
  rename-execution loop, run right after each file is physically renamed
  to its final name (Title needs the final `new_stem` to exist as the
  actual filename at write time).
- **Phase 3** (`--process-and-rename`) inherits this for free — it already
  reuses Phase 2's rename-execution mechanics wholesale. The reconciliation
  step is simply a no-op there, since Phase 3 has no review checkpoint and
  therefore nothing could have diverged between `short_caption` and a
  hand-renamed JPEG.
- (Earlier in this design conversation "Phase 3" was briefly misused to
  mean this feature's placement — corrected: the user meant Phase 2
  throughout. Noting this only so a future reader doesn't get confused by
  scrollback if this doc is ever read alongside the original conversation.)

## Backfill mode: embedding metadata into already-renamed files

**Decided: option 2 from the design discussion** — a dedicated review
file, `review/metadata_changes.json`, gates this the same way
`rename_mappings.json` gates a rename, even though this mode never
touches filenames.

**Use case:** files that were already captioned and renamed by slate in a
past run (before this feature existed), sitting in known folders. Goal:
regenerate LONG + KEYWORDS via a fresh VLM pass, take SHORT/Title straight
from the file's current name (no parsing needed — see the Title design
above, it was already meant to be read live from whatever the file is
currently named), and write it all in as metadata. No renaming involved.

**Why this is a distinct mode, not a variant of Phase 1/2:** almost none
of the rename-specific machinery applies — no `filenames.py`/
`assemble_stem()`, no `rename.py` execution, no disambiguation, no
`review_sync.py` JPEG-hash reconciliation (there's no filename being
edited via JPEG rename in this flow; SHORT/Title isn't editable at all
here, it's just whatever the file is already named). What *does* carry
over unmodified: `pairing.py` (still groups by shared stem — both files in
an original pair still share the renamed stem) and `extraction.py` (frame
sampling doesn't care about filenames).

**Prompt variant:** drop the `SHORT:` section entirely for this mode —
no reason to spend decode tokens on a caption that gets discarded:
```
Analyze this clip and respond in exactly this format:
LONG: <one to two sentences>
KEYWORDS: <6-10 comma-separated single words or short phrases...>
```

**`review/metadata_changes.json` schema** — same `{"app_version": ...,
"groups": [...]}` top-level shape as `rename_mappings.json` (reuse
`major_version_mismatch`/versioning machinery), but each group entry
(`status == "ok"`) is:

- `current_files: list[str]` — the already-renamed file(s) in this group.
  Named `current_files`, deliberately **not** `original_files` like
  `rename_mappings.json` uses — these are the files as they exist *now*,
  post-rename; there's no earlier "original" name recorded here (unless a
  prior run already embedded `com.slate.original-filename`, which is a
  separate concern from this JSON's own field naming).
- `title: str` — informational only, populated at generation time from
  the current filename stem so the review JSON is self-documenting.
  **Not authoritative** — consistent with the Title design decided above
  (always derived live, never trusted from storage), the apply step
  re-reads the real file's current name fresh rather than trusting this
  field, in case the file got renamed again between generate and apply.
  Not meant to be hand-edited here; if a different title is wanted, rename
  the file directly and re-run generation.
- `long_caption: str` — proposed, human-editable.
- `keywords: list[str]` — proposed, human-editable.
- `preview_jpeg: str` — composited preview JPEG via the existing
  `build_montage()`, same rationale as Phase 1's preview: let a human
  sanity-check LONG/KEYWORDS against the actual footage without opening
  the video.
- `source_used_for_caption: str` — mirrors `rename_mappings.json`'s field
  (which paired file was used as the captioning source).
- `status`/`error` — same "ok"/"error" pattern as `MappingEntry`.

**Two-step flow, mirroring Phase 1 → Phase 2's shape, using the flags
decided above:**
1. **Generate** (`--metadata-backfill --dry-run --input-dir=...`, reusing
   the existing input-selection flags) — scans the given files/dir,
   re-runs pairing + extraction + the 2-section VLM prompt, writes
   `review/metadata_changes.json` + preview JPEGs. No writes to the real
   files. Incremental/re-runnable the same way Phase 1 is: groups already
   present (matched by `current_files`) get skipped and carried over,
   regardless of prior `status`.
2. **Apply** (`--metadata-backfill
   --metadata-mappings=review/metadata_changes.json`, no `--dry-run`) —
   re-checks every file still exists, re-derives `title` live from each
   file's actual current name (not from the JSON), confirms, then writes
   Title/Description/Keywords + the `com.slate.*` provenance fields via
   exiftool. On success, archives the mapping file in place to
   `applied_metadata_changes_<timestamp>.json`, matching the
   `applied_renames_<timestamp>.json` audit-trail convention.

**Idempotency:** worth adding a check (via an exiftool read) to skip files
that already carry `com.slate.*` provenance tags from a prior apply, as a
safety net beyond the JSON-based carried-over-group skip — covers the case
where `metadata_changes.json` itself was deleted/archived between runs but
the target files were already processed.

**Same disk/I/O caveat as the main design applies here too** — nothing
different about this mode changes the earlier `-overwrite_original`
disk-doubling tradeoff or the whole-file-rewrite cost; it's the same
exiftool mechanics, just reached via a different entry point.

## Open questions / not yet decided

- Module structure: new `metadata.py` (parallel to `rename.py`,
  `review_sync.py`) vs. folding exiftool calls directly into `rename.py`.
- Error-handling policy for exiftool failures mid-batch: skip-and-warn
  (matches the MOV/MP4 pair-deletion "warning + skip" precedent at Phase
  2's pre-flight) vs. hard-abort.
- Interaction with the undo script: does undoing a rename also need to
  care about embedded metadata (it doesn't rename anything back that
  wasn't itself renamed, so probably no — metadata isn't touched by the
  undo path — but worth confirming explicitly rather than assuming).
- Testing strategy: unit tests will need exiftool calls mocked/stubbed
  (matching the hermetic-unit-suite convention — see CLAUDE.md's "No CI"
  section, `make check`'s unit suite has no real `ffmpeg`/`mlx-vlm`/
  network); real exiftool behavior only gets exercised in
  `tests/integration` against `tests/fixtures/footage/`.
- Once built: update `PROJECT_SPEC.md` (new "Metadata Embedding" section,
  probably after "Filename Assembly" and before "Workflow Modes," since
  Phase 2's description will need to reference it) and README's "Technical
  Decisions and Opinions," then delete this file.
- Backfill mode's `-overwrite_original` vs. keeping `_original` backups:
  same open tradeoff as the main flow, not yet decided either place.
- Whether the `com.slate.*`-tag idempotency check (backfill mode) needs an
  exiftool *read* call per candidate file before the VLM pass, or can be
  folded into the same invocation as the eventual write — affects whether
  it costs an extra subprocess call per file.

## Next steps to pick this back up

1. Decide the open questions above (or bring them back to a design
   conversation).
2. Prototype the three-section prompt against `tests/fixtures/footage/`
   and check the `KEYWORDS:` section's failure rate empirically.
3. Benchmark exiftool's write time against the largest real file in
   `tests/fixtures/footage/` to confirm the full-file-rewrite cost is
   acceptable.
4. Implement schema changes in `mappings.py`, prompt/parsing changes in
   `inference.py`, the exiftool shell-out (wherever module structure ends
   up), `review_sync.py`'s extended reconciliation, `preflight.py`'s new
   binary check, and README/`PROJECT_SPEC.md` updates.
