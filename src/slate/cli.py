from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from rich.markup import escape
from rich.prompt import Confirm

from slate import output
from slate.backfill import run_backfill_apply, run_backfill_generate
from slate.config import (
    DEFAULT_NUM_FRAMES_FOR_CAPTION,
    DEFAULT_PROMPT,
    METADATA_PROMPT,
    load_config,
)
from slate.extraction import ExtractionError, build_montage, extract_frames
from slate.filenames import (
    assemble_stem,
    normalize_caption,
    normalize_long_caption,
    truncate_caption,
)
from slate.inference import (
    MAX_CAPTION_TOKENS_WITH_METADATA,
    check_for_model_updates,
    derive_keywords_from_short,
    derive_short_from_keywords,
    derive_short_from_long,
    generate_caption,
    parse_caption_sections,
)
from slate.mappings import (
    APP_VERSION,
    MappingEntry,
    disambiguate,
    find_existing_match,
    load_mappings,
    major_version_mismatch,
    read_app_version,
    save_mappings,
)
from slate.metadata import EmbedOutcome
from slate.pairing import build_groups, discover_input_dir, validate_media_files
from slate.preflight import run_metadata_tool_checks, run_preflight_checks
from slate.rename import (
    RenameLogEntry,
    build_rename_plan,
    perform_renames,
    write_audit_trail,
    write_undo_script,
)
from slate.review_sync import hash_file, reconcile_short_caption_edits, sync_from_review


class UsageError(Exception):
    pass


_USAGE_EXAMPLES = [
    (
        "Phase 1: scan a directory, caption clips, write "
        "rename_mappings.json for review",
        "slate --input-dir ~/Movies/Footage --dry-run",
    ),
    (
        "Phase 2: after reviewing (rename a JPEG in review/ to correct its "
        "caption, delete one to skip that file), apply the renames",
        "slate --input-dir ~/Movies/Footage --rename-only \\\n"
        "      --rename-mappings=review/rename_mappings.json",
    ),
    (
        "Phase 3: caption and rename in one step, skipping the review phase",
        "slate --input-dir ~/Movies/Footage --process-and-rename",
    ),
    (
        "operate on an explicit file list instead of a whole directory",
        "slate --input-files clip1.MOV clip1.MP4 clip2.MP4 --dry-run",
    ),
    (
        "prepend the caption instead of appending it, eg. for a known "
        "geographical location prefix",
        "slate --input-dir ~/Movies/Footage --dry-run \\\n"
        '      --prepend-generated-name --prefix "Boston, MA"',
    ),
    (
        "override the configured/default model for one run",
        "slate --input-dir ~/Movies/Footage --dry-run \\\n"
        "      --model mlx-community/Qwen2.5-VL-7B-Instruct-4bit",
    ),
    (
        "check for a newer revision of the model weights",
        "slate --model-update-check",
    ),
    (
        "sample more frames per clip for a richer caption (default 3)",
        "slate --input-dir ~/Movies/Footage --dry-run \\\n"
        "      --num-frames-for-caption 5",
    ),
]


class _HelpAction(argparse.Action):
    """Prints argparse's normal help, then a colorized Usage examples block
    via `output.console` -- rich auto-detects TTY vs. redirected output, so
    this stays consistent with every other status line the app prints
    (colored/emoji in a real terminal, plain text when piped/redirected)."""

    def __init__(self, option_strings, dest=argparse.SUPPRESS, help=None):
        super().__init__(option_strings=option_strings, dest=dest, nargs=0, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        parser.print_help()
        output.console.print("\n[bold]Usage examples:[/bold]")
        for comment, command in _USAGE_EXAMPLES:
            output.console.print(f"  [dim]# {comment}[/dim]")
            lines = command.splitlines()
            output.console.print(f"  [green]$[/green] [cyan]{escape(lines[0])}[/cyan]")
            for line in lines[1:]:
                output.console.print(f"    [cyan]{escape(line)}[/cyan]")
            output.console.print()
        parser.exit()


class _VersionAction(argparse.Action):
    """Prints the running version via `output.console`, same as _HelpAction,
    instead of argparse's own plain-print version handling."""

    def __init__(self, option_strings, dest=argparse.SUPPRESS, help=None):
        super().__init__(option_strings=option_strings, dest=dest, nargs=0, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        output.console.print(f"slate {APP_VERSION}")
        parser.exit()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="slate",
        add_help=False,
        description=(
            "Caption and rename camera footage using a local vision-language\n"
            "model. Exactly one of --dry-run / --rename-only /\n"
            "--process-and-rename / --model-update-check / "
            "--metadata-backfill is required."
        ),
    )
    parser.add_argument(
        "-h",
        "--help",
        action=_HelpAction,
        help="show this help message and exit",
    )
    parser.add_argument(
        "--version",
        action=_VersionAction,
        help="show slate's version and exit",
    )

    # Not required=True: --metadata-backfill is a standalone flag (below,
    # outside this group) that combines freely with --dry-run but conflicts
    # with --rename-only/--process-and-rename -- not a simple pairwise
    # exclusion argparse's own group mechanism can express alone. The "at
    # least one mode selected" invariant this group's required=True used to
    # enforce moves to _validate_mode_flags(), called right after parsing.
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Phase 1: scan, pair MOV/MP4 files, and caption each clip. "
            "Writes review/rename_mappings.json plus captioned preview "
            "JPEGs, both under review/. Never renames or otherwise modifies "
            "source files. Safe to re-run on the same folder -- groups "
            "already in rename_mappings.json are skipped and carried over "
            "unchanged."
        ),
    )
    mode_group.add_argument(
        "--rename-only",
        action="store_true",
        help=(
            "Phase 2: apply a previously-reviewed (and optionally "
            "hand-edited) rename_mappings.json to disk. Requires "
            "--rename-mappings. Prompts for confirmation unless --yes/-y "
            "is passed; writes an audit trail JSON file and undo script afterward."
        ),
    )
    mode_group.add_argument(
        "--process-and-rename",
        action="store_true",
        help=(
            "Phase 3: run --dry-run and --rename-only back-to-back in one "
            "invocation, skipping the pause for hand-editing "
            "rename_mappings.json in between. Meant for footage you've already "
            "validated the prompt/model against -- not the default way "
            "to run a fresh camera dump. Its confirmation prompt shows a "
            "sample of the actual generated captions, since there's no "
            "review checkpoint."
        ),
    )
    mode_group.add_argument(
        "--model-update-check",
        action="store_true",
        help=(
            "Check the Hugging Face Hub for a newer revision of the "
            "configured/--model model and download it if one exists, "
            "then exit -- no footage is processed. Every other mode uses "
            "the local cache as-is with no network call once a model is "
            "downloaded; this is the only way to explicitly refresh it. "
            "Does not require --input-dir/--input-files."
        ),
    )

    input_group = parser.add_mutually_exclusive_group()
    input_group.add_argument(
        "--input-dir",
        type=Path,
        metavar="DIR",
        help=(
            "Directory to scan for camera footage (non-recursive). Mutually "
            "exclusive with --input-files; required for --dry-run/"
            "--process-and-rename unless --input-files is given."
        ),
    )
    input_group.add_argument(
        "--input-files",
        nargs="+",
        type=Path,
        metavar="FILE",
        help=(
            "Operate on exactly this list of files -- nothing else in "
            "their directory is discovered or touched, even a sibling "
            "MOV/MP4 of a file you did pass. All files must live in the "
            "same directory. Mutually exclusive with --input-dir."
        ),
    )

    parser.add_argument(
        "--rename-mappings",
        type=Path,
        metavar="PATH",
        help=(
            "Path to the rename_mappings.json to apply (written by --dry-run "
            "under review/). Required by --rename-only."
        ),
    )
    parser.add_argument(
        "--add-metadata",
        action="store_true",
        help=(
            "Also embed Title/Description/Keywords as real QuickTime/XMP "
            "metadata (not just the filename) via exiftool. Combinable with "
            "--dry-run/--rename-only/--process-and-rename; off by default. "
            "Must be passed again at --rename-only time even if the mapping "
            "file already has long_caption/keywords populated. Conflicts "
            "with --metadata-backfill."
        ),
    )
    parser.add_argument(
        "--metadata-backfill",
        action="store_true",
        help=(
            "Standalone mode: embed metadata into files already renamed by "
            "a past slate run, without renaming anything. Generate step: "
            "--metadata-backfill --dry-run --input-dir=... (or "
            "--input-files=...), writes review/metadata_changes.json. Apply "
            "step: --metadata-backfill --metadata-mappings=... (no "
            "--dry-run). Conflicts with --rename-only/--process-and-rename/"
            "--add-metadata."
        ),
    )
    parser.add_argument(
        "--metadata-mappings",
        type=Path,
        metavar="PATH",
        help=(
            "Path to the metadata_changes.json to apply (written by "
            "--metadata-backfill --dry-run). Required by --metadata-backfill's "
            "apply step (i.e. --metadata-backfill without --dry-run)."
        ),
    )
    parser.add_argument(
        "--model",
        metavar="REPO_ID",
        help=(
            "Hugging Face repo ID for the vision-language model used to "
            "caption frames (any repo mlx-vlm/huggingface_hub can resolve). "
            "Overrides the config file's model key and the built-in "
            "default for this run only."
        ),
    )
    parser.add_argument(
        "--num-frames-for-caption",
        type=int,
        metavar="N",
        help=(
            "Number of frames sampled per clip and fed to the model "
            "together for one caption (spread across the clip -- from 5%% "
            "of the way through, evenly through the middle, up to 90%% of "
            "the way through, avoiding the very start/end). Overrides the "
            "config file's num_frames_for_caption key and the built-in default "
            f"({DEFAULT_NUM_FRAMES_FOR_CAPTION}) for this run only. Must "
            "be >= 1."
        ),
    )

    caption_position_group = parser.add_mutually_exclusive_group()
    caption_position_group.add_argument(
        "--prepend-generated-name",
        action="store_true",
        help=(
            "Put the caption before the original filename: '<caption> <original_stem>'."
        ),
    )
    caption_position_group.add_argument(
        "--append-generated-name",
        action="store_true",
        help=(
            "Put the caption after the original filename: "
            "'<original_stem> <caption>'. This is the default behavior "
            "when neither flag is passed."
        ),
    )

    parser.add_argument(
        "--prefix",
        default=None,
        metavar="TEXT",
        help=(
            "Text prepended to the entire assembled filename, e.g. a shoot's location."
        ),
    )
    parser.add_argument(
        "--suffix",
        default=None,
        metavar="TEXT",
        help="Text appended to the entire assembled filename.",
    )
    parser.add_argument(
        "--skip-generate-undo-script",
        action="store_true",
        help=(
            "Don't write an undo_renames_<timestamp>.sh reversal script "
            "after a rename batch. Undo scripts are written by default."
        ),
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help=(
            "Skip the confirmation prompt before renaming in "
            "--rename-only/--process-and-rename."
        ),
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help=(
            "With --add-metadata, also print each group's short caption, "
            "long caption, and keywords, plus the exact Title/Description/"
            "Keywords tag values about to be (or already) written."
        ),
    )

    return parser


def _validate_mode_flags(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> None:
    """Not a simple pairwise conflict argparse's mutually_exclusive_group
    can express alone (--dry-run must combine freely with either axis,
    while --metadata-backfill conflicts with three specific other flags) --
    see the "Implementation note" in spec/metadata-embedding.md."""
    if args.metadata_backfill:
        if args.rename_only or args.process_and_rename:
            parser.error(
                "--metadata-backfill cannot be combined with --rename-only "
                "or --process-and-rename -- backfill mode never renames "
                "anything."
            )
        if args.add_metadata:
            parser.error(
                "--metadata-backfill cannot be combined with --add-metadata "
                "-- backfill mode's metadata writing isn't optional/"
                "toggleable the way --add-metadata is."
            )
        if args.model_update_check:
            parser.error(
                "--metadata-backfill cannot be combined with --model-update-check."
            )
        if not args.dry_run and (args.input_dir or args.input_files):
            parser.error(
                "--metadata-backfill's apply step (no --dry-run) doesn't "
                "take --input-dir/--input-files -- pass --metadata-mappings "
                "instead."
            )
        return

    if not (
        args.dry_run
        or args.rename_only
        or args.process_and_rename
        or args.model_update_check
    ):
        parser.error(
            "one of the arguments --dry-run --rename-only "
            "--process-and-rename --model-update-check --metadata-backfill "
            "is required"
        )


def _resolve_input_files(args: argparse.Namespace) -> tuple[list[Path], Path]:
    if args.input_dir:
        input_dir = args.input_dir
        if not input_dir.is_dir():
            raise UsageError(f"--input-dir {input_dir} is not a directory")
        return discover_input_dir(input_dir), input_dir

    if args.input_files:
        missing = [f for f in args.input_files if not f.is_file()]
        if missing:
            raise UsageError(
                "--input-files: file(s) not found: "
                + ", ".join(str(m) for m in missing)
            )
        parents = {f.resolve().parent for f in args.input_files}
        if len(parents) != 1:
            raise UsageError("--input-files: all files must live in the same directory")
        return list(args.input_files), args.input_files[0].parent

    raise UsageError("exactly one of --input-dir or --input-files is required")


def _effective_settings(args: argparse.Namespace):
    config = load_config()

    model = args.model or config.model

    if args.prepend_generated_name:
        prepend = True
    elif args.append_generated_name:
        prepend = False
    else:
        prepend = config.prepend_generated_name

    prefix = args.prefix if args.prefix is not None else config.prefix
    suffix = args.suffix if args.suffix is not None else config.suffix

    num_frames_for_caption = (
        args.num_frames_for_caption
        if args.num_frames_for_caption is not None
        else config.num_frames_for_caption
    )

    generate_undo = (
        False if args.skip_generate_undo_script else config.generate_undo_script
    )

    add_metadata = True if args.add_metadata else config.add_metadata

    return (
        config,
        model,
        prepend,
        prefix,
        suffix,
        num_frames_for_caption,
        generate_undo,
        add_metadata,
    )


def _run_preflight_or_exit(*, require_metadata_tools: bool) -> None:
    failures = list(run_preflight_checks())
    # Bento4 is only required when --add-metadata/--metadata-backfill are
    # in play -- see run_metadata_tool_checks()'s docstring for why this
    # can't just live in run_preflight_checks()'s unconditional list.
    if require_metadata_tools:
        failures += run_metadata_tool_checks()
    if failures:
        output.fatal("slate cannot run in this environment:")
        for message in failures:
            output.fatal(f"  - {message}")
        sys.exit(1)


def _check_mapping_version_or_exit(mappings_path: Path) -> None:
    # A missing app_version (no file yet, or a file predating this field) is
    # treated as compatible -- only a *known* major-version difference is
    # grounds to refuse, since the mapping file format is what's actually at
    # risk of having changed underneath it.
    file_version = read_app_version(mappings_path)
    if file_version is None:
        return
    if major_version_mismatch(file_version, APP_VERSION):
        output.fatal(
            f"{mappings_path} was written by slate v{file_version}, but this "
            f"is v{APP_VERSION} -- a major version apart. Its format may be "
            "incompatible with this version of slate."
        )
        output.fatal(
            "Re-run --dry-run to regenerate it, or verify compatibility "
            "by hand before proceeding."
        )
        sys.exit(1)


# --- --add-metadata --verbose output --------------------------------------

# Printed once per run (not per group -- the mapping itself never varies,
# only the values below it do). Tag names/groups verified empirically
# against real exiftool -- see the header comment in metadata.py.
_METADATA_TAG_LEGEND = [
    ("Title", "ItemList:Title / Keys:Title (com.apple.quicktime.title) / XMP-dc:Title"),
    (
        "Description",
        "ItemList:Description / Keys:Description (com.apple.quicktime.description) "
        "/ XMP-dc:Description",
    ),
    (
        "Keywords",
        "ItemList:Keyword / Keys:Keywords (com.apple.quicktime.keywords) / "
        "XMP-dc:Subject",
    ),
]


def _print_metadata_tag_legend() -> None:
    output.console.print("[dim]Metadata tag mapping (--add-metadata):[/dim]")
    for label, tags in _METADATA_TAG_LEGEND:
        output.console.print(f"[dim]  {label:<11} -> {tags}[/dim]")


def _print_metadata_changes(entry: MappingEntry) -> None:
    output.console.print("  [bold cyan]Metadata changes:[/bold cyan]")
    output.console.print(f"    [dim]Short caption: {entry.short_caption}[/dim]")
    output.console.print(f"    [dim]Title:         {entry.new_stem}[/dim]")
    output.console.print(f"    [dim]Description:   {entry.long_caption}[/dim]")
    output.console.print(
        f"    [dim]Keywords:      {', '.join(entry.keywords or [])}[/dim]"
    )


# --- Phase 1: --dry-run --------------------------------------------------


def run_phase1(
    files: list[Path],
    base_dir: Path,
    mappings_path: Path,
    review_dir: Path,
    *,
    model: str,
    prompt: str,
    prepend: bool,
    prefix: str,
    suffix: str,
    max_file_name_length: int,
    num_frames_for_caption: int,
    add_metadata: bool = False,
    verbose: bool = False,
) -> tuple[list[MappingEntry], list[MappingEntry], list[MappingEntry]]:
    """Returns (all_entries, new_entries, skipped_entries)."""
    if add_metadata and prompt != DEFAULT_PROMPT:
        output.info(
            "your configured prompt is not used with --add-metadata; using "
            "the fixed SHORT/LONG/KEYWORDS format instead."
        )

    if add_metadata and verbose:
        _print_metadata_tag_legend()

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
    _check_mapping_version_or_exit(mappings_path)
    existing = load_mappings(mappings_path)

    all_entries: list[MappingEntry] = []
    new_entries: list[MappingEntry] = []
    skipped_entries: list[MappingEntry] = []
    pending_previews: dict[int, Path] = {}  # id(entry) -> its still-tmp-named frame

    review_dir.mkdir(parents=True, exist_ok=True)

    for group in groups:
        original_files = group.original_files
        match = find_existing_match(existing, original_files)
        if match is not None:
            output.skip(
                f"{' / '.join(original_files)}: skipping, already in "
                "rename_mappings.json"
            )
            skipped_entries.append(match)
            all_entries.append(match)
            continue

        if group.warning:
            output.warn(group.warning)

        if group.status == "error":
            entry = MappingEntry(
                status="error", original_files=original_files, error=group.error
            )
            new_entries.append(entry)
            all_entries.append(entry)
            output.error(f"{' / '.join(original_files)}: {group.error}")
            continue

        assert group.source_file is not None
        tmp_frame_path = review_dir / f".tmp.{group.source_file.stem}.jpg"
        with tempfile.TemporaryDirectory(prefix="slate-frames-") as raw_frames_dir:
            try:
                frame_paths = extract_frames(
                    group.source_file, Path(raw_frames_dir), num_frames_for_caption
                )
            except ExtractionError as e:
                entry = MappingEntry(
                    status="error", original_files=original_files, error=str(e)
                )
                new_entries.append(entry)
                all_entries.append(entry)
                output.error(f"{' / '.join(original_files)}: {e}")
                continue

            if len(original_files) > 1:
                output.processing(
                    f"Running image recognition on {' / '.join(original_files)} "
                    f"(frame from {group.source_file.name})..."
                )
            else:
                output.processing(
                    f"Running image recognition on {original_files[0]}..."
                )

            if add_metadata:
                raw_text = generate_caption(
                    [str(p) for p in frame_paths],
                    METADATA_PROMPT,
                    model,
                    max_tokens=MAX_CAPTION_TOKENS_WITH_METADATA,
                )
                sections = parse_caption_sections(raw_text)
                # SHORT missing (or just an echoed <placeholder>) --
                # derive a short caption from LONG, then KEYWORDS, rather
                # than falling back to the full raw multi-section
                # response, which would otherwise leak "long: ...
                # keywords: ..." text (and echoed prompt instructions like
                # "3-6 words") straight into the filename. Only truly
                # unstructured output (nothing parsed at all) falls back
                # to raw_text itself.
                if sections.short:
                    short_text = sections.short
                elif sections.long:
                    short_text = derive_short_from_long(sections.long)
                elif sections.keywords:
                    short_text = derive_short_from_keywords(sections.keywords)
                else:
                    short_text = raw_text
                long_caption = (
                    normalize_long_caption(sections.long) if sections.long else None
                )
                keywords = sections.keywords
                captioned_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                short_text = generate_caption(
                    [str(p) for p in frame_paths], prompt, model
                )
                long_caption = None
                keywords = None
                captioned_at = None

            build_montage(frame_paths, tmp_frame_path)
        caption = truncate_caption(normalize_caption(short_text))

        # Guard against a partially-parsed response leaving some
        # *persisted* fields None while others hold real content --
        # rename.py/review_sync.py treat "long_caption is None or
        # keywords is None" as "never captioned" and skip --add-metadata
        # embedding entirely for that file, even though real captioning
        # data exists (reported real-world case: SHORT + KEYWORDS parsed
        # fine, only LONG was an echoed <placeholder>, so a real caption
        # never got embedded on rename). Backfill whichever of
        # long_caption/keywords is still missing from `caption` (the
        # final short caption -- same value stored as short_caption
        # below) rather than let one missing section blank out the whole
        # entry. Skipped when `caption` itself is empty (captioning
        # produced nothing usable at all) so that case still skips
        # embedding cleanly instead of writing blank metadata.
        if add_metadata and caption:
            if long_caption is None:
                long_caption = caption
            if keywords is None:
                keywords = derive_keywords_from_short(caption)

        new_stem = assemble_stem(
            original_stem=group.source_file.stem,
            caption=caption,
            prefix=prefix,
            suffix=suffix,
            prepend_caption=prepend,
            max_length=max_file_name_length,
        )

        # Note: not yet renamed to its new_stem-based name -- two clips
        # processed in *this same run* can coincidentally assemble to the
        # identical name (e.g. stem "a" + caption "b c" and stem "a b" +
        # caption "c" both assemble to "a b c"), and disambiguate() hasn't
        # run yet to tell them apart. Renaming straight to `preview_name`
        # here would let the second one silently overwrite the first's
        # preview file on disk. Stay at the (per-group, always-unique)
        # tmp name until every entry's final new_stem is settled, below.
        preview_sha256 = hash_file(tmp_frame_path)

        entry = MappingEntry(
            status="ok",
            original_files=original_files,
            new_stem=new_stem,
            preview_jpeg=None,
            preview_jpeg_sha256=preview_sha256,
            source_used_for_caption=group.source_file.name,
            short_caption=caption if add_metadata else None,
            long_caption=long_caption,
            keywords=keywords,
            captioned_at=captioned_at,
        )
        pending_previews[id(entry)] = tmp_frame_path
        new_entries.append(entry)
        all_entries.append(entry)
        output.ok(f"{' / '.join(original_files)} -> {new_stem}")
        if add_metadata and verbose:
            _print_metadata_changes(entry)

    disambiguated = disambiguate(all_entries)

    # Relocate any carried-over entry a fresh collision just gave a suffix
    # to *before* newly-processed entries claim their final names below --
    # otherwise a new entry could write straight into an old entry's
    # not-yet-vacated on-disk spot.
    for entry in disambiguated:
        if id(entry) in pending_previews or entry.preview_jpeg is None:
            continue
        old_preview_path = review_dir / entry.preview_jpeg
        new_preview_name = f"{entry.new_stem}.jpg"
        new_preview_path = review_dir / new_preview_name
        if old_preview_path.is_file():
            old_preview_path.rename(new_preview_path)
        entry.preview_jpeg = new_preview_name

    # Now that every entry's new_stem is final (disambiguated, if it needed
    # to be) and colliding old entries are out of the way, move each newly
    # processed preview from its tmp name to its real one.
    for entry in new_entries:
        if entry.status != "ok":
            continue
        tmp_frame_path = pending_previews[id(entry)]
        preview_name = f"{entry.new_stem}.jpg"
        preview_path = review_dir / preview_name
        tmp_frame_path.rename(preview_path)
        entry.preview_jpeg = preview_name

    save_mappings(mappings_path, all_entries)
    _print_phase1_summary(all_entries, new_entries, skipped_entries, disambiguated)

    return all_entries, new_entries, skipped_entries


def _print_phase1_summary(
    all_entries: list[MappingEntry],
    new_entries: list[MappingEntry],
    skipped_entries: list[MappingEntry],
    disambiguated: list[MappingEntry],
) -> None:
    new_errors = sum(1 for e in new_entries if e.status == "error")
    carried_errors = sum(1 for e in skipped_entries if e.status == "error")
    total_errors = new_errors + carried_errors

    error_color = "bold red" if total_errors else "dim"
    disambig_color = "yellow" if disambiguated else "dim"

    output.console.print("\n[bold]Summary:[/bold]")
    output.console.print(f"  {len(all_entries)} groups total")
    output.console.print(f"  [green]{len(new_entries)}[/green] newly processed")
    output.console.print(
        f"  [cyan]{len(skipped_entries)}[/cyan] skipped "
        "(already in rename_mappings.json)"
    )
    output.console.print(
        f"  [{disambig_color}]{len(disambiguated)}[/{disambig_color}] disambiguated "
        "(suffix appended to avoid a name collision)"
    )
    output.console.print(
        f"  [{error_color}]{total_errors} error[/{error_color}] "
        f"({new_errors} new, {carried_errors} carried over from a previous run)"
    )


def _print_phase1_next_steps(
    review_dir: Path,
    mappings_path: Path,
    new_entries: list[MappingEntry],
    *,
    add_metadata: bool,
) -> None:
    output.console.print("\n[bold]Next steps:[/bold]")
    output.console.print(
        f"  1. Review the captions: rename a JPEG in "
        f"[cyan]{review_dir}/[/cyan] to correct it, or delete one to skip "
        f"that file. (You can also hand-edit the [magenta]new_stem[/magenta] "
        f"value for each file directly in [cyan]{mappings_path}[/cyan].)"
    )
    output.console.print(
        "  2. Apply the renames: "
        f"[cyan]slate --rename-only --rename-mappings={mappings_path}[/cyan]"
    )
    if add_metadata:
        metadata_count = sum(
            1 for e in new_entries if e.status == "ok" and e.long_caption is not None
        )
        output.console.print(
            f"     - Description + Keywords generated for "
            f"{metadata_count} group(s), see long_caption/keywords in "
            f"[cyan]{mappings_path}[/cyan] for review. To also embed "
            "them as QuickTime/XMP Title/Description/Keywords, add "
            "[cyan]--add-metadata[/cyan] to the command above."
        )


# --- Phase 2: --rename-only / Phase 3: --process-and-rename --------------


def run_phase2(
    entries: list[MappingEntry],
    base_dir: Path,
    mappings_path: Path,
    *,
    generate_undo_script: bool,
    assume_yes: bool,
    phase3_newly_processed_ok: list[MappingEntry] | None = None,
    add_metadata: bool = False,
    app_version: str = APP_VERSION,
    caption_model: str = "",
    prefix: str = "",
    suffix: str = "",
    prepend: bool = False,
    max_file_name_length: int = 255,
    verbose: bool = False,
) -> None:
    if not add_metadata:
        # Silent non-write footgun (flag-safety review point 1): easy to
        # generate long_caption/keywords via --dry-run --add-metadata, then
        # later run --rename-only without the flag by mistake. Rename still
        # proceeds -- only metadata is skipped -- but this must not be a
        # silent no-op.
        metadata_entries = [
            e
            for e in entries
            if e.status == "ok" and e.long_caption is not None and e.keywords
        ]
        if metadata_entries:
            output.warn(
                "WARNING: rename_mappings.json contains generated "
                f"Description/Keywords for {len(metadata_entries)} group(s), "
                "but --add-metadata was not passed -- proceeding with "
                "rename only; metadata will NOT be written this run."
            )

    review_dir = mappings_path.parent  # rename_mappings.json lives inside review/
    sync_result = sync_from_review(entries, review_dir)

    if sync_result.renamed:
        save_mappings(mappings_path, entries)
        for entry in sync_result.renamed:
            output.ok(
                f"Synced from review/: {' / '.join(entry.original_files)} -> "
                f"{entry.new_stem}"
            )
    for digest in sync_result.ambiguous_hashes:
        output.warn(
            f"WARNING: multiple groups share an identical preview JPEG "
            f"(hash {digest[:12]}...) -- skipping review/ sync for those "
            "groups; resolve manually in rename_mappings.json."
        )
    for entry in sync_result.deleted:
        output.warn(
            f'WARNING: skipping rename for "{entry.new_stem}": preview JPEG '
            "no longer found in review/ (deleted?) -- restore it, or edit "
            "rename_mappings.json directly, then re-run."
        )

    if add_metadata:
        short_caption_changed = reconcile_short_caption_edits(
            entries,
            prefix=prefix,
            suffix=suffix,
            prepend=prepend,
            max_file_name_length=max_file_name_length,
        )
        if short_caption_changed:
            save_mappings(mappings_path, entries)
            for entry in short_caption_changed:
                output.ok(
                    "Applied short_caption edit: "
                    f"{' / '.join(entry.original_files)} -> {entry.new_stem}"
                )

    deleted_ids = {id(e) for e in sync_result.deleted}
    plan_entries = [e for e in entries if id(e) not in deleted_ids]

    plan = build_rename_plan(plan_entries, base_dir)

    if plan.error_group_count:
        output.warn(
            f"{plan.error_group_count} group(s) skipped due to earlier "
            "extraction errors"
        )
    for message in plan.whole_group_missing:
        output.warn(message)
    for message in plan.partial_pair_missing:
        output.warn(message)
    for message in plan.collisions:
        output.warn(message)

    if not plan.operations:
        output.info("Nothing to rename.")
        return

    if not assume_yes:
        if phase3_newly_processed_ok is not None:
            confirmed = _prompt_phase3(
                plan, phase3_newly_processed_ok, add_metadata=add_metadata
            )
        else:
            confirmed = _prompt_phase2(plan, add_metadata=add_metadata)
        if not confirmed:
            output.warn("Aborted -- no files renamed.")
            return

    if add_metadata and verbose and phase3_newly_processed_ok is None:
        # In --process-and-rename, run_phase1() above already printed this
        # legend once -- don't repeat it a second time within one invocation.
        _print_metadata_tag_legend()

    # Keyed by new_stem (== path.stem post-rename) so on_metadata below can
    # look up the group's caption/keywords from just the path it's given --
    # both files of a MOV/MP4 pair share one entry and one new_stem.
    entries_by_new_stem = {e.new_stem: e for e in plan_entries if e.status == "ok"}

    log: list[RenameLogEntry] = []
    metadata_stats = {"embedded": 0, "preserved": 0, "failed": 0}

    def on_metadata(path: Path, outcome: EmbedOutcome | None) -> None:
        if outcome is None:
            output.warn(
                f'WARNING: skipping metadata for "{path.stem}": '
                "long_caption/keywords missing from rename_mappings.json "
                "(generated without --add-metadata?) -- re-run --dry-run "
                "--add-metadata first, or use --metadata-backfill "
                "afterward."
            )
            return
        if verbose:
            entry = entries_by_new_stem.get(path.stem)
            if entry is not None:
                _print_metadata_changes(entry)
        if outcome.embedded:
            metadata_stats["embedded"] += 1
            if outcome.preserved_fields:
                metadata_stats["preserved"] += 1
                for field_name in outcome.preserved_fields:
                    output.console.print(
                        f"  Pre-existing {field_name} preserved as "
                        f"com.slate.original-{field_name.lower()}"
                    )
            output.console.print("  Metadata embedded (Title, Description, Keywords)")
        else:
            metadata_stats["failed"] += 1
            output.warn(
                f'WARNING: metadata embedding failed for "{path.name}" '
                f"({outcome.error}) -- filename was still renamed; re-run "
                "--metadata-backfill on this file to retry."
            )

    try:
        perform_renames(
            plan,
            log,
            on_rename=lambda e: output.renamed(
                f"{e.old_path.name} -> {e.new_path.name}"
            ),
            embed_metadata_flag=add_metadata,
            app_version=app_version,
            caption_model=caption_model,
            on_metadata=on_metadata if add_metadata else None,
        )
    finally:
        timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        if mappings_path.is_file():
            # rename_mappings.json lives inside review/, so the undo script
            # (meant to sit at the top level for easy discovery/running,
            # unlike the audit trail which stays alongside the preview
            # JPEGs it archives) belongs one level above that.
            top_level_dir = mappings_path.parent.parent
            applied_path = write_audit_trail(mappings_path, timestamp)
            output.info(
                f"Audit trail written: {applied_path.relative_to(top_level_dir)}"
            )
            if generate_undo_script and log:
                undo_path = top_level_dir / f"undo_renames_{timestamp}.sh"
                write_undo_script(log, undo_path)
                output.info(f"Undo script written: {undo_path.name}")
                output.console.print(f"  Run with: [cyan]./{undo_path.name}[/cyan]")
        if add_metadata:
            output.console.print(
                f"\nMetadata: {metadata_stats['embedded']} embedded, "
                f"{metadata_stats['preserved']} preserved pre-existing "
                f"field(s), {metadata_stats['failed']} failed"
            )


def _print_rename_preview(plan) -> None:
    output.console.print("[bold]Rename preview:[/bold]")
    for op in plan.operations:
        for old_path, new_path in zip(op.old_paths, op.new_paths, strict=True):
            output.console.print(
                f"  [cyan]{old_path.name}[/cyan] --> [green]{new_path.name}[/green]"
            )
    output.console.print()


def _prompt_phase2(plan, add_metadata: bool = False) -> bool:
    _print_rename_preview(plan)
    message = f"{len(plan.operations)} rename operations"
    if add_metadata:
        message += ", will also embed Title/Description/Keywords via exiftool"
    if plan.problem_count:
        message += f", [yellow]{plan.problem_count} issue(s)[/yellow] reported above"
    return Confirm.ask(message, default=False)


def _prompt_phase3(
    plan, newly_processed_ok: list[MappingEntry], add_metadata: bool = False
) -> bool:
    newly_processed_ids = {id(e) for e in newly_processed_ok}
    newly_captioned_in_plan = sum(
        1 for op in plan.operations if id(op.entry) in newly_processed_ids
    )
    carried_over_in_plan = len(plan.operations) - newly_captioned_in_plan

    output.warn(
        "Phase 3 (--process-and-rename): no review checkpoint -- captions "
        "below have not been manually reviewed."
    )
    metadata_note = (
        " --add-metadata: Description/Keywords will also be embedded."
        if add_metadata
        else ""
    )
    output.console.print(
        f"\n{len(plan.operations)} rename operations pending "
        f"([green]{newly_captioned_in_plan} newly captioned[/green], "
        f"[cyan]{carried_over_in_plan} carried over[/cyan] from a previous "
        f"run).{metadata_note}\n"
    )

    _print_rename_preview(plan)

    question = (
        f"Continue with {len(plan.operations)} renames and metadata writes?"
        if add_metadata
        else f"Continue with {len(plan.operations)} renames?"
    )
    return Confirm.ask(question, default=False)


# --- Entry point -----------------------------------------------------------


def _mode_description(
    args: argparse.Namespace, *, add_metadata: bool
) -> tuple[str, list[str]]:
    """Returns (short mode name, a list of one or more bullet points
    describing what it does). A second bullet is appended for the three
    main phases when --add-metadata is in effect (CLI flag or config.toml)
    -- --metadata-backfill's own bullets already describe metadata
    end-to-end, so it never needs a second one."""
    if args.metadata_backfill:
        if args.dry_run:
            return (
                "Metadata backfill mode (generate)",
                [
                    "Captions (Description + Keywords only) are "
                    "regenerated for already-renamed files and written, "
                    "alongside preview JPEGs, to the review/ folder. "
                    "Nothing is embedded yet -- review "
                    "review/metadata_changes.json, then apply it without "
                    "--dry-run."
                ],
            )
        return (
            "Metadata backfill mode (apply)",
            [
                "A previously reviewed metadata_changes.json is applied: "
                "Title/Description/Keywords are embedded via exiftool "
                "into each file, with Title re-derived live from its "
                "current filename. No renaming happens in this mode."
            ],
        )

    if args.dry_run:
        bullets = [
            "Captions are generated for each clip and written, alongside "
            "preview JPEGs, to the review/ folder. Nothing is renamed -- "
            "review the captions there, then apply them with --rename-only."
        ]
        if add_metadata:
            bullets.append(
                "Title, Description, and Keywords are also generated "
                "from the caption data -- reviewed here, then written as "
                "QuickTime/XMP metadata once you apply with --rename-only."
            )
        return "Dry-run mode", bullets

    if args.rename_only:
        bullets = [
            "A previously reviewed rename_mappings.json is applied to "
            "disk: files are re-checked, renamed, and an audit trail plus "
            "an undo script are written. No captioning happens in this "
            "phase."
        ]
        if add_metadata:
            bullets.append(
                "Title, Description, and Keywords generated from the "
                "caption data are also embedded as QuickTime/XMP metadata "
                "into each renamed file via exiftool."
            )
        return "Rename mode", bullets

    if args.process_and_rename:
        bullets = [
            "Captioning and renaming run back-to-back in one pass, with "
            "no review checkpoint -- the confirmation prompt shows a "
            "sample of the generated captions before anything is renamed."
        ]
        if add_metadata:
            bullets.append(
                "Title, Description, and Keywords are also generated "
                "from the caption data and embedded as QuickTime/XMP "
                "metadata into each file via exiftool in this same pass."
            )
        return "Process-and-rename mode", bullets

    if args.model_update_check:
        return (
            "Model-update-check mode",
            [
                "The Hugging Face Hub is checked for a newer revision of "
                "the configured model, which is downloaded if found. No "
                "footage is processed."
            ],
        )
    return "", []


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate_mode_flags(args, parser)

    # Resolved ahead of _effective_settings() below, specifically so the
    # startup banner's metadata bullet reflects add_metadata even when it's
    # only set via config.toml, not the --add-metadata CLI flag. Reading
    # config.toml twice (again inside _effective_settings()) is cheap and
    # side-effect-free.
    banner_add_metadata = args.add_metadata or load_config().add_metadata
    name, description_bullets = _mode_description(
        args, add_metadata=banner_add_metadata
    )
    if name:
        output.console.print(f"[bold]slate[/bold] started in {name}...")
        output.console.print()
        output.console.print(f"[bold]{name}:[/bold]")
        output.console.print()
        for bullet in description_bullets:
            output.console.print(f"[dim]- {bullet}[/dim]")
    else:
        output.console.print("[bold]slate[/bold] started")

    try:
        if args.dry_run or args.process_and_rename:
            if not args.input_dir and not args.input_files:
                raise UsageError(
                    "--dry-run/--process-and-rename requires --input-dir or "
                    "--input-files"
                )
        if args.rename_only and not args.rename_mappings:
            raise UsageError("--rename-only requires --rename-mappings")
        if args.metadata_backfill and not args.dry_run and not args.metadata_mappings:
            raise UsageError(
                "--metadata-backfill's apply step (no --dry-run) requires "
                "--metadata-mappings"
            )

        _run_preflight_or_exit(
            require_metadata_tools=banner_add_metadata or args.metadata_backfill
        )

        (
            config,
            model,
            prepend,
            prefix,
            suffix,
            num_frames_for_caption,
            generate_undo,
            add_metadata,
        ) = _effective_settings(args)
        if num_frames_for_caption < 1:
            raise UsageError("--num-frames-for-caption must be >= 1")

        if args.metadata_backfill:
            if args.dry_run:
                files, base_dir = _resolve_input_files(args)
                review_dir = Path("review")
                mappings_path = review_dir / "metadata_changes.json"
                run_backfill_generate(
                    files,
                    base_dir,
                    mappings_path,
                    review_dir,
                    model=model,
                    num_frames_for_caption=num_frames_for_caption,
                )
                output.console.print("\n[bold]Next steps:[/bold]")
                output.console.print(
                    "  Review the generated Description/Keywords (and "
                    f"preview JPEGs) in [cyan]{mappings_path}[/cyan], then "
                    "run:\n  [cyan]slate --metadata-backfill "
                    f"--metadata-mappings={mappings_path}[/cyan]"
                )
            else:
                run_backfill_apply(
                    args.metadata_mappings,
                    Path.cwd(),
                    assume_yes=args.yes,
                    app_version=APP_VERSION,
                    caption_model=model,
                )

        elif args.dry_run:
            files, base_dir = _resolve_input_files(args)
            review_dir = Path("review")
            mappings_path = review_dir / "rename_mappings.json"
            _, new_entries, _ = run_phase1(
                files,
                base_dir,
                mappings_path,
                review_dir,
                model=model,
                prompt=config.prompt,
                prepend=prepend,
                prefix=prefix,
                suffix=suffix,
                max_file_name_length=config.max_file_name_length,
                num_frames_for_caption=num_frames_for_caption,
                add_metadata=add_metadata,
                verbose=args.verbose,
            )
            _print_phase1_next_steps(
                review_dir, mappings_path, new_entries, add_metadata=add_metadata
            )

        elif args.rename_only:
            base_dir = args.input_dir if args.input_dir else Path.cwd()
            _check_mapping_version_or_exit(args.rename_mappings)
            entries = load_mappings(args.rename_mappings)
            run_phase2(
                entries,
                base_dir,
                args.rename_mappings,
                generate_undo_script=generate_undo,
                assume_yes=args.yes,
                add_metadata=add_metadata,
                app_version=APP_VERSION,
                caption_model=model,
                prefix=prefix,
                suffix=suffix,
                prepend=prepend,
                max_file_name_length=config.max_file_name_length,
                verbose=args.verbose,
            )

        elif args.process_and_rename:
            files, base_dir = _resolve_input_files(args)
            review_dir = Path("review")
            mappings_path = review_dir / "rename_mappings.json"
            all_entries, new_entries, _skipped = run_phase1(
                files,
                base_dir,
                mappings_path,
                review_dir,
                model=model,
                prompt=config.prompt,
                prepend=prepend,
                prefix=prefix,
                suffix=suffix,
                max_file_name_length=config.max_file_name_length,
                num_frames_for_caption=num_frames_for_caption,
                add_metadata=add_metadata,
                verbose=args.verbose,
            )
            newly_processed_ok = [e for e in new_entries if e.status == "ok"]
            run_phase2(
                all_entries,
                base_dir,
                mappings_path,
                generate_undo_script=generate_undo,
                assume_yes=args.yes,
                phase3_newly_processed_ok=newly_processed_ok,
                add_metadata=add_metadata,
                app_version=APP_VERSION,
                caption_model=model,
                prefix=prefix,
                suffix=suffix,
                prepend=prepend,
                max_file_name_length=config.max_file_name_length,
                verbose=args.verbose,
            )

        elif args.model_update_check:
            output.info(f"Checking Hugging Face Hub for updates to {model}...")
            updated, path = check_for_model_updates(model)
            if updated:
                output.ok(f"Downloaded a new snapshot of {model} -> {path}")
            else:
                output.info(f"{model} is already up to date ({path})")

    except UsageError as e:
        parser.error(str(e))


if __name__ == "__main__":
    main()
