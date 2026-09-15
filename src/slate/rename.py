from __future__ import annotations

import os
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from slate.mappings import MappingEntry, sort_key
from slate.metadata import EmbedOutcome, embed_metadata

# See "Workflow Modes" Phase 2/3 and "Undo Script" in PROJECT_SPEC.md.


@dataclass
class RenameOperation:
    entry: MappingEntry
    old_paths: list[Path]
    new_paths: list[Path]


@dataclass
class RenamePlan:
    operations: list[RenameOperation]
    error_group_count: int  # "ok" groups skipped entirely, no new_stem to use
    whole_group_missing: list[str]  # report strings, batch-level
    partial_pair_missing: list[str]  # individual warning strings
    collisions: list[str]  # individual warning strings

    @property
    def problem_count(self) -> int:
        return (
            len(self.whole_group_missing)
            + len(self.partial_pair_missing)
            + len(self.collisions)
        )


def build_rename_plan(entries: list[MappingEntry], base_dir: Path) -> RenamePlan:
    operations: list[RenameOperation] = []
    whole_group_missing: list[str] = []
    partial_pair_missing: list[str] = []
    collisions: list[str] = []
    destinations_claimed: dict[Path, MappingEntry] = {}

    error_group_count = sum(1 for e in entries if e.status == "error")
    ok_entries = sorted((e for e in entries if e.status == "ok"), key=sort_key)

    for entry in ok_entries:
        old_paths = [base_dir / name for name in entry.original_files]
        existing = [p for p in old_paths if p.is_file()]

        if not existing:
            whole_group_missing.append(
                f"1 group skipped: no files found on disk for "
                f"{', '.join(entry.original_files)}"
            )
            continue

        if len(existing) != len(old_paths):
            missing = [p for p in old_paths if not p.is_file()]
            partial_pair_missing.append(
                f'WARNING: skipping rename for "{entry.new_stem}": paired file '
                f"{missing[0].name} no longer exists on disk (deleted since "
                "dry-run?) -- resolve manually and re-run."
            )
            continue

        new_paths = [base_dir / f"{entry.new_stem}{p.suffix}" for p in old_paths]

        conflict: Path | None = None
        for new_path in new_paths:
            if new_path in old_paths:
                continue  # renaming to itself is a no-op, not a conflict
            if new_path.is_file() or new_path in destinations_claimed:
                conflict = new_path
                break

        if conflict is not None:
            collisions.append(
                f'WARNING: skipping rename for "{entry.new_stem}": destination '
                f"{conflict.name} already exists (on disk or claimed by another "
                "group in this batch) -- resolve manually and re-run."
            )
            continue

        for new_path in new_paths:
            destinations_claimed[new_path] = entry
        operations.append(
            RenameOperation(entry=entry, old_paths=old_paths, new_paths=new_paths)
        )

    return RenamePlan(
        operations=operations,
        error_group_count=error_group_count,
        whole_group_missing=whole_group_missing,
        partial_pair_missing=partial_pair_missing,
        collisions=collisions,
    )


@dataclass
class RenameLogEntry:
    old_path: Path
    new_path: Path
    metadata_embedded: bool = False


def perform_renames(
    plan: RenamePlan,
    log: list[RenameLogEntry],
    on_rename=None,
    *,
    embed_metadata_flag: bool = False,
    app_version: str = "",
    caption_model: str = "",
    on_metadata: Callable[[Path, EmbedOutcome | None], None] | None = None,
) -> None:
    # `log` is mutated in place (not returned) so a mid-batch crash still
    # leaves the caller holding a record of everything that succeeded before
    # the exception -- see "Log renames incrementally," Phase 2 step 4.
    #
    # Metadata embedding (--add-metadata) runs per physical file, right
    # after that file's own rename, not once per group -- both the .MOV and
    # .MP4 in a pair get their own exiftool write. See "Where this runs in
    # the pipeline" in spec/metadata-embedding.md. on_rename always fires
    # before on_metadata for a given file, matching the design doc's CLI
    # output ordering (the rename line prints before any metadata line).
    for op in plan.operations:
        for old_path, new_path in zip(op.old_paths, op.new_paths, strict=True):
            os.rename(old_path, new_path)
            log_entry = RenameLogEntry(old_path=old_path, new_path=new_path)
            log.append(log_entry)
            if on_rename is not None:
                on_rename(log_entry)

            if not embed_metadata_flag:
                continue

            if op.entry.long_caption is None or op.entry.keywords is None:
                # Missing fields -- warn-and-skip is the caller's policy
                # (see "Error handling for exiftool write failures"); this
                # just signals "skipped" via the None outcome.
                log_entry.metadata_embedded = False
                if on_metadata is not None:
                    on_metadata(new_path, None)
                continue

            outcome = embed_metadata(
                new_path,
                title=op.entry.new_stem,
                description=op.entry.long_caption,
                keywords=op.entry.keywords,
                original_filename=old_path.name,
                app_version=app_version,
                caption_model=caption_model,
                # Per-entry, not a batch-wide timestamp: captioned_at
                # reflects when that specific group was actually captioned
                # in Phase 1, which can be arbitrarily earlier than this
                # rename/embed run across the review checkpoint.
                generated_at=op.entry.captioned_at or "",
            )
            log_entry.metadata_embedded = outcome.embedded
            if on_metadata is not None:
                on_metadata(new_path, outcome)


def write_audit_trail(mappings_path: Path, timestamp: str) -> Path:
    # rename_mappings.json already lives inside review/, alongside the
    # preview JPEGs it describes -- archiving it is just a rename in place,
    # not alongside the undo script, which stays at the top level for easy
    # discovery/running. The shared timestamp correlates the two.
    applied_path = mappings_path.parent / f"applied_renames_{timestamp}.json"
    os.rename(mappings_path, applied_path)
    return applied_path


def write_undo_script(log: list[RenameLogEntry], path: Path) -> None:
    # Only successfully-applied renames are ever in `log`, so a crashed
    # mid-batch run still gets a correct (partial) undo script.
    #
    # Title always mirrors new_stem (see spec/metadata-embedding.md's
    # "Title is a special case"), so undoing a rename without also
    # resetting Title would leave the file's embedded Title pointing at
    # its post-rename caption text even after the filename reverted --
    # see "Undo script: Title must revert too". Description/Keywords/
    # com.slate.* are deliberately NOT reset: they describe the clip's
    # content or its processing history, neither of which changes when a
    # filename is undone.
    lines = ["#!/usr/bin/env bash", "set -euo pipefail", ""]

    if any(entry.metadata_embedded for entry in log):
        # Only entries with metadata_embedded == True add an exiftool line
        # below, so a plain rename-only batch (or one where --add-metadata
        # failed for every file) gets a byte-identical, exiftool-free
        # script to today's -- this guard is the one new dependency the
        # generated script picks up, and only when it's actually needed.
        lines.append(
            "command -v exiftool >/dev/null || "
            '{ echo "exiftool required to undo embedded Title metadata -- '
            "install via 'brew install exiftool'\" >&2; exit 1; }"
        )
        lines.append("")

    for entry in log:
        old = shlex.quote(str(entry.old_path))
        new = shlex.quote(str(entry.new_path))
        lines.append(f"mv -n -- {new} {old}")
        if entry.metadata_embedded:
            # Ordering matters: if this exiftool call fails, the file has
            # already been correctly renamed back by this point -- a stale
            # Title is a strictly better partial-failure state than a
            # wrongly-named file.
            old_stem = shlex.quote(entry.old_path.stem)
            # ItemList/Keys/XMP-dc are the three tag families Title is
            # written to (see metadata.py) -- empirically verified group
            # prefixes against real exiftool. No -config needed here: Title
            # isn't one of the custom com.slate.* tags that requires it.
            lines.append(
                f"exiftool -overwrite_original -ItemList:Title={old_stem} "
                f"-Keys:Title={old_stem} -XMP-dc:Title={old_stem} -- {old}"
            )
    path.write_text("\n".join(lines) + "\n")
    os.chmod(path, 0o755)
