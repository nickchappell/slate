# Metadata Write Failures on Malformed `meta` Atoms

**Status:** investigation complete, no fix implemented. Current behavior
(skip-and-warn) is unchanged and believed correct. This doc exists so the
investigation doesn't have to be redone before deciding whether/how to act
on it.

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

## Where this leaves things

Current behavior (skip-and-warn, filename rename still succeeds, metadata
silently not written for this file) stands as correct given both
alternatives investigated cause real, verified damage. Concretely
unresolved for future work:

- Whether to add an opt-in `-m`-equivalent flag for users who've reviewed
  this doc and want the tradeoff on a specific file, and how to word its
  warning so it's not just generic boilerplate.
- Whether to report the underlying exiftool bug upstream (repro is
  documented above) and revisit once/if it's fixed.
- Whether the ffmpeg path is worth narrowing (e.g. gate on "no `mebx`
  track present") rather than abandoning outright -- not attempted here.
- No code changes were made as part of this investigation; `metadata.py`
  and `cli.py`'s handling of this failure mode are unchanged.
