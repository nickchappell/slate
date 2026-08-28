from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

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
            entry.preview_jpeg = str(Path(review_dir.name) / jpeg_path.name)
            result.renamed.append(entry)

    for group in by_hash.values():
        for entry in group:
            if id(entry) not in seen:
                result.deleted.append(entry)

    return result
