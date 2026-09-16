from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# See "Writing mechanism" and "Pre-write collision check" in
# spec/metadata-embedding.md: shell out to exiftool (not a Python library,
# not ffmpeg) to write Title/Description/Keywords across all three metadata
# containers (classic udta atoms, com.apple.quicktime.* keys, XMP) plus
# slate's own com.slate.* provenance namespace.
#
# Tag syntax below is empirically verified against real exiftool (v13.55)
# and a synthetic ProRes .mov -- not just inferred from documentation:
# - Classic udta atoms live in exiftool's "ItemList" group: -ItemList:Title=,
#   -ItemList:Description=, -ItemList:Keyword= (singular -- confirmed NOT a
#   list-type tag; repeated -ItemList:Keyword= flags overwrite rather than
#   append, so it's written once as a comma-joined string, like Keys below).
# - The com.apple.quicktime.* mechanism is exiftool's "Keys" group:
#   -Keys:Title=, -Keys:Description=, -Keys:Keywords= (plural -- also
#   confirmed NOT list-type; comma-joined string, matching the design doc's
#   field-mapping table). Per QuickTime.pm's Keys table: writing a bare
#   -Title=/-Description= without a group prefix resolves ambiguously to
#   ItemList (which exiftool prefers when writing), NOT Keys -- so Keys:
#   must be specified explicitly to reach the quicktime.* namespace, e.g.
#   `-Keys:Title=`.
# - XMP-dc:Subject is confirmed to be a true list-type tag -- repeated
#   -XMP-dc:Subject= flags genuinely append, unlike the two tag families
#   above. This matches the design doc's claim that dc:subject is "the one
#   field of the three families that's a true list rather than a joined
#   string."
# - Custom com.slate.* keys require an ExifTool user-defined-tag config
#   (arbitrary dotted tag names are rejected outright on the command line
#   with "Invalid tag name" -- there is no way around this). Verified via
#   QuickTime.pm's %Keys table itself: an entry whose hash key already
#   starts with "com." (see the built-in 'com.android.*' entries) is
#   written to the moov atom as that literal string, with NO
#   "com.apple.quicktime." prefix added -- exactly the mechanism GoPro/DJI
#   rely on for their own vendor keys, and confirmed here by grepping the
#   written file's raw bytes for the literal string "com.slate.app-version".
#   _EXIFTOOL_CONFIG below defines exactly seven such entries: the four
#   com.slate.* provenance keys, plus the three com.slate.original-* keys
#   the pre-write collision check preserves into.

_EXIFTOOL_CONFIG = """\
%Image::ExifTool::UserDefined = (
    'Image::ExifTool::QuickTime::Keys' => {
        'com.slate.original-filename'    => 'SlateOriginalFilename',
        'com.slate.app-version'          => 'SlateAppVersion',
        'com.slate.caption-model'        => 'SlateCaptionModel',
        'com.slate.generated-at'         => 'SlateGeneratedAt',
        'com.slate.original-title'       => 'SlateOriginalTitle',
        'com.slate.original-description' => 'SlateOriginalDescription',
        'com.slate.original-keywords'    => 'SlateOriginalKeywords',
    },
);
1;  #end
"""

SUBPROCESS_TIMEOUT = 60  # matches extraction.py's convention

# Known exiftool minor-error signatures that are confirmed recoverable via
# the ffmpeg/exiftool atom-family split in embed_metadata() below (see
# _ffmpeg_write_keys_family's docstring) -- each entry here must be backed
# by a real repro fixture and a write-up in
# spec/metadata-write-corruption.md before being added, the same way the
# first one was. Deliberately NOT "any exiftool [minor] error": that
# severity class covers unrelated warnings (a truncated thumbnail, an odd
# codec profile, ...) that have nothing to do with the vendor `meta` atom
# parsing bug this list exists for, and routing those through the ffmpeg
# remux would take on its costs (mebx-family mislabeling, audio-timing
# shift) for a file that never needed them.
#
# - "Terminator found in" -- Image::ExifTool::WriteQuickTime.pl:1063, a
#   real exiftool bug misparsing certain vendor-written top-level `meta`
#   atoms (seen on Lux Optics "Kino" recordings), not a corrupt file.
#   Deliberately not the full message (which includes a byte count that
#   varies per file).
_RECOVERABLE_WRITE_ERROR_SIGNATURES = ("Terminator found in",)

# See "Follow-up: fixing the mebx mislabeling with Bento4" in
# spec/metadata-write-corruption.md. ffmpeg's mov muxer (used by
# _ffmpeg_write_keys_family above) writes a dummy 24-byte sample
# description and mangles the media handler type for any track type it
# doesn't recognize -- a Kino-style `mebx` timed-metadata track, for
# instance -- while leaving that track's actual sample payload in `mdat`
# byte-identical (confirmed via direct payload extraction/diff). Detected
# by comparing each trak's stsd entry fourcc, via Bento4's `mp4dump
# --format json`, between the pre-remux file and ffmpeg's output --
# video/audio never differ here since `-c copy` preserves their real codec
# tags -- and repaired by splicing the real `hdlr`/`stsd` atoms from the
# pre-remux file into ffmpeg's output via `mp4extract`/`mp4edit`, which
# recalculate every ancestor box's size field themselves (no hand-rolled
# ISO-BMFF size-cascade math). Strictly best-effort: a no-op, not a
# failure, whenever Bento4 (`brew install bento4`) isn't installed --
# _ffmpeg_write_keys_family()'s Keys/mdta write already succeeded by the
# time this runs, so this is purely an enhancement on top of that, never a
# reason to fail the embed.

# The two atoms that identify a track's type/format -- mdia-level hdlr
# (media handler type) and the stsd sample description ffmpeg dummy-values
# together (see the comment above). Named once here so adding a third atom
# to repair later (e.g. the edit list, if the separate audio-timing-shift
# cost is ever tackled) is a one-line change instead of touching every
# place that currently spells out "mdia/hdlr" / "mdia/minf/stbl/stsd".
_TRACK_DESCRIPTION_ATOM_PATHS = ("mdia/hdlr", "mdia/minf/stbl/stsd")


def _dump_trak_stsd_fourccs(path: Path) -> list[str | None] | None:
    """Returns the first stsd-child fourcc for each `trak` in `path`'s
    `moov`, in file order, via Bento4's `mp4dump` -- or None if `mp4dump`
    isn't installed or the output can't be parsed as expected. A per-track
    None (rather than the whole call returning None) means that
    particular trak's stsd shape wasn't where expected -- e.g. a
    non-timed-metadata track type this function has no reason to expect --
    and it's excluded from the mismatch comparison rather than guessed at.
    Never raises."""
    try:
        result = subprocess.run(
            ["mp4dump", "--format", "json", "--verbosity", "1", str(path)],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
    except subprocess.TimeoutExpired, OSError:
        return None

    if result.returncode != 0:
        return None

    try:
        boxes = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    moov = next((b for b in boxes if b.get("name") == "moov"), None)
    if moov is None:
        return None

    return [
        _first_stsd_entry_fourcc(trak)
        for trak in moov.get("children", [])
        if trak.get("name") == "trak"
    ]


def _first_stsd_entry_fourcc(trak: dict) -> str | None:
    node = trak
    for name in ("mdia", "minf", "stbl", "stsd"):
        node = next(
            (c for c in node.get("children", []) if c.get("name") == name), None
        )
        if node is None:
            return None
    entries = node.get("children") or []
    return entries[0].get("name") if entries else None


def _extract_atom(path: Path, atom_path: str) -> Path | None:
    try:
        with tempfile.NamedTemporaryFile(
            prefix="slate-atom-", suffix=".bin", delete=False
        ) as f:
            out_path = Path(f.name)
        result = subprocess.run(
            ["mp4extract", atom_path, str(path), str(out_path)],
            capture_output=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
    except subprocess.TimeoutExpired, OSError:
        return None

    if result.returncode != 0 or not out_path.is_file() or out_path.stat().st_size == 0:
        out_path.unlink(missing_ok=True)
        return None
    return out_path


def _repair_mislabeled_data_tracks(original: Path, remuxed: Path) -> None:
    """Best-effort repair of ffmpeg's dummy-valued track(s) in `remuxed`
    (still a temp file at this point, not yet swapped into the real path),
    using atoms sourced from `original` (the pre-remux file, which
    _ffmpeg_write_keys_family's caller has not yet overwritten). Mutates
    `remuxed` in place on success; leaves it untouched on any failure or
    when nothing needs repairing. See the module comment above this
    function for the detection/repair mechanism."""
    original_fourccs = _dump_trak_stsd_fourccs(original)
    remuxed_fourccs = _dump_trak_stsd_fourccs(remuxed)
    if (
        original_fourccs is None
        or remuxed_fourccs is None
        or len(original_fourccs) != len(remuxed_fourccs)
    ):
        return

    mismatched = [
        i
        for i, (orig, new) in enumerate(
            zip(original_fourccs, remuxed_fourccs, strict=True)
        )
        if orig is not None and orig != new
    ]
    if not mismatched:
        return

    atom_files: list[Path] = []
    tmp_repaired: Path | None = None
    try:
        replace_args: list[str] = []
        for i in mismatched:
            track_atom_files: list[Path] = []
            for atom_path in _TRACK_DESCRIPTION_ATOM_PATHS:
                atom_file = _extract_atom(original, f"moov/trak[{i}]/{atom_path}")
                if atom_file is None:
                    track_atom_files = []
                    break
                # Tracked for cleanup immediately, even if a later atom in
                # this same track fails and the whole track gets skipped
                # below -- otherwise a partial extraction would leak a
                # stray temp file.
                atom_files.append(atom_file)
                track_atom_files.append(atom_file)

            if not track_atom_files:
                continue

            for atom_path, atom_file in zip(
                _TRACK_DESCRIPTION_ATOM_PATHS, track_atom_files, strict=True
            ):
                replace_args += [
                    "--replace",
                    f"moov/trak[{i}]/{atom_path}:{atom_file}",
                ]

        if not replace_args:
            return

        with tempfile.NamedTemporaryFile(
            dir=remuxed.parent,
            prefix=".slate-mebx-repair-",
            suffix=remuxed.suffix,
            delete=False,
        ) as f:
            tmp_repaired = Path(f.name)

        result = subprocess.run(
            ["mp4edit", *replace_args, str(remuxed), str(tmp_repaired)],
            capture_output=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
        if (
            result.returncode != 0
            or not tmp_repaired.is_file()
            or tmp_repaired.stat().st_size == 0
        ):
            return

        tmp_repaired.replace(remuxed)
        tmp_repaired = None
    except subprocess.TimeoutExpired, OSError:
        return
    finally:
        for atom_file in atom_files:
            atom_file.unlink(missing_ok=True)
        if tmp_repaired is not None:
            tmp_repaired.unlink(missing_ok=True)


# Lazily written once per process and reused -- avoids bundling a non-.py
# resource file (packaging-config-free) while not re-writing it on every
# exiftool call. Left on disk at process exit (tiny, inert); OS temp
# cleanup handles it eventually, same as any other stray temp file.
_config_path: Path | None = None


def _exiftool_config_path() -> Path:
    global _config_path
    if _config_path is None:
        f = tempfile.NamedTemporaryFile(
            mode="w", suffix=".config", prefix="slate-exiftool-", delete=False
        )
        f.write(_EXIFTOOL_CONFIG)
        f.close()
        _config_path = Path(f.name)
    return _config_path


@dataclass
class ExistingMetadata:
    title: str | None
    description: str | None
    keywords: str | None
    has_slate_provenance: bool


@dataclass
class EmbedOutcome:
    embedded: bool
    preserved_fields: list[str] = field(default_factory=list)
    error: str | None = None


def _empty_metadata() -> ExistingMetadata:
    return ExistingMetadata(
        title=None, description=None, keywords=None, has_slate_provenance=False
    )


def read_existing_metadata(path: Path) -> ExistingMetadata:
    """Single exiftool -j read call covering Title/Description/Keywords
    plus com.slate.* provenance presence -- folds the backfill idempotency
    check into this same call, per the design doc's decision that it
    doesn't need its own subprocess round-trip. Never raises: any
    subprocess failure, non-zero exit, or unparseable output is treated as
    "nothing to preserve, no provenance seen" -- the caller's write attempt
    is what actually surfaces failure to the user, not this read.

    Bare -Title/-Description/-Keywords (no group prefix) is deliberately
    ambiguous across ItemList/Keys/XMP-dc when more than one is populated
    -- exiftool silently picks one "preferred" value per name. This is
    accepted: slate itself always writes all three families together (so
    they agree on a slate-written file), and the collision check only
    needs "is there *something* pre-existing to preserve," not which
    specific family it came from."""
    cmd = [
        "exiftool",
        "-config",
        str(_exiftool_config_path()),
        "-j",
        "-Title",
        "-Description",
        "-Keywords",
        "-SlateAppVersion",
        str(path),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT
        )
    except subprocess.TimeoutExpired, OSError:
        return _empty_metadata()

    if result.returncode != 0:
        return _empty_metadata()

    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return _empty_metadata()

    if not parsed:
        return _empty_metadata()

    entry = parsed[0]

    raw_keywords = entry.get("Keywords")
    if isinstance(raw_keywords, list):
        keywords = ", ".join(str(k) for k in raw_keywords) or None
    else:
        keywords = raw_keywords or None

    return ExistingMetadata(
        title=entry.get("Title") or None,
        description=entry.get("Description") or None,
        keywords=keywords,
        has_slate_provenance=bool(entry.get("SlateAppVersion")),
    )


def _ffmpeg_write_keys_family(
    path: Path,
    *,
    title: str,
    description: str,
    keywords: list[str],
    original_filename: str,
    app_version: str,
    caption_model: str,
    generated_at: str,
    existing: ExistingMetadata,
) -> bool:
    """Repairs the file's top-level `meta` atom AND writes the entire
    Keys/mdta tag family itself, in one ffmpeg stream-copy remux -- see
    "Follow-up: two-pass ffmpeg + exiftool" in
    spec/metadata-write-corruption.md for why exiftool must never be the
    one to append to this atom on a file that hit one of
    _RECOVERABLE_WRITE_ERROR_SIGNATURES: exiftool appending new Keys entries onto
    a Keys/mdta atom it didn't originally build lands on top of existing
    low-numbered vendor slots (Model/Make/Software/...) instead of
    allocating fresh ones, silently concatenating slate's new values into
    the vendor's -- invisible to exiftool's own reads (it resolves the
    collision by name) but visible, and real, to any other reader (verified
    via `ffprobe`). Routing every Keys-family write through ffmpeg instead
    avoids that append path entirely.

    `-map_metadata 0` carries the file's existing tags through, so the
    pre-existing com.apple.quicktime.* vendor tags survive; the new
    Title/Description/Keywords, any pre-existing values being preserved
    into com.slate.original-*, and the four com.slate.* provenance keys
    are all written here via `-metadata`, using the fully-qualified
    com.apple.quicktime.*/com.slate.* key names -- not the bare
    title=/description=/keywords= form, which writes a differently-named
    key that collides with ItemList's own same-named key on read instead
    of exiftool's Keys-group naming (confirmed via `ffprobe`: bare names
    show up as the same value duplicated with a literal ";" joining it to
    itself). No exiftool -config is needed here -- ffmpeg accepts
    arbitrary metadata key names as-is, unlike exiftool's custom-tag
    rejection of bare dotted names.

    embed_metadata()'s caller runs a second, ItemList/XMP-dc-only exiftool
    write immediately after this succeeds -- this function must never
    touch those two families itself, so there is exactly one writer per
    atom family and no double-write to reconcile.

    Video is unaffected (verified bit-for-bit identical in testing); audio
    decode timing shifts slightly (known, accepted cost -- see "Option B"
    in the design doc), since ffmpeg's muxer rewrites edit lists. A
    Kino-style mebx track's sample description/handler type also gets
    dummy-valued by this remux (ffmpeg's muxer doesn't understand that
    handler type), but _repair_mislabeled_data_tracks() below attempts to
    fix that as a best-effort follow-up before this file is swapped in --
    see its module comment for the mechanism. Returns False (path left
    untouched) on any subprocess failure; True means path now holds the
    rewritten (and, when applicable, mebx-repaired) file, swapped in via
    Path.replace so a crash mid-remux can never leave a half-written file
    at the real path.
    """
    tmp_output: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".slate-repair-", suffix=path.suffix, delete=False
        ) as f:
            tmp_output = Path(f.name)

        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(path),
            "-map",
            "0",
            "-c",
            "copy",
            "-map_metadata",
            "0",
            "-movflags",
            "use_metadata_tags",
        ]

        if existing.title:
            cmd += ["-metadata", f"com.slate.original-title={existing.title}"]
        if existing.description:
            cmd += [
                "-metadata",
                f"com.slate.original-description={existing.description}",
            ]
        if existing.keywords:
            cmd += ["-metadata", f"com.slate.original-keywords={existing.keywords}"]

        joined_keywords = ", ".join(keywords)
        cmd += [
            "-metadata",
            f"com.apple.quicktime.title={title}",
            "-metadata",
            f"com.apple.quicktime.description={description}",
            "-metadata",
            f"com.apple.quicktime.keywords={joined_keywords}",
            "-metadata",
            f"com.slate.original-filename={original_filename}",
            "-metadata",
            f"com.slate.app-version={app_version}",
            "-metadata",
            f"com.slate.caption-model={caption_model}",
            "-metadata",
            f"com.slate.generated-at={generated_at}",
            str(tmp_output),
        ]

        result = subprocess.run(cmd, capture_output=True, timeout=SUBPROCESS_TIMEOUT)
        if (
            result.returncode != 0
            or not tmp_output.is_file()
            or tmp_output.stat().st_size == 0
        ):
            return False

        # `path` still holds the pre-remux bytes here -- the atom source
        # this repair needs -- since the swap below hasn't happened yet.
        _repair_mislabeled_data_tracks(original=path, remuxed=tmp_output)

        tmp_output.replace(path)
        tmp_output = None
        return True
    except subprocess.TimeoutExpired, OSError:
        return False
    finally:
        if tmp_output is not None:
            tmp_output.unlink(missing_ok=True)


def embed_metadata(
    path: Path,
    *,
    title: str,
    description: str,
    keywords: list[str],
    original_filename: str,
    app_version: str,
    caption_model: str,
    generated_at: str,
) -> EmbedOutcome:
    """Read-before-write collision check, then one exiftool write call.

    Preserves any non-empty pre-existing Title/Description/Keywords into
    matching com.slate.original-* fields before overwriting them (see
    "Pre-write collision check" in the design doc) -- applies independently
    per field. Then writes Title/Description/Keywords across all three
    metadata containers plus the four com.slate.* provenance keys, with
    -overwrite_original (no backup copy -- see "Writing mechanism": a
    backup twin per clip would roughly double footage-directory disk usage
    across a batch run).

    Only ever touches the tags named explicitly below -- never -all= or an
    equivalent blanket clear -- so vendor-proprietary tracks (GoPro GPMF,
    DJI atoms, a Kino/Halide-style mebx timed-metadata track) and genuine
    camera-sourced fields (make/model/creationdate/location) are untouched
    regardless of manufacturer; this is a property of exiftool + the
    container format, not something this function has to guard separately.
    One narrow exception: if this write hits one of
    _RECOVERABLE_WRITE_ERROR_SIGNATURES (see
    spec/metadata-write-corruption.md), ownership of the three tag
    families splits in two -- _ffmpeg_write_keys_family() takes over the
    entire Keys/mdta family (see its docstring for why exiftool must not
    be the one appending to that atom on a file shaped like this) and a
    second, ItemList/XMP-dc-only exiftool write follows it. The ffmpeg
    remux that pass runs dummy-values a Kino-style mebx track's sample
    description/handler type; _ffmpeg_write_keys_family() attempts to
    repair that via Bento4 before swapping the file in (best-effort --
    silently left as-is if Bento4 isn't installed). Only triggers on a
    file whose metadata write was already failing outright.

    Never raises -- reports subprocess/exiftool failure via
    EmbedOutcome(embedded=False, error=...). Skip-and-warn on failure is
    the caller's policy (see "Error handling for exiftool write failures"),
    not this function's."""
    existing = read_existing_metadata(path)

    preserved_fields: list[str] = []
    cmd = [
        "exiftool",
        "-config",
        str(_exiftool_config_path()),
        "-overwrite_original",
    ]

    if existing.title:
        cmd.append(f"-Keys:SlateOriginalTitle={existing.title}")
        preserved_fields.append("Title")
    if existing.description:
        cmd.append(f"-Keys:SlateOriginalDescription={existing.description}")
        preserved_fields.append("Description")
    if existing.keywords:
        cmd.append(f"-Keys:SlateOriginalKeywords={existing.keywords}")
        preserved_fields.append("Keywords")

    joined_keywords = ", ".join(keywords)
    cmd += [
        f"-ItemList:Title={title}",
        f"-Keys:Title={title}",
        f"-XMP-dc:Title={title}",
        f"-ItemList:Description={description}",
        f"-Keys:Description={description}",
        f"-XMP-dc:Description={description}",
        # ItemList:Keyword (singular) and Keys:Keywords are both
        # comma-joined strings, not list-type tags -- confirmed empirically
        # (repeated flags overwrite, don't append). XMP-dc:Subject is the
        # one true list among the three families -- one flag per value.
        f"-ItemList:Keyword={joined_keywords}",
        f"-Keys:Keywords={joined_keywords}",
    ]
    for kw in keywords:
        cmd.append(f"-XMP-dc:Subject={kw}")

    cmd += [
        f"-Keys:SlateOriginalFilename={original_filename}",
        f"-Keys:SlateAppVersion={app_version}",
        f"-Keys:SlateCaptionModel={caption_model}",
        f"-Keys:SlateGeneratedAt={generated_at}",
        str(path),
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return EmbedOutcome(
            embedded=False, preserved_fields=preserved_fields, error=str(e)
        )

    if result.returncode != 0:
        error = (result.stderr or f"exiftool exited {result.returncode}").strip()

        if any(sig in error for sig in _RECOVERABLE_WRITE_ERROR_SIGNATURES):
            if not _ffmpeg_write_keys_family(
                path,
                title=title,
                description=description,
                keywords=keywords,
                original_filename=original_filename,
                app_version=app_version,
                caption_model=caption_model,
                generated_at=generated_at,
                existing=existing,
            ):
                return EmbedOutcome(
                    embedded=False, preserved_fields=preserved_fields, error=error
                )

            # Keys/mdta is done; a second, narrower exiftool call handles
            # the two families _ffmpeg_write_keys_family() must never touch.
            # No -config -- ItemList/XMP-dc are standard tags, not
            # slate's custom com.slate.* ones.
            itemlist_cmd = [
                "exiftool",
                "-overwrite_original",
                f"-ItemList:Title={title}",
                f"-XMP-dc:Title={title}",
                f"-ItemList:Description={description}",
                f"-XMP-dc:Description={description}",
                f"-ItemList:Keyword={joined_keywords}",
            ]
            for kw in keywords:
                itemlist_cmd.append(f"-XMP-dc:Subject={kw}")
            itemlist_cmd.append(str(path))

            try:
                itemlist_result = subprocess.run(
                    itemlist_cmd,
                    capture_output=True,
                    text=True,
                    timeout=SUBPROCESS_TIMEOUT,
                )
            except (subprocess.TimeoutExpired, OSError) as e:
                return EmbedOutcome(
                    embedded=False, preserved_fields=preserved_fields, error=str(e)
                )

            if itemlist_result.returncode == 0:
                return EmbedOutcome(embedded=True, preserved_fields=preserved_fields)

            itemlist_error = (
                itemlist_result.stderr
                or f"exiftool exited {itemlist_result.returncode}"
            ).strip()
            return EmbedOutcome(
                embedded=False,
                preserved_fields=preserved_fields,
                error=(
                    "Keys/mdta metadata written via ffmpeg repair, but the "
                    f"ItemList/XMP write failed afterward: {itemlist_error}"
                ),
            )

        return EmbedOutcome(
            embedded=False, preserved_fields=preserved_fields, error=error
        )

    return EmbedOutcome(embedded=True, preserved_fields=preserved_fields)
