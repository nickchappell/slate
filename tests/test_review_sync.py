from slate.mappings import MappingEntry
from slate.review_sync import hash_file, reconcile_short_caption_edits, sync_from_review


def write_jpeg(path, content=b"fake-jpeg-bytes"):
    path.write_bytes(content)
    return path


class TestHashFile:
    def test_same_content_same_hash(self, tmp_path):
        a = write_jpeg(tmp_path / "a.jpg", b"same")
        b = write_jpeg(tmp_path / "b.jpg", b"same")
        assert hash_file(a) == hash_file(b)

    def test_different_content_different_hash(self, tmp_path):
        a = write_jpeg(tmp_path / "a.jpg", b"one")
        b = write_jpeg(tmp_path / "b.jpg", b"two")
        assert hash_file(a) != hash_file(b)


class TestSyncFromReview:
    def test_renamed_jpeg_updates_new_stem_and_preview_jpeg(self, tmp_path):
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        content = b"frame-bytes"
        write_jpeg(review_dir / "renamed by human.jpg", content)
        entry = MappingEntry(
            status="ok",
            original_files=["a.MOV"],
            new_stem="original caption",
            preview_jpeg="original caption.jpg",
            preview_jpeg_sha256=hash_file(review_dir / "renamed by human.jpg"),
        )

        result = sync_from_review([entry], review_dir)

        assert result.renamed == [entry]
        assert entry.new_stem == "renamed by human"
        assert entry.preview_jpeg == "renamed by human.jpg"
        assert result.deleted == []
        assert entry.short_caption_locked is True

    def test_untouched_jpeg_is_a_no_op(self, tmp_path):
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        write_jpeg(review_dir / "original caption.jpg", b"frame-bytes")
        entry = MappingEntry(
            status="ok",
            original_files=["a.MOV"],
            new_stem="original caption",
            preview_jpeg="original caption.jpg",
            preview_jpeg_sha256=hash_file(review_dir / "original caption.jpg"),
        )

        result = sync_from_review([entry], review_dir)

        assert result.renamed == []
        assert result.deleted == []
        assert entry.new_stem == "original caption"
        assert entry.short_caption_locked is False

    def test_already_locked_entry_with_nothing_new_to_sync_stays_locked(self, tmp_path):
        # The JPEG already matches what's stored (e.g. from an earlier
        # run's sync-and-save) -- nothing for *this* call to do, but the
        # persisted lock from that earlier sync must survive untouched.
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        write_jpeg(review_dir / "renamed by human.jpg", b"frame-bytes")
        entry = MappingEntry(
            status="ok",
            original_files=["a.MOV"],
            new_stem="renamed by human",
            preview_jpeg="renamed by human.jpg",
            preview_jpeg_sha256=hash_file(review_dir / "renamed by human.jpg"),
            short_caption_locked=True,
        )

        result = sync_from_review([entry], review_dir)

        assert result.renamed == []
        assert entry.short_caption_locked is True

    def test_missing_jpeg_is_reported_deleted(self, tmp_path):
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        entry = MappingEntry(
            status="ok",
            original_files=["a.MOV"],
            new_stem="original caption",
            preview_jpeg="original caption.jpg",
            preview_jpeg_sha256="deadbeef",
        )

        result = sync_from_review([entry], review_dir)

        assert result.deleted == [entry]
        assert result.renamed == []
        assert entry.new_stem == "original caption"

    def test_missing_review_dir_reports_all_ok_entries_deleted(self, tmp_path):
        entry = MappingEntry(
            status="ok",
            original_files=["a.MOV"],
            new_stem="original caption",
            preview_jpeg="original caption.jpg",
            preview_jpeg_sha256="deadbeef",
        )

        result = sync_from_review([entry], tmp_path / "nonexistent-review")

        assert result.deleted == [entry]

    def test_entries_without_recorded_hash_are_ignored(self, tmp_path):
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        entry = MappingEntry(
            status="ok",
            original_files=["a.MOV"],
            new_stem="original caption",
            preview_jpeg="original caption.jpg",
        )

        result = sync_from_review([entry], review_dir)

        assert result.renamed == []
        assert result.deleted == []

    def test_error_entries_are_ignored(self, tmp_path):
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        entry = MappingEntry(status="error", original_files=["a.MOV"], error="boom")

        result = sync_from_review([entry], review_dir)

        assert result.renamed == []
        assert result.deleted == []

    def test_ambiguous_hash_across_two_entries_is_reported_and_left_untouched(
        self, tmp_path
    ):
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        shared_content = b"identical-frame"
        shared_hash = hash_file(write_jpeg(tmp_path / "probe.jpg", shared_content))
        write_jpeg(review_dir / "one.jpg", shared_content)
        entry_a = MappingEntry(
            status="ok",
            original_files=["a.MOV"],
            new_stem="a caption",
            preview_jpeg="a caption.jpg",
            preview_jpeg_sha256=shared_hash,
        )
        entry_b = MappingEntry(
            status="ok",
            original_files=["b.MOV"],
            new_stem="b caption",
            preview_jpeg="b caption.jpg",
            preview_jpeg_sha256=shared_hash,
        )

        result = sync_from_review([entry_a, entry_b], review_dir)

        assert result.ambiguous_hashes == [shared_hash]
        assert result.renamed == []
        assert result.deleted == []
        assert entry_a.new_stem == "a caption"
        assert entry_b.new_stem == "b caption"

    def test_stray_unrelated_jpeg_in_review_dir_is_ignored(self, tmp_path):
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        write_jpeg(review_dir / "unrelated.jpg", b"not tracked by any entry")
        entry = MappingEntry(
            status="ok",
            original_files=["a.MOV"],
            new_stem="original caption",
            preview_jpeg="original caption.jpg",
            preview_jpeg_sha256="deadbeef",
        )

        result = sync_from_review([entry], review_dir)

        assert result.deleted == [entry]

    def test_only_the_renamed_entry_is_touched_in_a_mixed_batch(self, tmp_path):
        """One renamed, one untouched, one deleted, all in the same call --
        each entry's outcome must be independent of the others'."""
        review_dir = tmp_path / "review"
        review_dir.mkdir()

        renamed_content = b"renamed-frame"
        write_jpeg(review_dir / "renamed by human.jpg", renamed_content)
        renamed_entry = MappingEntry(
            status="ok",
            original_files=["a.MOV"],
            new_stem="original a",
            preview_jpeg="original a.jpg",
            preview_jpeg_sha256=hash_file(review_dir / "renamed by human.jpg"),
        )

        untouched_content = b"untouched-frame"
        write_jpeg(review_dir / "original b.jpg", untouched_content)
        untouched_entry = MappingEntry(
            status="ok",
            original_files=["b.MOV"],
            new_stem="original b",
            preview_jpeg="original b.jpg",
            preview_jpeg_sha256=hash_file(review_dir / "original b.jpg"),
        )

        deleted_entry = MappingEntry(
            status="ok",
            original_files=["c.MOV"],
            new_stem="original c",
            preview_jpeg="original c.jpg",
            preview_jpeg_sha256="deadbeef",
        )

        result = sync_from_review(
            [renamed_entry, untouched_entry, deleted_entry], review_dir
        )

        assert result.renamed == [renamed_entry]
        assert renamed_entry.new_stem == "renamed by human"

        assert untouched_entry.new_stem == "original b"

        assert result.deleted == [deleted_entry]
        assert deleted_entry.new_stem == "original c"


class TestReconcileShortCaptionEdits:
    def _reconcile(self, entries, **overrides):
        return reconcile_short_caption_edits(
            entries,
            prefix=overrides.get("prefix", ""),
            suffix=overrides.get("suffix", ""),
            prepend=overrides.get("prepend", False),
            max_file_name_length=overrides.get("max_file_name_length", 255),
        )

    def test_edited_short_caption_rebuilds_new_stem(self):
        entry = MappingEntry(
            status="ok",
            original_files=["a.MOV"],
            new_stem="a original caption",
            short_caption="edited caption",
            long_caption="A long caption.",
            keywords=["a"],
        )
        changed = self._reconcile([entry])
        assert changed == [entry]
        assert entry.new_stem == "a edited caption"

    def test_applies_prefix_suffix_and_prepend(self):
        entry = MappingEntry(
            status="ok",
            original_files=["a.MOV"],
            new_stem="stale",
            short_caption="edited caption",
            long_caption="A long caption.",
            keywords=["a"],
        )
        self._reconcile([entry], prefix="Boston, MA", suffix="TAKE 2", prepend=True)
        assert entry.new_stem == "Boston, MA edited caption a TAKE 2"

    def test_jpeg_renamed_entry_is_skipped(self):
        entry = MappingEntry(
            status="ok",
            original_files=["a.MOV"],
            new_stem="jpeg chosen name",
            short_caption="edited caption",
            long_caption="A long caption.",
            keywords=["a"],
            short_caption_locked=True,
        )
        changed = self._reconcile([entry])
        assert changed == []
        assert entry.new_stem == "jpeg chosen name"

    def test_locked_entry_stays_locked_across_a_run_with_nothing_new_to_sync(self):
        # Regression test: sync_from_review()'s save can persist across a
        # declined/interrupted run, so a *later* run where the JPEG already
        # matches what's stored has nothing new to sync -- but must still
        # remember the name came from a human JPEG rename, not recompute it
        # from short_caption and clobber it. This is exactly the bug
        # reported: short_caption_locked=True must survive being loaded
        # fresh from a mapping file, not just exist in the same process
        # that set it.
        entry = MappingEntry(
            status="ok",
            original_files=["a.MOV"],
            new_stem="jpeg chosen name",
            short_caption="generated caption",
            long_caption="A long caption.",
            keywords=["a"],
            short_caption_locked=True,
        )
        # Round-trip through JSON, as a fresh `--rename-only` invocation
        # would load it, to prove the flag survives serialization.
        entry = MappingEntry.from_dict(entry.to_dict())
        changed = self._reconcile([entry])
        assert changed == []
        assert entry.new_stem == "jpeg chosen name"

    def test_non_add_metadata_entry_is_untouched(self):
        # No long_caption/keywords -- generated without --add-metadata, so
        # short_caption isn't even populated. Direct new_stem edits (the
        # pre-existing workflow) must keep working unmodified.
        entry = MappingEntry(
            status="ok", original_files=["a.MOV"], new_stem="hand-edited new_stem"
        )
        changed = self._reconcile([entry])
        assert changed == []
        assert entry.new_stem == "hand-edited new_stem"

    def test_idempotent_when_short_caption_unedited(self):
        entry = MappingEntry(
            status="ok",
            original_files=["a.MOV"],
            new_stem="a original caption",
            short_caption="original caption",
            long_caption="A long caption.",
            keywords=["a"],
        )
        changed = self._reconcile([entry])
        assert changed == []
        assert entry.new_stem == "a original caption"

    def test_error_entries_are_ignored(self):
        entry = MappingEntry(status="error", original_files=["a.MOV"], error="boom")
        assert self._reconcile([entry]) == []

    def test_missing_short_caption_is_ignored(self):
        # long_caption/keywords present but short_caption absent -- e.g. a
        # metadata_changes.json-style entry, shouldn't happen for
        # MappingEntry in practice but must not crash.
        entry = MappingEntry(
            status="ok",
            original_files=["a.MOV"],
            new_stem="original",
            long_caption="A long caption.",
            keywords=["a"],
        )
        assert self._reconcile([entry]) == []
