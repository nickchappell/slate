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
- Add to `preflight.py`'s binary checks, alongside `ffmpeg`/`ffprobe`/
  `qlmanage`/`sips` (Homebrew: `brew install exiftool`).
- Add to README's required-tools list, matching how `make` was added
  there (see recent commit `1cac43b`) — note whether it's required for all
  users or only those opting into metadata embedding.
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

## Where this runs in the pipeline

- **Phase 2** (`--rename-only`) is where this all belongs: `review_sync.py`
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

## Open questions / not yet decided

- Module structure: new `metadata.py` (parallel to `rename.py`,
  `review_sync.py`) vs. folding exiftool calls directly into `rename.py`.
- Opt-in flag vs. on-by-default once `exiftool` is confirmed present —
  needs a decision consistent with how other optional behaviors are gated
  elsewhere in `cli.py`.
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
