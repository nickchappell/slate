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
        return EmbedOutcome(
            embedded=False, preserved_fields=preserved_fields, error=error
        )

    return EmbedOutcome(embedded=True, preserved_fields=preserved_fields)
