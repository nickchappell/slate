from pathlib import Path

import pytest

from slate import cli
from slate.config import Config
from slate.pairing import VIDEO_EXTENSIONS, discover_input_dir
from slate.preflight import run_preflight_checks

FOOTAGE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "footage"


def _has_footage() -> bool:
    if not FOOTAGE_DIR.is_dir():
        return False
    return any(
        p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
        for p in FOOTAGE_DIR.iterdir()
    )


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _has_footage(),
        reason=(
            f"no real footage fixtures present in {FOOTAGE_DIR} -- "
            "see tests/fixtures/footage/README.md"
        ),
    ),
]


def test_preflight_passes_on_this_machine():
    assert run_preflight_checks() == []


def test_dry_run_against_real_footage(tmp_path):
    files = discover_input_dir(FOOTAGE_DIR)
    assert files, "expected at least one video file in the footage fixture dir"

    config = Config()
    mappings_path = tmp_path / "mappings.json"
    review_dir = tmp_path / "review"

    all_entries, _new_entries, _skipped = cli.run_phase1(
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
    )

    assert all_entries, "expected at least one group to be produced"

    ok_entries = [e for e in all_entries if e.status == "ok"]
    error_entries = [e for e in all_entries if e.status == "error"]

    if error_entries:
        messages = "\n".join(f"  {e.original_files}: {e.error}" for e in error_entries)
        pytest.fail(
            f"{len(error_entries)} group(s) failed extraction/pairing:\n{messages}"
        )

    assert mappings_path.is_file()

    for entry in ok_entries:
        assert entry.new_stem
        assert entry.new_stem.strip() != ""
        preview_path = tmp_path / entry.preview_jpeg
        assert preview_path.is_file()
        assert preview_path.stat().st_size > 0
