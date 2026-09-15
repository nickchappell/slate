import slate.backfill as backfill
from slate.mappings import (
    MetadataChangeEntry,
    save_metadata_changes,
)
from slate.metadata import EmbedOutcome, ExistingMetadata


def touch(path):
    path.write_bytes(b"")
    return path


class TestRunBackfillGenerate:
    def _stub_pipeline(self, monkeypatch, caption_text):
        monkeypatch.setattr(backfill, "validate_media_files", lambda files: (files, []))

        def fake_extract_frames(source, output_dir, num_frames):
            frame = output_dir / "frame_0.jpg"
            frame.write_bytes(b"fake-frame-bytes")
            return [frame]

        monkeypatch.setattr(backfill, "extract_frames", fake_extract_frames)
        monkeypatch.setattr(backfill, "generate_caption", lambda *a, **k: caption_text)
        monkeypatch.setattr(
            backfill,
            "build_montage",
            lambda frame_paths, output_jpeg: output_jpeg.write_bytes(b"fake-montage"),
        )

    def test_uses_backfill_prompt_and_token_budget(self, tmp_path, monkeypatch):
        source = touch(tmp_path / "A017_C015 red kayak at sunset.MOV")
        self._stub_pipeline(
            monkeypatch, "LONG: A red kayak drifts.\nKEYWORDS: kayak, lake"
        )
        calls = {}

        def fake_generate_caption(image_paths, prompt, model, **kwargs):
            calls["prompt"] = prompt
            calls["kwargs"] = kwargs
            return "LONG: A red kayak drifts.\nKEYWORDS: kayak, lake"

        monkeypatch.setattr(backfill, "generate_caption", fake_generate_caption)

        review_dir = tmp_path / "review"
        mappings_path = review_dir / "metadata_changes.json"
        backfill.run_backfill_generate(
            [source],
            tmp_path,
            mappings_path,
            review_dir,
            model="fake-model",
            num_frames_for_caption=3,
        )

        assert calls["prompt"] == backfill.METADATA_BACKFILL_PROMPT
        expected_tokens = backfill.MAX_CAPTION_TOKENS_WITH_METADATA
        assert calls["kwargs"]["max_tokens"] == expected_tokens

    def test_entry_fields_populated_no_short_caption(self, tmp_path, monkeypatch):
        source = touch(tmp_path / "A017_C015 red kayak at sunset.MOV")
        self._stub_pipeline(
            monkeypatch,
            "LONG: A red kayak drifts across a lake.\nKEYWORDS: kayak, lake",
        )

        review_dir = tmp_path / "review"
        mappings_path = review_dir / "metadata_changes.json"
        all_entries, new_entries, _skipped = backfill.run_backfill_generate(
            [source],
            tmp_path,
            mappings_path,
            review_dir,
            model="fake-model",
            num_frames_for_caption=3,
        )

        entry = new_entries[0]
        assert entry.current_files == ["A017_C015 red kayak at sunset.MOV"]
        assert entry.title == "A017_C015 red kayak at sunset"
        assert entry.long_caption == "A red kayak drifts across a lake."
        assert entry.keywords == ["kayak", "lake"]
        assert entry.captioned_at is not None
        assert entry.preview_jpeg == "A017_C015 red kayak at sunset.jpg"
        assert (review_dir / entry.preview_jpeg).is_file()

    def test_rerun_skips_already_present_current_files(self, tmp_path, monkeypatch):
        source = touch(tmp_path / "a.MOV")
        self._stub_pipeline(monkeypatch, "LONG: A caption.\nKEYWORDS: a, b")

        review_dir = tmp_path / "review"
        mappings_path = review_dir / "metadata_changes.json"
        backfill.run_backfill_generate(
            [source],
            tmp_path,
            mappings_path,
            review_dir,
            model="fake-model",
            num_frames_for_caption=3,
        )

        def boom(*a, **k):
            raise AssertionError("generate_caption should not be called again")

        monkeypatch.setattr(backfill, "generate_caption", boom)

        all_entries, new_entries, skipped_entries = backfill.run_backfill_generate(
            [source],
            tmp_path,
            mappings_path,
            review_dir,
            model="fake-model",
            num_frames_for_caption=3,
        )
        assert new_entries == []
        assert len(skipped_entries) == 1
        assert len(all_entries) == 1

    def test_pair_grouping_shares_one_entry(self, tmp_path, monkeypatch):
        touch(tmp_path / "a.MOV")
        touch(tmp_path / "a.MP4")
        self._stub_pipeline(monkeypatch, "LONG: A caption.\nKEYWORDS: a, b")

        review_dir = tmp_path / "review"
        mappings_path = review_dir / "metadata_changes.json"
        all_entries, new_entries, _skipped = backfill.run_backfill_generate(
            [tmp_path / "a.MOV", tmp_path / "a.MP4"],
            tmp_path,
            mappings_path,
            review_dir,
            model="fake-model",
            num_frames_for_caption=3,
        )
        assert len(new_entries) == 1
        assert sorted(new_entries[0].current_files) == ["a.MOV", "a.MP4"]

    def test_extraction_error_becomes_error_entry(self, tmp_path, monkeypatch):
        source = touch(tmp_path / "a.MOV")
        monkeypatch.setattr(backfill, "validate_media_files", lambda files: (files, []))

        def raising_extract(source, output_dir, num_frames):
            raise backfill.ExtractionError("decode failed")

        monkeypatch.setattr(backfill, "extract_frames", raising_extract)

        review_dir = tmp_path / "review"
        mappings_path = review_dir / "metadata_changes.json"
        _all, new_entries, _skipped = backfill.run_backfill_generate(
            [source],
            tmp_path,
            mappings_path,
            review_dir,
            model="fake-model",
            num_frames_for_caption=3,
        )
        assert new_entries[0].status == "error"
        assert "decode failed" in new_entries[0].error


class TestRunBackfillApply:
    def test_title_rederived_live_not_from_stored_json(self, tmp_path, monkeypatch):
        # The file gets renamed again between generate and apply -- title
        # must reflect the *current* name, not metadata_changes.json's
        # stored (now-stale) title field.
        current_path = tmp_path / "renamed again.MOV"
        touch(current_path)
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        mappings_path = review_dir / "metadata_changes.json"
        save_metadata_changes(
            mappings_path,
            [
                MetadataChangeEntry(
                    status="ok",
                    current_files=["renamed again.MOV"],
                    title="stale title from generate time",
                    long_caption="A caption.",
                    keywords=["a"],
                    captioned_at="2026-09-14T18:32:07Z",
                )
            ],
        )

        calls = []

        def fake_embed(path, **kwargs):
            calls.append((path, kwargs))
            return EmbedOutcome(embedded=True)

        monkeypatch.setattr(backfill, "embed_metadata", fake_embed)
        monkeypatch.setattr(
            backfill,
            "read_existing_metadata",
            lambda path: ExistingMetadata(
                title=None, description=None, keywords=None, has_slate_provenance=False
            ),
        )

        backfill.run_backfill_apply(
            mappings_path,
            tmp_path,
            assume_yes=True,
            app_version="0.2.2",
            caption_model="model",
        )

        path, kwargs = calls[0]
        assert kwargs["title"] == "renamed again"
        assert kwargs["generated_at"] == "2026-09-14T18:32:07Z"

    def test_missing_file_warns_and_skips(self, tmp_path, capsys):
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        mappings_path = review_dir / "metadata_changes.json"
        save_metadata_changes(
            mappings_path,
            [
                MetadataChangeEntry(
                    status="ok",
                    current_files=["ghost.MOV"],
                    title="ghost",
                    long_caption="A caption.",
                    keywords=["a"],
                )
            ],
        )

        backfill.run_backfill_apply(
            mappings_path,
            tmp_path,
            assume_yes=True,
            app_version="0.2.2",
            caption_model="model",
        )

        out = capsys.readouterr().out
        assert "no longer exists on disk" in out
        # Nothing to apply -- mappings_path is never archived in this case.
        assert mappings_path.is_file()

    def test_idempotency_guard_skips_already_provenanced_files(
        self, tmp_path, monkeypatch, capsys
    ):
        touch(tmp_path / "a.MOV")
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        mappings_path = review_dir / "metadata_changes.json"
        save_metadata_changes(
            mappings_path,
            [
                MetadataChangeEntry(
                    status="ok",
                    current_files=["a.MOV"],
                    title="a",
                    long_caption="A caption.",
                    keywords=["a"],
                )
            ],
        )

        def boom(*a, **k):
            raise AssertionError("embed_metadata should not have been called")

        monkeypatch.setattr(backfill, "embed_metadata", boom)
        monkeypatch.setattr(
            backfill,
            "read_existing_metadata",
            lambda path: ExistingMetadata(
                title=None, description=None, keywords=None, has_slate_provenance=True
            ),
        )

        backfill.run_backfill_apply(
            mappings_path,
            tmp_path,
            assume_yes=True,
            app_version="0.2.2",
            caption_model="model",
        )

        out = capsys.readouterr().out
        assert "already has slate metadata" in out
        # Rich may wrap this across lines depending on terminal width --
        # check the substrings independently rather than one exact phrase.
        assert "already" in out and "processed (skipped)" in out

    def test_archives_mappings_file_on_completion(self, tmp_path, monkeypatch):
        touch(tmp_path / "a.MOV")
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        mappings_path = review_dir / "metadata_changes.json"
        save_metadata_changes(
            mappings_path,
            [
                MetadataChangeEntry(
                    status="ok",
                    current_files=["a.MOV"],
                    title="a",
                    long_caption="A caption.",
                    keywords=["a"],
                )
            ],
        )
        monkeypatch.setattr(
            backfill, "embed_metadata", lambda *a, **k: EmbedOutcome(embedded=True)
        )
        monkeypatch.setattr(
            backfill,
            "read_existing_metadata",
            lambda path: ExistingMetadata(
                title=None, description=None, keywords=None, has_slate_provenance=False
            ),
        )

        backfill.run_backfill_apply(
            mappings_path,
            tmp_path,
            assume_yes=True,
            app_version="0.2.2",
            caption_model="model",
        )

        assert not mappings_path.exists()
        applied_files = list(review_dir.glob("applied_metadata_changes_*.json"))
        assert len(applied_files) == 1

    def test_declined_confirmation_applies_nothing(self, tmp_path, monkeypatch):
        touch(tmp_path / "a.MOV")
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        mappings_path = review_dir / "metadata_changes.json"
        save_metadata_changes(
            mappings_path,
            [
                MetadataChangeEntry(
                    status="ok",
                    current_files=["a.MOV"],
                    title="a",
                    long_caption="A caption.",
                    keywords=["a"],
                )
            ],
        )

        def boom(*a, **k):
            raise AssertionError("embed_metadata should not have been called")

        monkeypatch.setattr(backfill, "embed_metadata", boom)
        monkeypatch.setattr(backfill.Confirm, "ask", lambda *a, **k: False)

        backfill.run_backfill_apply(
            mappings_path,
            tmp_path,
            assume_yes=False,
            app_version="0.2.2",
            caption_model="model",
        )

        assert mappings_path.is_file()

    def test_preserved_field_is_reported(self, tmp_path, monkeypatch, capsys):
        touch(tmp_path / "a.MOV")
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        mappings_path = review_dir / "metadata_changes.json"
        save_metadata_changes(
            mappings_path,
            [
                MetadataChangeEntry(
                    status="ok",
                    current_files=["a.MOV"],
                    title="a",
                    long_caption="A caption.",
                    keywords=["a"],
                )
            ],
        )
        preserved_outcome = EmbedOutcome(
            embedded=True, preserved_fields=["Description"]
        )
        monkeypatch.setattr(
            backfill, "embed_metadata", lambda *a, **k: preserved_outcome
        )
        monkeypatch.setattr(
            backfill,
            "read_existing_metadata",
            lambda path: ExistingMetadata(
                title=None,
                description="Edited in FCPX",
                keywords=None,
                has_slate_provenance=False,
            ),
        )

        backfill.run_backfill_apply(
            mappings_path,
            tmp_path,
            assume_yes=True,
            app_version="0.2.2",
            caption_model="model",
        )

        out = capsys.readouterr().out
        assert "Pre-existing Description preserved" in out
        assert "1 embedded, 1 preserved pre-existing field(s), 0 failed" in out
