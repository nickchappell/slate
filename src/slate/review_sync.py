from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from slate.filenames import assemble_stem, normalize_caption, truncate_caption
from slate.mappings import MappingEntry

# See "Workflow Modes" in PROJECT_SPEC.md: a human reviews captions by
# renaming preview JPEGs directly in review/ (instead of, or in addition to,
# hand-editing new_stem in rename_mappings.json). Since that rename happens
# out of band of the script, this module reconciles it before Phase 2 builds
# its rename plan -- a JPEG's SHA-256 is the durable link back to its
# MappingEntry, since a plain file rename never touches file bytes.


def hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class SyncResult:
    renamed: list[MappingEntry] = field(default_factory=list)
    deleted: list[MappingEntry] = field(default_factory=list)
    ambiguous_hashes: list[str] = field(default_factory=list)


def sync_from_review(entries: list[MappingEntry], review_dir: Path) -> SyncResult:
    """Mutates matched entries' new_stem/preview_jpeg in place to reflect a
    human's rename of their preview JPEG in review_dir. Entries whose
    preview JPEG can no longer be found by hash (deleted, not renamed) are
    reported in `.deleted` for the caller to exclude from the rename plan --
    left otherwise untouched, so a future run keeps warning rather than
    silently reverting to the originally generated name. Entries with no
    recorded preview_jpeg_sha256 (older mapping files predating this field)
    are ignored entirely, same as before this existed."""
    result = SyncResult()

    by_hash: dict[str, list[MappingEntry]] = {}
    for entry in entries:
        if entry.status == "ok" and entry.preview_jpeg_sha256:
            by_hash.setdefault(entry.preview_jpeg_sha256, []).append(entry)

    if not by_hash:
        return result

    if not review_dir.is_dir():
        result.deleted = [e for group in by_hash.values() for e in group]
        return result

    seen: set[int] = set()
    for jpeg_path in sorted(review_dir.glob("*.jpg")):
        digest = hash_file(jpeg_path)
        matches = by_hash.get(digest)
        if not matches:
            continue

        if len(matches) > 1:
            result.ambiguous_hashes.append(digest)
            seen.update(id(e) for e in matches)
            continue

        entry = matches[0]
        seen.add(id(entry))
        new_stem = jpeg_path.stem
        if new_stem != entry.new_stem:
            entry.new_stem = new_stem
            entry.preview_jpeg = jpeg_path.name
            # Persisted, not just a same-run marker -- see
            # MappingEntry.short_caption_locked's docstring: this entry's
            # save (below, in the caller) can outlive this run if the
            # confirmation prompt is later declined, so a *future* run
            # needs to still know this name came from a human JPEG rename.
            entry.short_caption_locked = True
            result.renamed.append(entry)

    for group in by_hash.values():
        for entry in group:
            if id(entry) not in seen:
                result.deleted.append(entry)

    return result


def reconcile_short_caption_edits(
    entries: list[MappingEntry],
    *,
    prefix: str,
    suffix: str,
    prepend: bool,
    max_file_name_length: int,
) -> list[MappingEntry]:
    """Recomputes new_stem from a hand-edited short_caption -- see
    "Reconciliation & precedence" in spec/metadata-embedding.md.

    Scoped to "ok" entries generated under --add-metadata (long_caption and
    keywords both populated); non-add-metadata entries have no short_caption
    to diverge from and keep today's direct-new_stem-editing workflow
    completely untouched. Within that scope, short_caption is the
    authoritative source for the caption portion of new_stem -- but only
    for entries that aren't `short_caption_locked`: a JPEG rename is the
    more direct, unambiguous "this is exactly what I want it called" signal
    and wins over a short_caption edit, per the design doc's decided
    precedence. Checking the persisted flag (not just "did sync_from_review
    change anything in this same call") matters because sync_from_review's
    save can outlive a declined/interrupted run -- a later run where the
    JPEG already matches what's stored has nothing left to sync, but must
    still remember the name came from a human JPEG rename, not from
    short_caption, or it would silently clobber that choice back.

    Idempotent when short_caption wasn't edited (recomputing from it
    reproduces the same new_stem). Mutates new_stem in place on entries
    that changed; returns just those entries, for the caller to re-save."""
    changed: list[MappingEntry] = []

    for entry in entries:
        if entry.status != "ok":
            continue
        if entry.long_caption is None or entry.keywords is None:
            continue
        if entry.short_caption is None:
            continue
        if entry.short_caption_locked:
            continue

        original_stem = Path(entry.original_files[0]).stem
        caption = truncate_caption(normalize_caption(entry.short_caption))
        new_stem = assemble_stem(
            original_stem=original_stem,
            caption=caption,
            prefix=prefix,
            suffix=suffix,
            prepend_caption=prepend,
            max_length=max_file_name_length,
        )

        if new_stem != entry.new_stem:
            entry.new_stem = new_stem
            changed.append(entry)

    return changed
