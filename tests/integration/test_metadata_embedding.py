import json
import shutil
import subprocess
from pathlib import Path

import pytest

from slate import cli
from slate.config import Config
from slate.pairing import VIDEO_EXTENSIONS, discover_input_dir
from slate.rename import build_rename_plan, perform_renames, write_undo_script

FOOTAGE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "footage"


def _has_footage() -> bool:
    if not FOOTAGE_DIR.is_dir():
        return False
    return any(
        p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
        for p in FOOTAGE_DIR.iterdir()
    )


def _has_exiftool() -> bool:
    return shutil.which("exiftool") is not None


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _has_footage(),
        reason=(
            f"no real footage fixtures present in {FOOTAGE_DIR} -- "
            "see tests/fixtures/footage/README.md"
        ),
    ),
    pytest.mark.skipif(
        not _has_exiftool(),
        reason="exiftool not found on PATH -- see preflight.py's binary checks",
    ),
]


def _read_via_real_exiftool(path: Path) -> dict:
    result = subprocess.run(
        ["exiftool", "-j", "-Title", "-Description", "-Keywords", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(result.stdout)[0]


def test_dry_run_add_metadata_populates_caption_fields(tmp_path):
    files = discover_input_dir(FOOTAGE_DIR)
    assert files, "expected at least one video file in the footage fixture dir"

    config = Config()
    review_dir = tmp_path / "review"
    mappings_path = review_dir / "rename_mappings.json"

    all_entries, _new, _skipped = cli.run_phase1(
        files,
        FOOTAGE_DIR,
        mappings_path,
        review_dir,
        model=config.model,
        prompt=config.prompt,
        prepend=False,
        prefix="",
        suffix="",
        max_file_name_length=config.max_file_name_length,
        num_frames_for_caption=config.num_frames_for_caption,
        add_metadata=True,
    )

    ok_entries = [e for e in all_entries if e.status == "ok"]
    assert ok_entries, "expected at least one successfully captioned group"

    for entry in ok_entries:
        assert entry.short_caption
        assert entry.long_caption
        assert entry.keywords
        assert len(entry.keywords) >= 1
        assert entry.captioned_at


def test_rename_only_add_metadata_is_readable_back_via_real_exiftool(tmp_path):
    footage_copy = tmp_path / "footage"
    footage_copy.mkdir()
    for f in discover_input_dir(FOOTAGE_DIR):
        shutil.copyfile(f, footage_copy / f.name)
    files = discover_input_dir(footage_copy)

    config = Config()
    review_dir = tmp_path / "review"
    mappings_path = review_dir / "rename_mappings.json"

    all_entries, _new, _skipped = cli.run_phase1(
        files,
        footage_copy,
        mappings_path,
        review_dir,
        model=config.model,
        prompt=config.prompt,
        prepend=False,
        prefix="",
        suffix="",
        max_file_name_length=config.max_file_name_length,
        num_frames_for_caption=config.num_frames_for_caption,
        add_metadata=True,
    )
    ok_entries = [e for e in all_entries if e.status == "ok"]
    assert ok_entries

    cli.run_phase2(
        all_entries,
        footage_copy,
        mappings_path,
        generate_undo_script=True,
        assume_yes=True,
        add_metadata=True,
        app_version="test",
        caption_model=config.model,
    )

    entry = ok_entries[0]
    suffix = Path(entry.original_files[0]).suffix
    renamed_path = footage_copy / f"{entry.new_stem}{suffix}"
    assert renamed_path.is_file()

    real = _read_via_real_exiftool(renamed_path)
    assert real["Title"] == entry.new_stem
    assert real["Description"] == entry.long_caption


def test_undo_script_reverts_title(tmp_path):
    footage_copy = tmp_path / "footage"
    footage_copy.mkdir()
    for f in discover_input_dir(FOOTAGE_DIR):
        shutil.copyfile(f, footage_copy / f.name)
    files = discover_input_dir(footage_copy)
    original_name = files[0].name

    config = Config()
    review_dir = tmp_path / "review"
    mappings_path = review_dir / "rename_mappings.json"

    all_entries, _new, _skipped = cli.run_phase1(
        files,
        footage_copy,
        mappings_path,
        review_dir,
        model=config.model,
        prompt=config.prompt,
        prepend=False,
        prefix="",
        suffix="",
        max_file_name_length=config.max_file_name_length,
        num_frames_for_caption=config.num_frames_for_caption,
        add_metadata=True,
    )

    log = []
    plan = build_rename_plan(all_entries, footage_copy)
    perform_renames(
        plan,
        log,
        embed_metadata_flag=True,
        app_version="test",
        caption_model=config.model,
    )

    undo_path = tmp_path / "undo.sh"
    write_undo_script(log, undo_path)

    subprocess.run(["bash", str(undo_path)], check=True, cwd=tmp_path)

    reverted_path = footage_copy / original_name
    assert reverted_path.is_file()
    real = _read_via_real_exiftool(reverted_path)
    assert real["Title"] == Path(original_name).stem
