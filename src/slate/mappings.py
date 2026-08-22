from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# See "Workflow Modes" (Phase 1 steps 2, 4, 7) in PROJECT_SPEC.md.


@dataclass
class MappingEntry:
    status: str  # "ok" or "error"
    original_files: list[str]
    new_stem: str | None = None
    preview_jpeg: str | None = None
    source_used_for_caption: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"status": self.status, "original_files": self.original_files}
        if self.status == "ok":
            d["new_stem"] = self.new_stem
            d["preview_jpeg"] = self.preview_jpeg
            d["source_used_for_caption"] = self.source_used_for_caption
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
            source_used_for_caption=d.get("source_used_for_caption"),
            error=d.get("error"),
        )


def load_mappings(path: Path) -> list[MappingEntry]:
    if not path.is_file():
        return []
    with path.open() as f:
        data = json.load(f)
    return [MappingEntry.from_dict(d) for d in data]


def save_mappings(path: Path, entries: list[MappingEntry]) -> None:
    with path.open("w") as f:
        json.dump([e.to_dict() for e in entries], f, indent=2)
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
    ok_entries = sorted(
        (e for e in entries if e.status == "ok"), key=sort_key
    )

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
