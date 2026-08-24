from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from rich.markup import escape
from rich.prompt import Confirm

from slate import output
from slate.config import load_config
from slate.extraction import ExtractionError, extract_frame
from slate.filenames import assemble_stem, normalize_caption, truncate_caption
from slate.inference import check_for_model_updates, generate_caption
from slate.mappings import (
    MappingEntry,
    disambiguate,
    find_existing_match,
    load_mappings,
    save_mappings,
    sort_key,
)
from slate.pairing import build_groups, discover_input_dir
from slate.preflight import run_preflight_checks
from slate.rename import (
    RenameLogEntry,
    build_rename_plan,
    perform_renames,
    write_audit_trail,
    write_undo_script,
)


class UsageError(Exception):
    pass


_USAGE_EXAMPLES = [
    (
        "Phase 1: scan a directory, caption clips, write "
        "rename_mappings.json for review",
        "slate --input-dir ~/Movies/Footage --dry-run",
    ),
    (
        "Phase 2: apply a reviewed (optionally hand-edited) rename_mappings.json",
        "slate --input-dir ~/Movies/Footage --rename-only \\\n"
        "      --rename-mappings=rename_mappings.json",
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="slate",
        add_help=False,
        description=(
            "Caption and rename camera footage using a local vision-language\n"
            "model. Exactly one of --dry-run / --rename-only /\n"
            "--process-and-rename / --model-update-check is required."
        ),
    )
    parser.add_argument(
        "-h",
        "--help",
        action=_HelpAction,
        help="show this help message and exit",
    )

    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Phase 1: scan, pair MOV/MP4 files, and caption each clip. "
            "Writes rename_mappings.json plus captioned preview JPEGs under "
            "review/. Never renames or otherwise modifies source files. Safe "
            "to re-run on the same folder -- groups already in "
            "rename_mappings.json are skipped and carried over unchanged."
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
        help="Path to the rename_mappings.json to apply. Required by --rename-only.",
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

    return parser


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

    generate_undo = (
        False if args.skip_generate_undo_script else config.generate_undo_script
    )

    return config, model, prepend, prefix, suffix, generate_undo


def _run_preflight_or_exit() -> None:
    failures = run_preflight_checks()
    if failures:
        output.fatal("slate cannot run in this environment:")
        for message in failures:
            output.fatal(f"  - {message}")
        sys.exit(1)


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
) -> tuple[list[MappingEntry], list[MappingEntry], list[MappingEntry]]:
    """Returns (all_entries, new_entries, skipped_entries)."""
    groups = build_groups(files)
    existing = load_mappings(mappings_path)

    all_entries: list[MappingEntry] = []
    new_entries: list[MappingEntry] = []
    skipped_entries: list[MappingEntry] = []

    review_dir.mkdir(parents=True, exist_ok=True)

    for group in groups:
        original_files = group.original_files
        match = find_existing_match(existing, original_files)
        if match is not None:
            output.skip(
                f"already in rename_mappings.json: {' / '.join(original_files)}"
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
        try:
            extract_frame(group.source_file, tmp_frame_path)
        except ExtractionError as e:
            entry = MappingEntry(
                status="error", original_files=original_files, error=str(e)
            )
            new_entries.append(entry)
            all_entries.append(entry)
            output.error(f"{' / '.join(original_files)}: {e}")
            continue

        raw_caption = generate_caption(str(tmp_frame_path), prompt, model)
        caption = truncate_caption(normalize_caption(raw_caption))

        new_stem = assemble_stem(
            original_stem=group.source_file.stem,
            caption=caption,
            prefix=prefix,
            suffix=suffix,
            prepend_caption=prepend,
            max_length=max_file_name_length,
        )

        preview_name = f"{new_stem}.jpg"
        preview_path = review_dir / preview_name
        tmp_frame_path.rename(preview_path)

        entry = MappingEntry(
            status="ok",
            original_files=original_files,
            new_stem=new_stem,
            preview_jpeg=str(Path(review_dir.name) / preview_name),
            source_used_for_caption=group.source_file.name,
        )
        new_entries.append(entry)
        all_entries.append(entry)
        output.ok(f"{' / '.join(original_files)} -> {new_stem}")

    disambiguated = disambiguate(all_entries)
    for entry in disambiguated:
        if entry.preview_jpeg is None:
            continue
        old_preview_path = mappings_path.parent / entry.preview_jpeg
        new_preview_name = f"{entry.new_stem}.jpg"
        new_preview_path = review_dir / new_preview_name
        if old_preview_path.is_file():
            old_preview_path.rename(new_preview_path)
        entry.preview_jpeg = str(Path(review_dir.name) / new_preview_name)

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


# --- Phase 2: --rename-only / Phase 3: --process-and-rename --------------


def run_phase2(
    entries: list[MappingEntry],
    base_dir: Path,
    mappings_path: Path,
    *,
    generate_undo_script: bool,
    assume_yes: bool,
    phase3_newly_processed_ok: list[MappingEntry] | None = None,
) -> None:
    plan = build_rename_plan(entries, base_dir)

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
            confirmed = _prompt_phase3(plan, phase3_newly_processed_ok)
        else:
            confirmed = _prompt_phase2(plan)
        if not confirmed:
            output.warn("Aborted -- no files renamed.")
            return

    log: list[RenameLogEntry] = []
    try:
        perform_renames(
            plan,
            log,
            on_rename=lambda e: output.renamed(
                f"{e.old_path.name} -> {e.new_path.name}"
            ),
        )
    finally:
        timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        if mappings_path.is_file():
            top_level_dir = mappings_path.parent
            applied_path = write_audit_trail(mappings_path, timestamp)
            output.info(
                f"Audit trail written: {applied_path.relative_to(top_level_dir)}"
            )
            if generate_undo_script and log:
                undo_path = top_level_dir / f"undo_renames_{timestamp}.sh"
                write_undo_script(log, undo_path)
                output.info(f"Undo script written: {undo_path.name}")


def _prompt_phase2(plan) -> bool:
    message = f"{len(plan.operations)} rename operations"
    if plan.problem_count:
        message += f", [yellow]{plan.problem_count} issue(s)[/yellow] reported above"
    return Confirm.ask(message, default=False)


def _prompt_phase3(plan, newly_processed_ok: list[MappingEntry]) -> bool:
    newly_processed_ids = {id(e) for e in newly_processed_ok}
    newly_captioned_in_plan = sum(
        1 for op in plan.operations if id(op.entry) in newly_processed_ids
    )
    carried_over_in_plan = len(plan.operations) - newly_captioned_in_plan

    output.warn(
        "Phase 3 (--process-and-rename): no review checkpoint -- captions "
        "below have not been manually reviewed."
    )
    output.console.print(
        f"\n{len(plan.operations)} rename operations pending "
        f"([green]{newly_captioned_in_plan} newly captioned[/green], "
        f"[cyan]{carried_over_in_plan} carried over[/cyan] from a previous run).\n"
    )

    sample = sorted(newly_processed_ok, key=sort_key)[:3]
    if sample:
        output.console.print("[bold]Sample of newly generated captions:[/bold]")
        for entry in sample:
            output.console.print(
                f"  {min(entry.original_files)}  ->  [italic]{entry.new_stem}[/italic]"
            )
        output.console.print()

    return Confirm.ask(f"Continue with {len(plan.operations)} renames?", default=False)


# --- Entry point -----------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.dry_run or args.process_and_rename:
            if not args.input_dir and not args.input_files:
                raise UsageError(
                    "--dry-run/--process-and-rename requires --input-dir or "
                    "--input-files"
                )
        if args.rename_only and not args.rename_mappings:
            raise UsageError("--rename-only requires --rename-mappings")

        _run_preflight_or_exit()

        config, model, prepend, prefix, suffix, generate_undo = _effective_settings(
            args
        )

        if args.dry_run:
            files, base_dir = _resolve_input_files(args)
            mappings_path = Path("rename_mappings.json")
            review_dir = Path("review")
            run_phase1(
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
            )

        elif args.rename_only:
            base_dir = args.input_dir if args.input_dir else Path.cwd()
            entries = load_mappings(args.rename_mappings)
            run_phase2(
                entries,
                base_dir,
                args.rename_mappings,
                generate_undo_script=generate_undo,
                assume_yes=args.yes,
            )

        elif args.process_and_rename:
            files, base_dir = _resolve_input_files(args)
            mappings_path = Path("rename_mappings.json")
            review_dir = Path("review")
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
            )
            newly_processed_ok = [e for e in new_entries if e.status == "ok"]
            run_phase2(
                all_entries,
                base_dir,
                mappings_path,
                generate_undo_script=generate_undo,
                assume_yes=args.yes,
                phase3_newly_processed_ok=newly_processed_ok,
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
