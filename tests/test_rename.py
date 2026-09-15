from pathlib import Path

import slate.rename as rename
from slate.mappings import MappingEntry
from slate.metadata import EmbedOutcome
from slate.rename import (
    RenameLogEntry,
    build_rename_plan,
    perform_renames,
    write_audit_trail,
    write_undo_script,
)


def touch(path: Path) -> Path:
    path.write_bytes(b"")
    return path


class TestBuildRenamePlan:
    def test_ok_group_with_existing_files_produces_operation(self, tmp_path):
        touch(tmp_path / "a.MOV")
        touch(tmp_path / "a.MP4")
        entries = [
            MappingEntry(
                status="ok", original_files=["a.MOV", "a.MP4"], new_stem="a caption"
            )
        ]
        plan = build_rename_plan(entries, tmp_path)
        assert len(plan.operations) == 1
        op = plan.operations[0]
        assert sorted(p.name for p in op.new_paths) == [
            "a caption.MOV",
            "a caption.MP4",
        ]

    def test_error_groups_are_counted_not_planned(self, tmp_path):
        entries = [MappingEntry(status="error", original_files=["a.MOV"], error="boom")]
        plan = build_rename_plan(entries, tmp_path)
        assert plan.operations == []
        assert plan.error_group_count == 1

    def test_whole_group_missing_is_reported_and_skipped(self, tmp_path):
        entries = [
            MappingEntry(
                status="ok", original_files=["ghost.MOV", "ghost.MP4"], new_stem="x"
            )
        ]
        plan = build_rename_plan(entries, tmp_path)
        assert plan.operations == []
        assert len(plan.whole_group_missing) == 1
        assert "ghost.MOV" in plan.whole_group_missing[0]

    def test_partial_pair_missing_is_warned_and_skipped(self, tmp_path):
        touch(tmp_path / "a.MOV")
        # a.MP4 deliberately absent
        entries = [
            MappingEntry(
                status="ok", original_files=["a.MOV", "a.MP4"], new_stem="a caption"
            )
        ]
        plan = build_rename_plan(entries, tmp_path)
        assert plan.operations == []
        assert len(plan.partial_pair_missing) == 1
        assert "a.MP4" in plan.partial_pair_missing[0]
        assert (tmp_path / "a.MOV").is_file()  # surviving file left untouched

    def test_collision_with_existing_file_on_disk_is_skipped(self, tmp_path):
        touch(tmp_path / "a.MOV")
        touch(tmp_path / "already taken.MOV")
        entries = [
            MappingEntry(
                status="ok", original_files=["a.MOV"], new_stem="already taken"
            )
        ]
        plan = build_rename_plan(entries, tmp_path)
        assert plan.operations == []
        assert len(plan.collisions) == 1

    def test_collision_between_two_groups_in_same_batch_is_skipped(self, tmp_path):
        touch(tmp_path / "a.MOV")
        touch(tmp_path / "b.MOV")
        entries = [
            MappingEntry(status="ok", original_files=["a.MOV"], new_stem="same name"),
            MappingEntry(status="ok", original_files=["b.MOV"], new_stem="same name"),
        ]
        plan = build_rename_plan(entries, tmp_path)
        # first in sorted order claims it, second collides
        assert len(plan.operations) == 1
        assert plan.operations[0].old_paths[0].name == "a.MOV"
        assert len(plan.collisions) == 1

    def test_renaming_to_same_name_is_not_a_collision(self, tmp_path):
        touch(tmp_path / "a.MOV")
        entries = [MappingEntry(status="ok", original_files=["a.MOV"], new_stem="a")]
        plan = build_rename_plan(entries, tmp_path)
        assert len(plan.operations) == 1
        assert plan.collisions == []

    def test_operations_in_deterministic_sorted_order(self, tmp_path):
        touch(tmp_path / "b.MOV")
        touch(tmp_path / "a.MOV")
        entries = [
            MappingEntry(status="ok", original_files=["b.MOV"], new_stem="b caption"),
            MappingEntry(status="ok", original_files=["a.MOV"], new_stem="a caption"),
        ]
        plan = build_rename_plan(entries, tmp_path)
        assert [op.old_paths[0].name for op in plan.operations] == ["a.MOV", "b.MOV"]


class TestPerformRenames:
    def test_renames_files_on_disk(self, tmp_path):
        touch(tmp_path / "a.MOV")
        touch(tmp_path / "a.MP4")
        entries = [
            MappingEntry(
                status="ok", original_files=["a.MOV", "a.MP4"], new_stem="a caption"
            )
        ]
        plan = build_rename_plan(entries, tmp_path)
        log: list[RenameLogEntry] = []
        perform_renames(plan, log)
        assert not (tmp_path / "a.MOV").exists()
        assert (tmp_path / "a caption.MOV").is_file()
        assert (tmp_path / "a caption.MP4").is_file()

    def test_log_built_incrementally_in_place(self, tmp_path):
        touch(tmp_path / "a.MOV")
        entries = [
            MappingEntry(status="ok", original_files=["a.MOV"], new_stem="a caption")
        ]
        plan = build_rename_plan(entries, tmp_path)
        log: list[RenameLogEntry] = []
        perform_renames(plan, log)
        assert len(log) == 1
        assert log[0].old_path.name == "a.MOV"
        assert log[0].new_path.name == "a caption.MOV"

    def test_on_rename_callback_invoked_per_file(self, tmp_path):
        touch(tmp_path / "a.MOV")
        touch(tmp_path / "a.MP4")
        entries = [
            MappingEntry(
                status="ok", original_files=["a.MOV", "a.MP4"], new_stem="a caption"
            )
        ]
        plan = build_rename_plan(entries, tmp_path)
        seen = []
        perform_renames(plan, [], on_rename=lambda e: seen.append(e.old_path.name))
        assert seen == ["a.MOV", "a.MP4"]

    def test_embed_metadata_flag_false_never_touches_metadata_module(
        self, tmp_path, monkeypatch
    ):
        # Regression guard: a plain rename batch must stay byte-identical
        # in behavior -- embed_metadata_flag defaults to False and must
        # never call into metadata.py at all.
        def boom(*a, **k):
            raise AssertionError("embed_metadata should not have been called")

        monkeypatch.setattr(rename, "embed_metadata", boom)
        touch(tmp_path / "a.MOV")
        entries = [
            MappingEntry(
                status="ok",
                original_files=["a.MOV"],
                new_stem="a caption",
                long_caption="A caption.",
                keywords=["a"],
            )
        ]
        plan = build_rename_plan(entries, tmp_path)
        log: list[RenameLogEntry] = []
        perform_renames(plan, log)
        assert log[0].metadata_embedded is False

    def test_embed_metadata_success_sets_flag_and_fires_on_metadata(
        self, tmp_path, monkeypatch
    ):
        outcome = EmbedOutcome(embedded=True, preserved_fields=[])
        monkeypatch.setattr(rename, "embed_metadata", lambda *a, **k: outcome)
        touch(tmp_path / "a.MOV")
        entries = [
            MappingEntry(
                status="ok",
                original_files=["a.MOV"],
                new_stem="a caption",
                long_caption="A caption.",
                keywords=["a"],
            )
        ]
        plan = build_rename_plan(entries, tmp_path)
        log: list[RenameLogEntry] = []
        seen = []
        perform_renames(
            plan,
            log,
            embed_metadata_flag=True,
            on_metadata=lambda path, o: seen.append((path.name, o)),
        )
        assert log[0].metadata_embedded is True
        assert seen == [("a caption.MOV", outcome)]

    def test_embed_metadata_forwards_entry_fields_and_provenance_args(
        self, tmp_path, monkeypatch
    ):
        calls = []

        def fake_embed(path, **kwargs):
            calls.append((path, kwargs))
            return EmbedOutcome(embedded=True)

        monkeypatch.setattr(rename, "embed_metadata", fake_embed)
        touch(tmp_path / "a.MOV")
        entries = [
            MappingEntry(
                status="ok",
                original_files=["a.MOV"],
                new_stem="a caption",
                long_caption="A long caption.",
                keywords=["a", "b"],
                captioned_at="2026-09-14T18:32:07Z",
            )
        ]
        plan = build_rename_plan(entries, tmp_path)
        perform_renames(
            plan,
            [],
            embed_metadata_flag=True,
            app_version="0.2.2",
            caption_model="some/model",
        )
        path, kwargs = calls[0]
        assert path.name == "a caption.MOV"
        assert kwargs["title"] == "a caption"
        assert kwargs["description"] == "A long caption."
        assert kwargs["keywords"] == ["a", "b"]
        assert kwargs["original_filename"] == "a.MOV"
        assert kwargs["app_version"] == "0.2.2"
        assert kwargs["caption_model"] == "some/model"
        assert kwargs["generated_at"] == "2026-09-14T18:32:07Z"

    def test_generated_at_comes_from_each_entrys_captioned_at(
        self, tmp_path, monkeypatch
    ):
        # Not a batch-wide timestamp -- different entries in the same batch
        # can have been captioned at different times (some freshly, some
        # carried over from an earlier run).
        calls = []

        def fake_embed(path, **kwargs):
            calls.append(kwargs["generated_at"])
            return EmbedOutcome(embedded=True)

        monkeypatch.setattr(rename, "embed_metadata", fake_embed)
        touch(tmp_path / "a.MOV")
        touch(tmp_path / "b.MOV")
        entries = [
            MappingEntry(
                status="ok",
                original_files=["a.MOV"],
                new_stem="a caption",
                long_caption="A caption.",
                keywords=["a"],
                captioned_at="2026-01-01T00:00:00Z",
            ),
            MappingEntry(
                status="ok",
                original_files=["b.MOV"],
                new_stem="b caption",
                long_caption="B caption.",
                keywords=["b"],
                captioned_at="2026-09-14T18:32:07Z",
            ),
        ]
        plan = build_rename_plan(entries, tmp_path)
        perform_renames(plan, [], embed_metadata_flag=True)
        assert sorted(calls) == ["2026-01-01T00:00:00Z", "2026-09-14T18:32:07Z"]

    def test_missing_long_caption_or_keywords_skips_embed_and_fires_none(
        self, tmp_path, monkeypatch
    ):
        def boom(*a, **k):
            raise AssertionError("embed_metadata should not have been called")

        monkeypatch.setattr(rename, "embed_metadata", boom)
        touch(tmp_path / "a.MOV")
        entries = [
            MappingEntry(status="ok", original_files=["a.MOV"], new_stem="a caption")
        ]
        plan = build_rename_plan(entries, tmp_path)
        log: list[RenameLogEntry] = []
        seen = []
        perform_renames(
            plan,
            log,
            embed_metadata_flag=True,
            on_metadata=lambda path, o: seen.append(o),
        )
        assert log[0].metadata_embedded is False
        assert seen == [None]

    def test_embed_metadata_runs_for_each_file_in_a_pair(self, tmp_path, monkeypatch):
        seen = []

        def fake_embed(path, **kwargs):
            seen.append(path.name)
            return EmbedOutcome(embedded=True)

        monkeypatch.setattr(rename, "embed_metadata", fake_embed)
        touch(tmp_path / "a.MOV")
        touch(tmp_path / "a.MP4")
        entries = [
            MappingEntry(
                status="ok",
                original_files=["a.MOV", "a.MP4"],
                new_stem="a caption",
                long_caption="A caption.",
                keywords=["a"],
            )
        ]
        plan = build_rename_plan(entries, tmp_path)
        perform_renames(plan, [], embed_metadata_flag=True)
        assert sorted(seen) == ["a caption.MOV", "a caption.MP4"]


class TestWriteAuditTrail:
    def test_renames_mappings_file_to_applied_name(self, tmp_path):
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        mappings_path = review_dir / "rename_mappings.json"
        mappings_path.write_text("[]")
        applied_path = write_audit_trail(mappings_path, "20260101T000000")
        assert not mappings_path.exists()
        assert applied_path.name == "applied_renames_20260101T000000.json"
        assert applied_path.read_text() == "[]"

    def test_writes_alongside_mappings_path(self, tmp_path):
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        mappings_path = review_dir / "rename_mappings.json"
        mappings_path.write_text("[]")
        applied_path = write_audit_trail(mappings_path, "20260101T000000")
        assert applied_path.parent == review_dir

    def test_preserves_other_files_already_in_review_dir(self, tmp_path):
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        (review_dir / "some-preview.jpg").write_bytes(b"fake")
        mappings_path = review_dir / "rename_mappings.json"
        mappings_path.write_text("[]")
        applied_path = write_audit_trail(mappings_path, "20260101T000000")
        assert applied_path.parent == review_dir
        assert (review_dir / "some-preview.jpg").is_file()


class TestWriteUndoScript:
    def test_content_reverses_renames_and_quotes_paths(self, tmp_path):
        import shlex

        new_path = tmp_path / "a caption, take 2.MOV"
        old_path = tmp_path / "a.MOV"
        log = [RenameLogEntry(old_path=old_path, new_path=new_path)]
        script_path = tmp_path / "undo.sh"
        write_undo_script(log, script_path)
        content = script_path.read_text()
        assert "set -euo pipefail" in content
        assert (
            f"mv -n -- {shlex.quote(str(new_path))} {shlex.quote(str(old_path))}"
            in content
        )

    def test_script_is_executable(self, tmp_path):
        script_path = tmp_path / "undo.sh"
        write_undo_script([], script_path)
        assert script_path.stat().st_mode & 0o111 == 0o111

    def test_only_includes_entries_actually_in_log(self, tmp_path):
        log = [
            RenameLogEntry(
                old_path=tmp_path / "a.MOV", new_path=tmp_path / "a caption.MOV"
            )
        ]
        script_path = tmp_path / "undo.sh"
        write_undo_script(log, script_path)
        content = script_path.read_text()
        assert content.count("mv -n --") == 1

    def test_zero_metadata_embedded_entries_produce_no_exiftool_lines(self, tmp_path):
        # Regression guard: a batch with no embedded metadata must produce
        # byte-identical output to today -- no guard line, no exiftool call.
        log = [
            RenameLogEntry(
                old_path=tmp_path / "a.MOV",
                new_path=tmp_path / "a caption.MOV",
                metadata_embedded=False,
            )
        ]
        script_path = tmp_path / "undo.sh"
        write_undo_script(log, script_path)
        content = script_path.read_text()
        assert "exiftool" not in content

    def test_metadata_embedded_entry_gets_title_reset_after_its_mv_line(self, tmp_path):
        old_path = tmp_path / "a.MOV"
        new_path = tmp_path / "a caption.MOV"
        log = [
            RenameLogEntry(old_path=old_path, new_path=new_path, metadata_embedded=True)
        ]
        script_path = tmp_path / "undo.sh"
        write_undo_script(log, script_path)
        lines = script_path.read_text().splitlines()

        mv_index = next(i for i, line in enumerate(lines) if line.startswith("mv -n"))
        exiftool_index = mv_index + 1
        assert lines[exiftool_index].startswith("exiftool -overwrite_original")
        assert "-ItemList:Title=a.MOV" not in lines[exiftool_index]  # stem, not name
        assert "-ItemList:Title=a" in lines[exiftool_index]
        assert "-Keys:Title=a" in lines[exiftool_index]
        assert "-XMP-dc:Title=a" in lines[exiftool_index]

    def test_metadata_embedded_entry_triggers_exiftool_presence_guard(self, tmp_path):
        log = [
            RenameLogEntry(
                old_path=tmp_path / "a.MOV",
                new_path=tmp_path / "a caption.MOV",
                metadata_embedded=True,
            )
        ]
        script_path = tmp_path / "undo.sh"
        write_undo_script(log, script_path)
        content = script_path.read_text()
        assert content.count("command -v exiftool") == 1
        assert "brew install exiftool" in content

    def test_mixed_batch_only_embedded_entries_get_exiftool_lines(self, tmp_path):
        log = [
            RenameLogEntry(
                old_path=tmp_path / "a.MOV",
                new_path=tmp_path / "a caption.MOV",
                metadata_embedded=True,
            ),
            RenameLogEntry(
                old_path=tmp_path / "b.MOV",
                new_path=tmp_path / "b caption.MOV",
                metadata_embedded=False,
            ),
        ]
        script_path = tmp_path / "undo.sh"
        write_undo_script(log, script_path)
        content = script_path.read_text()
        assert content.count("mv -n --") == 2
        assert content.count("exiftool -overwrite_original") == 1
