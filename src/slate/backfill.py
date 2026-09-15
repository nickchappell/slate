from __future__ import annotations

import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from rich.prompt import Confirm

from slate import output
from slate.config import METADATA_BACKFILL_PROMPT
from slate.extraction import ExtractionError, build_montage, extract_frames
from slate.filenames import normalize_long_caption
from slate.inference import (
    MAX_CAPTION_TOKENS_WITH_METADATA,
    generate_caption,
    parse_caption_sections,
)
from slate.mappings import (
    APP_VERSION,
    MetadataChangeEntry,
    find_existing_metadata_change,
    load_metadata_changes,
    major_version_mismatch,
    read_app_version,
    save_metadata_changes,
)
from slate.metadata import embed_metadata, read_existing_metadata
from slate.pairing import build_groups, validate_media_files

# See "Backfill mode: embedding metadata into already-renamed files" in
# spec/metadata-embedding.md. Standalone mode, entirely separate from the
# rename flow -- almost none of the rename-specific machinery applies here
# (no filenames.assemble_stem/disambiguation, no rename.py execution, no
# review_sync.py JPEG-hash reconciliation, since nothing is being renamed
# and SHORT/Title isn't editable at all in this mode).


def _check_metadata_mappings_version_or_exit(mappings_path: Path) -> None:
    # Mirrors cli._check_mapping_version_or_exit's logic for
    # rename_mappings.json -- duplicated rather than imported to avoid a
    # circular import (cli.py imports this module to dispatch to it).
    file_version = read_app_version(mappings_path)
    if file_version is None:
        return
    if major_version_mismatch(file_version, APP_VERSION):
        output.fatal(
            f"{mappings_path} was written by slate v{file_version}, but "
            f"this is v{APP_VERSION} -- a major version apart. Its format "
            "may be incompatible with this version of slate."
        )
        output.fatal(
            "Re-run --metadata-backfill --dry-run to regenerate it, or "
            "verify compatibility by hand before proceeding."
        )
        sys.exit(1)


def run_backfill_generate(
    files: list[Path],
    base_dir: Path,
    mappings_path: Path,
    review_dir: Path,
    *,
    model: str,
    num_frames_for_caption: int,
) -> tuple[
    list[MetadataChangeEntry], list[MetadataChangeEntry], list[MetadataChangeEntry]
]:
    """Returns (all_entries, new_entries, skipped_entries). Mirrors
    cli.run_phase1's shape (scan, pair, caption, incremental re-run via
    current_files matching), but uses the 2-section LONG/KEYWORDS-only
    prompt (no SHORT -- nothing to spend decode tokens on since it's
    discarded), and title is populated from the file's current name rather
    than assembled/generated."""
    if files:
        output.console.print(
            f"\n[bold]Starting processing of {len(files)} file(s):[/bold]"
        )
        for path in files:
            output.console.print(f"  [cyan]{path}[/cyan]")

    files, rejected = validate_media_files(files)
    for path, reason in rejected:
        output.warn(f"skipping {path.name}: {reason}")
    groups = build_groups(files)
    _check_metadata_mappings_version_or_exit(mappings_path)
    existing = load_metadata_changes(mappings_path)

    all_entries: list[MetadataChangeEntry] = []
    new_entries: list[MetadataChangeEntry] = []
    skipped_entries: list[MetadataChangeEntry] = []

    review_dir.mkdir(parents=True, exist_ok=True)

    for group in groups:
        current_files = group.original_files
        match = find_existing_metadata_change(existing, current_files)
        if match is not None:
            output.skip(
                f"{' / '.join(current_files)}: skipping, already in "
                "metadata_changes.json"
            )
            skipped_entries.append(match)
            all_entries.append(match)
            continue

        if group.warning:
            output.warn(group.warning)

        if group.status == "error":
            entry = MetadataChangeEntry(
                status="error", current_files=current_files, error=group.error
            )
            new_entries.append(entry)
            all_entries.append(entry)
            output.error(f"{' / '.join(current_files)}: {group.error}")
            continue

        assert group.source_file is not None
        # No assemble_stem/disambiguation in this mode -- the file is
        # already named its final name, and (unlike Phase 1's preview
        # JPEGs) two groups can never collide on this name, since
        # group_by_stem already guarantees one group per distinct stem.
        title = group.source_file.stem
        preview_path = review_dir / f"{title}.jpg"

        with tempfile.TemporaryDirectory(prefix="slate-frames-") as raw_frames_dir:
            try:
                frame_paths = extract_frames(
                    group.source_file, Path(raw_frames_dir), num_frames_for_caption
                )
            except ExtractionError as e:
                entry = MetadataChangeEntry(
                    status="error", current_files=current_files, error=str(e)
                )
                new_entries.append(entry)
                all_entries.append(entry)
                output.error(f"{' / '.join(current_files)}: {e}")
                continue

            if len(current_files) > 1:
                output.processing(
                    f"Running image recognition on {' / '.join(current_files)} "
                    f"(frame from {group.source_file.name})..."
                )
            else:
                output.processing(f"Running image recognition on {current_files[0]}...")

            raw_text = generate_caption(
                [str(p) for p in frame_paths],
                METADATA_BACKFILL_PROMPT,
                model,
                max_tokens=MAX_CAPTION_TOKENS_WITH_METADATA,
            )
            build_montage(frame_paths, preview_path)

        sections = parse_caption_sections(raw_text)
        long_caption = normalize_long_caption(sections.long) if sections.long else None
        keywords = sections.keywords
        captioned_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        entry = MetadataChangeEntry(
            status="ok",
            current_files=current_files,
            title=title,
            long_caption=long_caption,
            keywords=keywords,
            preview_jpeg=preview_path.name,
            source_used_for_caption=group.source_file.name,
            captioned_at=captioned_at,
        )
        new_entries.append(entry)
        all_entries.append(entry)
        output.ok(f"{' / '.join(current_files)}: captioned")

    save_metadata_changes(mappings_path, all_entries)
    _print_backfill_generate_summary(all_entries, new_entries, skipped_entries)

    return all_entries, new_entries, skipped_entries


def _print_backfill_generate_summary(
    all_entries: list[MetadataChangeEntry],
    new_entries: list[MetadataChangeEntry],
    skipped_entries: list[MetadataChangeEntry],
) -> None:
    new_errors = sum(1 for e in new_entries if e.status == "error")
    carried_errors = sum(1 for e in skipped_entries if e.status == "error")
    total_errors = new_errors + carried_errors
    error_color = "bold red" if total_errors else "dim"

    output.console.print("\n[bold]Summary:[/bold]")
    output.console.print(f"  {len(all_entries)} groups total")
    output.console.print(f"  [green]{len(new_entries)}[/green] newly processed")
    output.console.print(
        f"  [cyan]{len(skipped_entries)}[/cyan] skipped "
        "(already in metadata_changes.json)"
    )
    output.console.print(
        f"  [{error_color}]{total_errors} error[/{error_color}] "
        f"({new_errors} new, {carried_errors} carried over from a previous run)"
    )


def run_backfill_apply(
    mappings_path: Path,
    base_dir: Path,
    *,
    assume_yes: bool,
    app_version: str,
    caption_model: str,
) -> None:
    """Re-checks every file still exists, re-derives title live from each
    file's *actual current name* (never trusts the stored title field --
    see "Title is a special case" in the design doc), confirms, then embeds
    directly via metadata.embed_metadata() per file (no rename.py
    involvement -- this entry point owns its own skip-and-warn loop).
    Skips files that already carry com.slate.* provenance (idempotency
    guard). Archives the mapping file on completion."""
    _check_metadata_mappings_version_or_exit(mappings_path)
    entries = load_metadata_changes(mappings_path)

    candidates: list[tuple[MetadataChangeEntry, list[Path]]] = []
    for entry in entries:
        if entry.status != "ok":
            continue
        paths = [base_dir / name for name in entry.current_files]
        missing = [p for p in paths if not p.is_file()]
        if missing:
            output.warn(
                f"WARNING: skipping metadata for "
                f"{' / '.join(entry.current_files)}: {missing[0].name} no "
                "longer exists on disk -- resolve manually and re-run."
            )
            continue
        candidates.append((entry, paths))

    if not candidates:
        output.info("Nothing to apply.")
        return

    output.console.print("[bold]Metadata preview:[/bold]")
    for _entry, paths in candidates:
        for path in paths:
            output.console.print(f"  [cyan]{path.name}[/cyan]")
    output.console.print()

    total_files = sum(len(paths) for _entry, paths in candidates)
    message = (
        f"{len(candidates)} groups, {total_files} files -- embed "
        "Title/Description/Keywords via exiftool?"
    )
    if not assume_yes and not Confirm.ask(message, default=False):
        output.warn("Aborted -- no metadata written.")
        return

    embedded = 0
    preserved = 0
    failed = 0
    already_processed = 0

    for entry, paths in candidates:
        for path in paths:
            existing = read_existing_metadata(path)
            if existing.has_slate_provenance:
                already_processed += 1
                output.skip(f"{path.name}: already has slate metadata, skipping")
                continue

            outcome = embed_metadata(
                path,
                title=path.stem,  # re-derived live, never trusted from storage
                description=entry.long_caption,
                keywords=entry.keywords,
                original_filename=path.name,
                app_version=app_version,
                caption_model=caption_model,
                generated_at=entry.captioned_at or "",
            )
            if outcome.embedded:
                embedded += 1
                if outcome.preserved_fields:
                    preserved += 1
                    for field_name in outcome.preserved_fields:
                        output.console.print(
                            f"  Pre-existing {field_name} preserved as "
                            f"com.slate.original-{field_name.lower()}"
                        )
                output.ok(f"{path.name}: metadata embedded")
            else:
                failed += 1
                output.warn(
                    f'WARNING: metadata embedding failed for "{path.name}" '
                    f"({outcome.error})."
                )

    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    top_level_dir = mappings_path.parent.parent
    applied_path = mappings_path.parent / f"applied_metadata_changes_{timestamp}.json"
    os.rename(mappings_path, applied_path)
    output.info(f"Audit trail written: {applied_path.relative_to(top_level_dir)}")

    summary = (
        f"\nMetadata: {embedded} embedded, {preserved} preserved "
        f"pre-existing field(s), {failed} failed"
    )
    if already_processed:
        summary += f", {already_processed} already processed (skipped)"
    output.console.print(summary)
