from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any

# See "Workflow Modes" (Phase 1 steps 2, 4, 7) in PROJECT_SPEC.md.

APP_VERSION = _pkg_version("slate")


@dataclass
class MappingEntry:
    status: str  # "ok" or "error"
    original_files: list[str]
    new_stem: str | None = None
    preview_jpeg: str | None = None
    preview_jpeg_sha256: str | None = None
    source_used_for_caption: str | None = None
    error: str | None = None
    # Populated only under --add-metadata (see spec/metadata-embedding.md).
    # short_caption is the JSON-editable alternative to renaming the
    # preview JPEG; long_caption/keywords have no filename equivalent.
    # captioned_at isn't in the original design doc's field list -- added
    # so com.slate.generated-at reflects the actual captioning run rather
    # than whenever --rename-only happens to execute across the review
    # checkpoint.
    short_caption: str | None = None
    long_caption: str | None = None
    keywords: list[str] | None = None
    captioned_at: str | None = None
    # Set True by review_sync.sync_from_review() the moment a human's JPEG
    # rename is first applied, and persisted from then on -- NOT just a
    # same-run flag. sync_from_review()/reconcile_short_caption_edits() can
    # run across separate invocations (sync-and-save happens before the
    # final confirmation prompt, so a declined/interrupted run still
    # persists the synced new_stem); without a durable marker, a later run
    # where the JPEG already matches has nothing to sync (correctly, a
    # no-op) but also no way to tell "this name came from a JPEG rename in
    # an earlier run" from "this name was never touched" -- and would
    # silently clobber the human's choice back to short_caption's text.
    # Once True, only another JPEG rename (never a short_caption edit)
    # moves the name again -- matches the pre-existing precedent that a
    # JPEG rename already wins over a direct new_stem edit too.
    short_caption_locked: bool = False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "status": self.status,
            "original_files": self.original_files,
        }
        if self.status == "ok":
            d["new_stem"] = self.new_stem
            d["preview_jpeg"] = self.preview_jpeg
            d["preview_jpeg_sha256"] = self.preview_jpeg_sha256
            d["source_used_for_caption"] = self.source_used_for_caption
            d["short_caption"] = self.short_caption
            d["long_caption"] = self.long_caption
            d["keywords"] = self.keywords
            d["captioned_at"] = self.captioned_at
            d["short_caption_locked"] = self.short_caption_locked
        else:
            d["error"] = self.error
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MappingEntry:
        return cls(
            status=d["status"],
            original_files=list(d["original_files"]),
            new_stem=d.get("new_stem"),
            preview_jpeg=d.get("preview_jpeg"),
            preview_jpeg_sha256=d.get("preview_jpeg_sha256"),
            source_used_for_caption=d.get("source_used_for_caption"),
            error=d.get("error"),
            short_caption=d.get("short_caption"),
            long_caption=d.get("long_caption"),
            keywords=d.get("keywords"),
            short_caption_locked=d.get("short_caption_locked", False),
            captioned_at=d.get("captioned_at"),
        )


def _load_raw(path: Path) -> dict[str, Any] | list[Any]:
    if not path.is_file():
        return {}
    with path.open() as f:
        return json.load(f)


def read_app_version(path: Path) -> str | None:
    """The app_version a mapping file was written with, or None if the file
    doesn't exist, predates this field, or was hand-created without one."""
    data = _load_raw(path)
    return data.get("app_version") if isinstance(data, dict) else None


def major_version_mismatch(file_version: str, running_version: str) -> bool:
    return file_version.split(".")[0] != running_version.split(".")[0]


def load_mappings(path: Path) -> list[MappingEntry]:
    data = _load_raw(path)
    # A bare list is the pre-app_version file format -- still readable.
    groups = data.get("groups", []) if isinstance(data, dict) else data
    return [MappingEntry.from_dict(d) for d in groups]


def save_mappings(path: Path, entries: list[MappingEntry]) -> None:
    data = {
        "app_version": APP_VERSION,
        "groups": [e.to_dict() for e in entries],
    }
    with path.open("w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def find_existing_match(
    existing: list[MappingEntry], original_files: list[str]
) -> MappingEntry | None:
    # Set-match (order-independent), regardless of the existing entry's
    # status -- see Phase 1 step 2's re-run/skip behavior.
    target = set(original_files)
    for entry in existing:
        if set(entry.original_files) == target:
            return entry
    return None


def sort_key(entry: MappingEntry) -> str:
    # Stable, deterministic order for disambiguation and Phase 3's caption
    # sample -- sorted by original filename.
    return min(entry.original_files)


def _final_names(entry: MappingEntry) -> list[str]:
    return [f"{entry.new_stem}{Path(name).suffix}" for name in entry.original_files]


def _fit_base_with_suffix(base: str, disambig_suffix: str, max_length: int) -> str:
    # "re-truncate the base portion (never the suffix)" -- Phase 1 step 4.
    limit = max_length - 1
    combined = base + disambig_suffix
    if len(combined) <= limit:
        return combined
    overflow = len(combined) - limit
    return base[: len(base) - overflow] + disambig_suffix


def disambiguate(
    entries: list[MappingEntry], max_file_name_length: int = 255
) -> list[MappingEntry]:
    """Mutates new_stem in place on colliding "ok" entries. `entries` should
    already include every "ok" group for this invocation (carried-over +
    newly-processed). Returns the list of entries that got a suffix, for the
    run-level summary."""
    ok_entries = sorted((e for e in entries if e.status == "ok"), key=sort_key)

    seen: set[str] = set()
    disambiguated: list[MappingEntry] = []

    for entry in ok_entries:
        final_names = _final_names(entry)
        if not any(name in seen for name in final_names):
            seen.update(final_names)
            continue

        base = entry.new_stem
        suffix_n = 2
        while True:
            disambig_suffix = f"_{suffix_n}"
            candidate_stem = _fit_base_with_suffix(
                base, disambig_suffix, max_file_name_length
            )
            candidate_names = [
                f"{candidate_stem}{Path(name).suffix}" for name in entry.original_files
            ]
            if not any(name in seen for name in candidate_names):
                break
            suffix_n += 1

        entry.new_stem = candidate_stem
        seen.update(candidate_names)
        disambiguated.append(entry)

    return disambiguated


@dataclass
class MetadataChangeEntry:
    # review/metadata_changes.json's group shape -- see "Backfill mode" in
    # spec/metadata-embedding.md. current_files (not original_files): these
    # are already-renamed files, there's no earlier "original" name tracked
    # here.
    status: str  # "ok" or "error"
    current_files: list[str]
    title: str | None = None  # informational only, re-derived live at apply
    long_caption: str | None = None
    keywords: list[str] | None = None
    preview_jpeg: str | None = None
    source_used_for_caption: str | None = None
    error: str | None = None
    # Not in the original design doc's literal field list -- added for the
    # same reason as MappingEntry.captioned_at: generate and apply can run
    # arbitrarily far apart, so com.slate.generated-at needs the actual
    # captioning-time timestamp, not whenever apply happens to run.
    captioned_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "status": self.status,
            "current_files": self.current_files,
        }
        if self.status == "ok":
            d["title"] = self.title
            d["long_caption"] = self.long_caption
            d["keywords"] = self.keywords
            d["preview_jpeg"] = self.preview_jpeg
            d["source_used_for_caption"] = self.source_used_for_caption
            d["captioned_at"] = self.captioned_at
        else:
            d["error"] = self.error
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MetadataChangeEntry:
        return cls(
            status=d["status"],
            current_files=list(d["current_files"]),
            title=d.get("title"),
            long_caption=d.get("long_caption"),
            keywords=d.get("keywords"),
            preview_jpeg=d.get("preview_jpeg"),
            source_used_for_caption=d.get("source_used_for_caption"),
            error=d.get("error"),
            captioned_at=d.get("captioned_at"),
        )


def load_metadata_changes(path: Path) -> list[MetadataChangeEntry]:
    data = _load_raw(path)
    groups = data.get("groups", []) if isinstance(data, dict) else data
    return [MetadataChangeEntry.from_dict(d) for d in groups]


def save_metadata_changes(path: Path, entries: list[MetadataChangeEntry]) -> None:
    data = {
        "app_version": APP_VERSION,
        "groups": [e.to_dict() for e in entries],
    }
    with path.open("w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def find_existing_metadata_change(
    existing: list[MetadataChangeEntry], current_files: list[str]
) -> MetadataChangeEntry | None:
    # Set-match, same rationale as find_existing_match -- lets a re-run of
    # --metadata-backfill --dry-run skip files already in the JSON.
    target = set(current_files)
    for entry in existing:
        if set(entry.current_files) == target:
            return entry
    return None
