# Metadata Write Failures on Malformed `meta` Atoms

**Status:** implemented (2026-09-16), in `src/slate/metadata.py`
(`_ffmpeg_write_keys_family()` + `embed_metadata()` + a new Bento4-based
`mebx` repair step). The first validated design -- ffmpeg does a
structural-only repair, then the *same* combined exiftool write is
retried -- turned out to be broken: it silently concatenates slate's new
values into the vendor's `com.apple.quicktime.*` tags, invisible to
`exiftool`'s own reads but real and visible via `ffprobe`. See "Follow-up:
two-pass ffmpeg + exiftool" for that finding and the corrected design that
replaced it (an atom-family ownership split, not a retry), shipped and
covered by unit tests. The `mebx` mislabeling from Option B is now also
fixed, as a third pass using Bento4's `mp4extract`/`mp4edit` -- see
"Follow-up: fixing the `mebx` mislabeling with Bento4". The audio-timing
shift from Option B remains open and orthogonal (see that section). A
second fixture from the same phone but a different camera app (Moment Pro)
was tested and hits *neither* bug, pointing at each app's own metadata
implementation rather than Apple's shared ProRes encoding pipeline -- see
"Follow-up: is this bug specific to Kino, or does any iPhone ProRes
recording trigger it?".

## The bug report

Running `--rename-only --add-metadata` against real footage
(`2026-09-12_09-35-48 train in urban setting.mov`, a Lux Optics "Kino" app
recording) produced:

```
WARNING: metadata embedding failed for "2026-09-12_09-35-48 train in urban
setting.mov" (Error:  Terminator found in Meta with 478 bytes remaining -
...) -- filename was still renamed; re-run --metadata-backfill on this file
to retry.
```

The rename succeeded (as designed); only the `exiftool` metadata write
failed. This is `metadata.embed_metadata()`'s existing skip-and-warn path
(`src/slate/metadata.py`) working as intended -- the open question is
whether/how to do better than "give up on this file's metadata."

## Root cause: a real exiftool bug, not a corrupt file

Traced to `Image::ExifTool::WriteQuickTime.pl:1063`:

```perl
$et->Error("Terminator found in $str remaining", 1);
```

The trailing `1` marks this a **minor** error in exiftool's own
classification (`ExifTool.pm:5626`, `sub Error`): with `-m`
(`IgnoreMinorErrors`) it downgrades to a warning and the write proceeds;
otherwise it's fatal and the write aborts (file untouched -- confirmed via
`md5` before/after a failed write).

**The file is not corrupt.** Walking the raw bytes directly (bypassing
exiftool entirely -- script preserved in this investigation, not checked
in) shows a textbook-valid ISO-BMFF `meta` FullBox at file offset 144:

```
00 00 00 00  00 00 00 21 68 64 6c 72  00 00 00 00 ...
└version/flags┘└─size 33──┘└─"hdlr"──┘
```

4-byte version/flags, then a valid 33-byte `hdlr` atom, then a valid `keys`
atom declaring 5 `com.apple.quicktime.*` keys. `exiftool`'s own
`QuickTime.pm:553` even declares `Start => 4  # skip 4-byte version number
header` for exactly this atom -- it knows it needs to skip those 4 bytes.
Tracing `ProcessMOV`'s read logic (`QuickTime.pm` ~line 9958:
`$raf->Seek($$dirInfo{DirStart}, 1)`), that skip should land exactly on the
valid `hdlr` header. Empirically it doesn't -- both the read path (silently,
via a `VPrint`, not an `Error` -- so reads don't abort, they just quietly
give up on that subtree) and the write path (fatally, via the `Error(...,
1)` above) misfire identically, treating byte 4 as a bogus zero-size atom.

Exact reason for the misfire not fully root-caused (would require
instrumenting/debugging exiftool's Perl internals further); confirmed only
that it's consistent and reproducible for this atom shape (movie-level
`meta`, as opposed to `moov/udta/meta`), and specific to whatever encoder
Kino/Lux Optics uses to write it. Not found in exiftool's local `Changes`
log; 13.55 (tested) is Homebrew's current stable, so this isn't something a
routine exiftool upgrade already fixes. Worth reporting upstream
(exiftool.org forum / GitHub) with the byte-offset walk above as repro.

## Option A: `exiftool -m` (IgnoreMinorErrors)

Downgrades the write-abort to a warning and lets the write proceed.
**Empirically verified to destroy real data, not just padding:**

Independently confirmed with `ffprobe` (a completely different MOV
demuxer) that the block exiftool can't parse contains real, meaningful
tags:

```
com.apple.quicktime.model             = iPhone
com.apple.quicktime.make              = Lux Optics
com.apple.quicktime.software          = Kino
com.apple.quicktime.location.ISO6709  = +45.5123-122.6639+017.635/...
com.apple.quicktime.creationdate      = 2026-09-12T16:35:48Z
```

After an `exiftool -m` write, `ffprobe` on the rewritten file shows only
the newly-written `title` tag -- all five are gone, permanently. Comparing
`exiftool -v3` traces before/after confirms this isn't a trim: the entire
original top-level `udta`/`meta` atom is discarded and a fresh one is built
elsewhere in the file to hold the new Title/XMP.

Mitigating factor: none of this was ever *readable* via any exiftool-based
workflow to begin with (confirmed: plain reads, `-ee`, `-api
RequestAll=3 -u -U` all return nothing for these fields on the original
file -- the read path hits the identical bug, silently). So `--add-metadata
--verbose`'s "pre-existing field preserved into `com.slate.original-*`"
safety net (`metadata.py`'s read-before-write collision check) can't see
this data either, and never could. But the raw bytes are still physically
present and recoverable today by any *other* tool (ffprobe demonstrated
this); `-m` makes that recovery permanently impossible.

**Verdict: do not enable by default.** Directly contradicts the design
principle in `PROJECT_SPEC.md`'s "Metadata Embedding" section that vendor
tracks "stay untouched no matter what gets embedded regardless of
manufacturer." An opt-in flag (e.g. `--ignore-minor-exif-errors`) remains
on the table if a future user explicitly wants this tradeoff on a specific
file, but should ship with the ffprobe-verified loss described above in its
`--help` text, not just a generic "may lose data" hedge.

## Option B: ffmpeg remux instead of exiftool for this class of file

Idea: dump existing tags via `ffprobe` (which reads this file fine),
merge in the new caption-derived Title/Description/Keywords in Python,
and write everything back via an `ffmpeg` stream-copy remux instead of
`exiftool`:

```
ffmpeg -i in.mov -map 0 -c copy -map_metadata 0 -movflags use_metadata_tags \
  -metadata title="..." -metadata description="..." -metadata keywords="..." \
  out.mov
```

**What worked:** `-movflags use_metadata_tags` writes to the same
`mdta`/`Keys` atom family the file already uses. Tested against a real copy
of the affected file: all 5 original `com.apple.quicktime.*` tags survived
the round-trip *and* the new Title/Description/Keywords were added, *and*
-- notably -- `exiftool` could read all of it back afterward (ffmpeg's atom
layout doesn't trigger the bug). Video stream verified bit-for-bit
identical by decoding both files to raw pixels and hashing
(`ffmpeg -f md5`). Fast: stream copy, <1s even on a 997MB file.

**What didn't, found by testing further:**

1. **Audio timing shifts slightly.** Compressed AAC bytes are identical
   (`md5` match on the extracted elementary stream), but *decoded* PCM
   output differs by roughly one frame (~15-20ms) -- almost certainly a
   different edit-list/start-offset written by ffmpeg's muxer vs. the
   original encoder. Small, probably inaudible, but a real change to
   archived footage that the current exiftool-only path never risks
   (exiftool, when it works, never rewrites `mdat` or edit lists, only
   declared metadata atoms).

2. **Only covers `Keys`/`mdta`, not `ItemList` or XMP.** No combination of
   `ffmpeg` muxer flags covers all three families the way one `exiftool`
   call does; matching current coverage would need a second `exiftool` pass
   after the `ffmpeg` one -- a two-tool pipeline, not a drop-in fix.

3. **Corrupts the embedded `mebx` timed-metadata track (Track3).** This
   file carries a GPS sample as a *timed* metadata track, separate from the
   static `com.apple.quicktime.*` tags. `ffmpeg` warns `Unknown hdlr_type
   for mebx, writing dummy values` and it's not cosmetic -- verified via
   `ffprobe -show_streams`:

   | | Original | After ffmpeg remux |
   |---|---|---|
   | `codec_tag_string` | `mebx` | `stts` (!) |
   | `HandlerType` | NRT Metadata | URL |
   | `MetaFormat` | mebx | *(gone)* |
   | `language` tag | und | *(gone)* |
   | generic media header fields | present | *(gone)* |

   `stts` is the fourcc of an unrelated atom (sample time-to-sample table)
   -- not a placeholder, a nonsensical format declaration. Any
   standards-compliant reader (QuickTime Player, Photos, AVFoundation,
   NLEs) determines how to interpret a track from exactly this declaration;
   none would recognize Track3 as metadata after this remux. The one
   mitigating detail: the raw 34-byte sample payload
   (`+45.5123-122.6639+017.635/`, the same GPS string) is byte-identical
   before/after (verified via `md5` on the extracted raw track), so the
   data physically survives -- but only recoverable by someone who already
   knows to manually reinterpret a mislabeled track as `mebx`, not through
   any normal read path.

**Verdict: not viable as a routine or default path.** Between the audio
timing shift and the `mebx` corruption, this trades "give up on this
file's metadata" for two new, independent forms of damage. Might be
narrower/safer for files that don't carry an embedded timed-metadata track,
but reliably detecting "this file has no `mebx` track" ahead of time, and
whether the audio-timing shift is acceptable at all for an archival tool,
are both open questions -- not concluded here.

## Follow-up: a real repro fixture

`tests/fixtures/footage/iphone_17_pro_kino_prores_422.mov` (198MB, checked
in 2026-09-15) is a second, independent Kino/Lux Optics recording that
reproduces the exact same bug as the original (uncommitted) bug-report
file. Confirmed via the identical repro steps as above:

```
$ exiftool -Title="test title" -overwrite_original iphone_17_pro_kino_prores_422.mov
Error: [minor] Terminator found in Meta with 478 bytes remaining - ...
    0 image files updated
```

(file MD5 unchanged -- write aborted cleanly, as before). Its shape
matches the original file closely enough to treat findings below as
general, not fixture-specific:

- 3 streams: video (`apcn`, ProRes 422, 3840x2160), audio (`mp4a`/AAC), and
  a `mebx` data track (Track index 2/ID 3) -- the same timed-GPS-metadata
  track described in Option B above.
- The same 5 `com.apple.quicktime.*` static tags (model/make/software/
  location/creationdate), readable via `ffprobe`'s `format_tags`,
  invisible to any exiftool read (bare, `-ee`, or otherwise) -- same
  silent-read-failure behavior as the original file.

This means the two investigations below no longer rest on a script/file
that "was preserved but not checked in" -- they're reproducible by anyone
with this repo.

## Follow-up: two-pass ffmpeg + exiftool

Section "Option B" above showed ffmpeg's `use_metadata_tags` remux
produces a `meta` atom exiftool can read cleanly afterward, but stopped
short of testing a second exiftool pass on top of it. This section covers
two designs tried in sequence: the first looked "viable" under exiftool's
own reads and shipped briefly, then turned out to be broken once checked
against `ffprobe`; the second, an atom-ownership split rather than a
retry, is what's actually implemented in `src/slate/metadata.py` today.

### First attempt (retry the same combined write) -- looked viable, wasn't

**Pass 1 (ffmpeg, structural repair only -- writes no new tag values):**

```
ffmpeg -i in.mov -map 0 -c copy -map_metadata 0 -movflags use_metadata_tags out.mov
```

**Pass 2:** retry the exact same combined exiftool call `embed_metadata()`
already builds (all three tag families -- `ItemList`, `Keys`, `XMP-dc` --
plus the four `com.slate.*` provenance keys, in one command) against the
repaired file.

This succeeded where the original file always failed with "Terminator
found in Meta," and every check run against it *by reading through
exiftool* came back clean: all three families populated, all four
provenance keys present, all 5 original `com.apple.quicktime.*` tags
(model/make/software/location/creationdate) apparently untouched, video
bit-for-bit identical, `preserved_fields=[]` correctly reflecting no
genuine pre-existing values. One real sequencing bug was caught and fixed
along the way: if pass 1 also wrote the *new* title/description/keywords
(not just repairing the atom), pass 2's pre-write collision check read
those back and wrongly preserved them into `SlateOriginalTitle`/etc. as if
they were camera-original values -- fixed by making pass 1
structural-repair-only.

**Then a `ffprobe` dump (not an exiftool read) during a later trial run
revealed the "untouched" vendor tags were actually corrupted:**

```
"com.apple.quicktime.model":    "iPhone;A commuter train moves through a city corridor."
"com.apple.quicktime.make":     "Lux Optics;train, urban, transit"
"com.apple.quicktime.software": "Kino;A train passing through an urban setting"
```

Each vendor field held its correct original value *concatenated* with one
of slate's new values, joined by `;` -- Model merged with the new
Description, Make merged with the new Keywords, Software merged with the
new Title. Several `ftyp`/brand-level fields (`major_brand`,
`minor_version`, `compatible_brands`, `creation_time`) showed the same
pattern merged with provenance fields (app version, caption model,
generated-at timestamp, original filename). `exiftool -G1` reads of the
identical file showed none of this -- it resolves the collision by tag
name and silently picks a value, hiding the corruption from the one tool
whose reads this doc had been trusting. Root cause: when exiftool appends
new `Keys`-family entries onto a `meta`/`Keys` atom that ffmpeg (not
exiftool) built, it doesn't allocate fresh, non-colliding key-array
indices -- it lands new values on top of existing low-numbered vendor
slots. `ffprobe` (and, presumably, any other reader parsing the
`Keys`/`ilst` atom the way the format actually specifies rather than the
way exiftool's own name-based resolution papers over) would show this
exact garbled data.

**This directly contradicts the design principle this doc opened with** --
vendor tracks must "stay untouched no matter what gets embedded" -- so the
retry design's "Verdict: viable" was wrong, based on incomplete
verification (no `ffprobe` dump was ever taken of the two-pass output
before that verdict was written).

### Corrected design: split ownership by atom family, not a retry

The fix isn't retrying the same command -- it's never letting exiftool
append to the `Keys`/`mdta` atom on a repaired file at all. Two writers,
one atom family each:

**ffmpeg pass (owns the entire `Keys`/`mdta` family: repairs the atom,
writes new values, provenance, and any preserved pre-existing values, all
in one remux):**

```
ffmpeg -i in.mov -map 0 -c copy -map_metadata 0 -movflags use_metadata_tags \
  -metadata com.apple.quicktime.title="..." \
  -metadata com.apple.quicktime.description="..." \
  -metadata com.apple.quicktime.keywords="..." \
  -metadata com.slate.original-filename="..." \
  -metadata com.slate.app-version="..." \
  -metadata com.slate.caption-model="..." \
  -metadata com.slate.generated-at="..." \
  out.mov
```

(plus `-metadata com.slate.original-title=...`/`-description`/`-keywords`
when the pre-write collision check found genuine pre-existing values to
preserve). No exiftool `-config` involved -- ffmpeg accepts arbitrary
metadata key names as-is, unlike exiftool's rejection of bare dotted names
on the command line.

**exiftool pass (owns `ItemList` + `XMP-dc` only -- no `-Keys:` flag
appears anywhere in this command):**

```
exiftool -overwrite_original \
  -ItemList:Title="..." -XMP-dc:Title="..." \
  -ItemList:Description="..." -XMP-dc:Description="..." \
  -ItemList:Keyword="..." -XMP-dc:Subject="..." [repeated per keyword] \
  out.mov
```

Since exiftool never touches `Keys` in this design, the index-collision
corruption above cannot happen -- there's exactly one writer per atom
family, never two.

**A second bug was caught and fixed while validating this:** the first
version of the ffmpeg command used bare `-metadata title=...`/
`description=`/`keywords=` (matching Option B's original example above),
not the fully-qualified `com.apple.quicktime.title` form exiftool's own
`-Keys:Title=` produces. Bare names write a *differently-named* key that
collides with `ItemList`'s own same-named key on read, showing up as the
same value duplicated and joined with a literal `;` to itself (e.g.
`"title": "A train passing...;A train passing..."`) -- benign (same
content twice, not data loss) but sloppy, and inconsistent with how a
healthy single-exiftool-call file looks. Confirmed by writing metadata to
a plain synthetic file via the current unmodified `embed_metadata()`: a
healthy file shows `title` (from `ItemList`) and `com.apple.quicktime.title`
(from `Keys`) as two distinct, separately-valued keys, never joined.
Switching the ffmpeg pass to the fully-qualified key names reproduced that
exact clean pattern.

Also confirmed while validating: the `major_brand`/`minor_version`/
`compatible_brands`/`creation_time` duplication noted above is a
pre-existing ffmpeg-remux artifact, not something introduced by this
fix -- it appears identically on a bare structural-repair-only remux with
*zero* new metadata written. Root-caused via a direct box-level diff
(`mp4dump`/`mp4extract` against both the original and the bare-remuxed
file): the original file has no `udta` box under `moov` at all -- only
the top-level `moov/meta` (the real `Keys`/`mdta` vendor-tag container).
`-map_metadata 0 -movflags use_metadata_tags` makes ffmpeg's muxer create
a **second, brand-new metadata container**, `moov/udta/meta`, that didn't
exist before, and dump its entire internal metadata dictionary into it as
plain string tags -- including `major_brand`/`minor_version`/
`compatible_brands` (normally structural fields living only in `ftyp`)
and `creation_time` (normally only in `mvhd`), alongside a second copy of
all five vendor `com.apple.quicktime.*` tags and an `encoder` tag.
`ffprobe`, reading the file back, merges the real structural value with
this new container's copy under the same display key -- joining rather
than overwriting for these four keys specifically (the five vendor tags
and `encoder` don't show this join; ffmpeg/ffprobe's read-side dictionary
merge apparently treats them differently, though the exact reason wasn't
traced into ffmpeg's own source).

**Correction to an earlier characterization in this doc:** three of the
four (`major_brand`, `compatible_brands`, `creation_time`) do turn out to
hold the *identical* string on both sides, so those are a cosmetic
duplicate with no data loss. But `minor_version` genuinely does **not** --
confirmed via `xxd` on the real `ftyp` box, which holds ffmpeg's own muxer
default (`512`, unrelated to the source file), while the copy sitting in
the new `udta/meta` container holds the *original* file's actual value
(`0`), carried forward unchanged by `-map_metadata 0`. So `"512;0"` is two
genuinely different values from two different sources landing under one
display key, not a repeated value -- this doc previously (incorrectly)
described the whole group as "a duplicate of the same value ... not
merged-with-different-content." Harmless in practice (no player reads
`minor_version` from a generic metadata tag instead of the real `ftyp`
box, and video/audio are unaffected either way), but worth being precise
about since "same value twice" and "two different values, one stray" are
different claims.

One more thing this dig surfaced, unrelated to the remux: the pristine,
*unprocessed* fixture file already shows
`com.apple.quicktime.location.ISO6709` as
`"+45.5137-122.6650+011.374/;+45.5137-122.6650+011.374/"` -- joined,
identically, before slate or ffmpeg ever touch the file. That duplication
is a property of how Kino itself wrote the file (or how ffprobe surfaces
whatever internal structure Kino used for that field), not an artifact of
this pipeline at all -- it would be a mistake to attribute it to the
remux just because it looks like the same pattern.

**Final verification** (`ffprobe` dump, via the real unmodified
`metadata.embed_metadata()` call, not a hand-typed reimplementation):

```json
{
  "com.apple.quicktime.model": "iPhone",
  "com.apple.quicktime.make": "Lux Optics",
  "com.apple.quicktime.software": "Kino",
  "com.apple.quicktime.location.ISO6709": "+45.5137-122.6650+011.374/;+45.5137-122.6650+011.374/",
  "com.apple.quicktime.creationdate": "2026-09-12T16:43:52Z",
  "com.apple.quicktime.title": "A train passing through an urban setting",
  "com.apple.quicktime.description": "A commuter train moves through a city corridor.",
  "com.apple.quicktime.keywords": "train, urban, transit",
  "com.slate.original-filename": "iphone_17_pro_kino_prores_422.mov",
  "com.slate.app-version": "0.1.0",
  "com.slate.caption-model": "mlx-community/Qwen2-VL-2B-Instruct-4bit",
  "com.slate.generated-at": "2026-09-15T00:00:00Z",
  "keywords": "train, urban, transit",
  "title": "A train passing through an urban setting"
}
```

Matches the clean baseline shape exactly -- vendor tags single-valued and
untouched, new tags correct in both `Keys` and `ItemList`/`XMP-dc`, no
cross-contamination anywhere. Video still bit-for-bit identical; audio PCM
still differs (unchanged, pre-existing cost from Option B); `mebx` still
mislabeled by ffmpeg's muxer (unchanged, see the section below).

**Verdict:** implemented, in `src/slate/metadata.py`
(`_ffmpeg_write_keys_family()` is the ffmpeg pass; `embed_metadata()`
builds and runs the `ItemList`/`XMP-dc`-only exiftool pass after it
succeeds), covered by unit tests in `tests/test_metadata.py`
(`TestEmbedMetadataFfmpegKeysFamilySplit`). The only behavior change from
the pre-existing single-exiftool-call path is on files that already hit
`_TERMINATOR_ERROR_SIGNATURE` -- everything else is untouched. Known,
accepted costs carried over unchanged from Option B: the audio-timing
shift and `mebx` mislabeling, both orthogonal to the metadata-write fix
itself (see the next section).

## Follow-up: does isolating the `mebx` track help?

Tested directly against the fixture: extracting *only* the `mebx` track
before touching anything else --

```
ffmpeg -i in.mov -map 0:2 -c copy mebx_only.mov
```

-- still produces `Unknown hdlr_type for mebx, writing dummy values` and
the same `stts` fourcc mislabeling Option B found on the full-file remux.
**Isolating the track doesn't route around the bug**: it's a limitation in
ffmpeg's MOV muxer's ability to *write* a `mebx` handler type at all, not
something triggered by copying other tracks alongside it. Extract-then-
reinsert doesn't help either, for the same reason -- reinsertion is just
another ffmpeg mux operation, hitting the identical wall.

**A different muxer, however, handles it correctly.** GPAC's `MP4Box`
(found already installed via Homebrew on this machine) was tried as a
substitute for ffmpeg's remux:

```
MP4Box -add in.mov -new out.mov
```

Despite printing `Unknown box type mebx in parent stsd` (a benign log
line -- GPAC doesn't need to understand mebx's internal sample format to
preserve it), the result:

- `mebx` track survives with correct `codec_tag_string` (`mebx`, not
  `stts`) and `handler_name` intact -- confirmed via `ffprobe`.
- The raw 34-byte GPS sample payload (`+45.5137-122.6650+011.374/`) is
  byte-identical before/after, confirmed by extracting the elementary
  stream and diffing raw bytes directly (not just a container-level
  check).
- Video stream bit-for-bit identical (`ffmpeg -f md5` match), despite
  console output about "Adjusting ProRes compliancy" and a timescale
  change (600->2400) that looked concerning but didn't affect decoded
  output in this test.

**But MP4Box has the same audio-timing problem as ffmpeg** -- decoded PCM
differs from the original by the same kind of edit-list/offset shift
Option B found. So MP4Box fixes the `mebx` corruption but not the audio
issue; ffmpeg fixes neither; no single tool tested so far fixes both.
Whether MP4Box could also replace ffmpeg's role in the two-pass approach
above (i.e. does it likewise produce a `meta` atom exiftool can then write
to cleanly) was not tested -- MP4Box wasn't pursued further for this,
because a narrower fix was found instead (next section) that doesn't
require replacing ffmpeg's role in the `Keys`/`mdta` write at all.

## Follow-up: fixing the `mebx` mislabeling with Bento4

The key realization: the `mebx` sample *payload* never leaves `mdat`
during ffmpeg's remux -- confirmed repeatedly by extracting stream 2's raw
bytes from both the original file and ffmpeg's output and diffing them
(`cmp`, byte-identical, every time this was checked). Only two small boxes
inside `moov` get corrupted:

- `trak[N]/mdia/hdlr` -- the media handler type, silently rewritten from
  `meta` to `url ` (a generic placeholder ffmpeg's `mov` muxer uses for
  any track type it doesn't recognize)
- `trak[N]/mdia/minf/stbl/stsd` -- the sample description, replaced with a
  minimal 24-byte dummy entry tagged `stts` (the fourcc of an unrelated,
  differently-shaped box), discarding the real 152-byte `mebx` entry
  (8-byte header + 144-byte payload, containing a nested `keys` box that
  declares which metadata identifiers appear in the track -- required to
  interpret each sample's TLV payload)

So there's no need to extract/remux the `mebx` *stream* at all -- the fix
is swapping two small atoms, sourced from the **original** file, into the
file ffmpeg already wrote. Confirmed via `strings` on
`libavformat.dylib` (ffmpeg 9.0.1, Homebrew) that `mebx` doesn't appear
anywhere in the library -- there's no muxer flag or `-tag:d:0 mebx`
override that helps (tried; silently ignored), because there's no
mebx-aware code path to steer. This has to be a post-process, done outside
ffmpeg.

**Tool choice: Bento4's `mp4extract`/`mp4edit`, not GPAC's `MP4Box`.**
Installed via `brew install bento4` (conflicts with `mp4v2` -- both ship
`mp4extract`/`mp4info`; worth a preflight note if this becomes a hard
dependency). `mp4edit --replace <atom_path>:<source_file>` takes a
standalone atom file (header + payload) and splices it in at that path,
recalculating every ancestor box's size field itself -- no hand-rolled
`struct.pack` size-cascade math needed. `mp4extract <atom_path> <in>
<out>` pulls a given atom out into exactly that standalone form. Bento4
was chosen over MP4Box here because `mp4edit` is a general-purpose atom
surgery tool (arbitrary `--insert`/`--remove`/--`replace` by atom path),
which fits this fix's shape far more directly than MP4Box's whole-file
remux model.

**Detection**, via `mp4dump --format json --verbosity 1` (avoids
hand-rolling an ISO-BMFF box parser in Python -- Bento4 already parses the
tree correctly, including `mebx`, confirmed by its JSON showing the
nested `mebx` entry under `stsd` with the correct 144-byte payload size):
walk `moov.children` for each `trak`, resolve
`mdia.hdlr`/`mdia.minf.stbl.stsd`, and record the `stsd` box's first
child's fourcc. Compare that fourcc index-by-index between the **original**
file and ffmpeg's **output**. Video/audio never differ here (`-c copy`
preserves their real codec tags exactly); any index where they *do* differ
is a track ffmpeg dummy-valued.

**Repair**, for each mismatched index `N`:

```
mp4extract "moov/trak[N]/mdia/hdlr" original.mov hdlr_N.bin
mp4extract "moov/trak[N]/mdia/minf/stbl/stsd" original.mov stsd_N.bin
mp4edit --replace "moov/trak[N]/mdia/hdlr:hdlr_N.bin" \
        --replace "moov/trak[N]/mdia/minf/stbl/stsd:stsd_N.bin" \
        ffmpeg_output.mov repaired.mov
```

(Bento4's atom-path indices are 0-based -- confirmed empirically: an
unindexed `trak` path matches the *first* `trak`, `trak[2]` matches the
third.) The whole `stsd` box is replaced wholesale, not just the nested
entry, sidestepping any need to reason about whether ffmpeg's dummy
`stsd`'s internal size bookkeeping is even self-consistent.

**Verified end to end** against the real fixture, using the exact
mismatched-track index (2) found by the detection step above:

- `ffprobe`: `codec_tag_string` back to `mebx` (was `stts`)
- `mp4dump`: `handler_type = meta` (was `url `), `stsd` now contains a
  nested `[mebx] size=8+144` entry, byte-for-byte matching the original
- Raw `mebx` sample payload: still byte-identical (it always was; this
  only fixes the wrapper describing it)
- Video stream MD5: unchanged
- `com.apple.quicktime.title`/`model`/`make` (the `Keys` metadata ffmpeg
  wrote in the earlier pass): all intact, untouched by the atom swap

This closes the `mebx` half of Option B's known costs with zero effect on
anything else in the pipeline. The audio-timing shift is untouched by this
fix (orthogonal -- it comes from ffmpeg's edit-list/offset handling during
the `-c copy` remux itself, not from anything in `moov`'s `trak`
structure) and remains open, as noted in Option B.

## Follow-up: is this bug specific to Kino, or does any iPhone ProRes recording trigger it?

Tested against `tests/fixtures/footage/iphone_17_pro_momentpro_prores_422.MOV`
(119MB, checked in 2026-09-16) -- a recording from the Moment Pro app, on
the *same* iPhone 17 Pro as the Kino fixture, also ProRes 422 (`apcn`).
This is the closest thing to a controlled minimal pair available: same
phone, same underlying Apple ProRes video encoder, different third-party
camera app.

**Result: neither bug reproduces.**

- A direct combined `exiftool -ItemList:Title=... -Keys:Title=...
  -XMP-dc:Title=...` write (the exact repro shape that triggers "Terminator
  found in Meta" on every Kino file tested) succeeded immediately --
  `1 image files updated`, exit 0, no error at all.
- The real, unmodified `slate --process-and-rename --add-metadata
  --verbose` pipeline run against a copy confirms the same: the first
  combined exiftool call inside `embed_metadata()` succeeds directly,
  `_TERMINATOR_ERROR_SIGNATURE` is never hit, and `_ffmpeg_write_keys_family()`
  never runs. `ffprobe` afterward shows all of Moment Pro's original
  vendor tags (`com.apple.quicktime.make` = `Apple`,
  `model` = `iPhone Camera`, `software` = `Moment 1.3.4`, `displayname`,
  `creationdate`, `location.ISO6709`) intact and single-valued, alongside
  slate's new Title/Description/Keywords/`com.slate.*` fields -- a clean
  write, first try, no fallback needed.
- This file also has **no `mebx` track at all** -- only 2 streams (video
  `apcn`/ProRes422, audio `lpcm`/`pcm_s16le` -- notably PCM, not AAC like
  the Kino file's audio). So the Bento4 repair path has nothing to engage
  with here either: the stsd-fourcc-comparison detection in the section
  above correctly finds zero mismatched tracks and no-ops.

**This points at each app's own metadata/`meta`-atom construction (and
each app's own choice whether to record a `mebx` timed-metadata track at
all), not at Apple's shared ProRes encoding pipeline.** Same phone
hardware, same video codec, same underlying frameworks for the actual
ProRes encode -- yet one app (Kino) hits both the exiftool parser bug and
needs the `mebx` repair, the other (Moment Pro) hits neither and never
recorded a `mebx` track to begin with. Evidence, not proof -- only two
apps/files have been tested -- but it directly rules out the strongest
form of "this happens on any iPhone ProRes footage." Also validates the
detection logic's design: neither fix branches on app identity or file
metadata like `com.apple.quicktime.software` -- both trigger purely on
observed symptoms (exiftool's error text; a `stsd` fourcc mismatch), so a
file that doesn't have either problem is naturally left untouched with no
per-app special-casing required.

## Where this leaves things

The write-failure fix is shipped: `metadata.embed_metadata()` detects
`_TERMINATOR_ERROR_SIGNATURE`, hands the entire `Keys`/`mdta` family to
`_ffmpeg_write_keys_family()`, and runs a narrower `ItemList`/`XMP-dc`-only
exiftool pass afterward. The `mebx` mislabeling that pass's remux
introduces is also fixed, via a third, Bento4-based repair step (see
"Follow-up: fixing the `mebx` mislabeling with Bento4"). Every file that
doesn't hit the exiftool bug is completely unaffected -- the single
combined exiftool call is still the first thing tried, unchanged; every
file without a `mebx`-shaped track mismatch is unaffected by the repair
step too. Concretely unresolved for future work:

- The audio-timing shift from Option B remains unsolved and orthogonal to
  both fixes above -- it comes from ffmpeg's own edit-list/offset handling
  during the `-c copy` remux, not from anything the `mebx` repair or the
  atom-family split touch.
- Whether to add an opt-in `-m`-equivalent flag for users who've reviewed
  this doc and want the Option A tradeoff on a specific file anyway, now
  that the atom-family split exists as the shipped default fix.
- Whether to report the underlying exiftool bug upstream (repro is
  documented above, now backed by a checked-in fixture) and revisit once/
  if it's fixed.
- Whether exiftool's Keys-append-collision bug (root-caused above, in
  "First attempt (retry the same combined write)") is itself worth
  reporting upstream alongside the original `Terminator found in Meta`
  bug -- a second, independent exiftool defect found during this
  investigation, distinct from the one this doc originally opened with.
- Whether the exiftool bug and the `mebx` mislabeling are Kino-specific or
  broader across third-party camera apps -- one additional app (Moment
  Pro) was tested and hits neither (see "Follow-up: is this bug specific
  to Kino..."), but that's two data points, not a survey. Worth revisiting
  if/when more third-party-app ProRes fixtures turn up.

**Resolved (2026-09-17):** `bento4` is now checked at startup via a new
`preflight.run_metadata_tool_checks()`, gated on
`--add-metadata`/`--metadata-backfill` being passed (see "Preflight
Checks" in `PROJECT_SPEC.md`). This went through two designs the same
day: the first made it advisory (a startup warning, never fatal), on the
theory that the `mebx` repair it enables is best-effort on top of a
Keys/mdta write that already succeeds without it. That missed a sharper
point, surfaced by working through the failure mode explicitly: once
`_ffmpeg_write_keys_family()` has run on a file without Bento4 present,
that file's `mebx` damage can never be fixed by a *later* run, even after
installing Bento4 -- the correct `hdlr`/`stsd` atoms only exist in the
pre-remux bytes, which that same call's `tmp_output.replace(path)`
overwrites moments after reading them (see "Follow-up: fixing the `mebx`
mislabeling with Bento4," above). A silent warning was therefore only
ever one missed line of terminal output away from irreversible,
undetected degradation. The check is now fatal, at the same severity as
`exiftool`/`ffmpeg`'s checks -- `slate` refuses to start with either flag
if `bento4` isn't installed, full stop -- just conditional on those two
flags rather than unconditional the way `exiftool`'s check is. Documented
in `README.md`'s Prerequisites and "Embedding Metadata" sections
alongside `exiftool`/`ffmpeg`.

The Kino-vs-Moment-Pro contrast this doc investigates by hand above is
also now covered by a real-fixture integration suite,
`tests/integration/test_metadata_write_quirks.py` (tagged
`@pytest.mark.kino`/`@pytest.mark.momentpro`, runnable independently via
`pytest -m kino`/`pytest -m momentpro`): it asserts Kino triggers the
`_ffmpeg_write_keys_family()` fallback and gets its `mebx` track repaired
when Bento4 is present, while Moment Pro's write succeeds without ever
invoking that fallback -- plus a vendor-tag survival check on both. A
further test, `test_kino_mebx_track_stays_mislabeled_without_bento4`,
simulates Bento4's absence at the `metadata.py` function level (not
reachable through the CLI anymore now that `bento4` is preflight-required)
to document, precisely, the unrecoverable state this whole fix exists to
prevent: `embed_metadata()` still reports success, but the file's `mebx`
track is left permanently mislabeled.
