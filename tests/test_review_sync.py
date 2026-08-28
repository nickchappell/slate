from slate.mappings import MappingEntry
from slate.review_sync import hash_file, sync_from_review


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
            preview_jpeg="review/original caption.jpg",
            preview_jpeg_sha256=hash_file(review_dir / "renamed by human.jpg"),
        )

        result = sync_from_review([entry], review_dir)

        assert result.renamed == [entry]
        assert entry.new_stem == "renamed by human"
        assert entry.preview_jpeg == "review/renamed by human.jpg"
        assert result.deleted == []

    def test_untouched_jpeg_is_a_no_op(self, tmp_path):
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        write_jpeg(review_dir / "original caption.jpg", b"frame-bytes")
        entry = MappingEntry(
            status="ok",
            original_files=["a.MOV"],
            new_stem="original caption",
            preview_jpeg="review/original caption.jpg",
            preview_jpeg_sha256=hash_file(review_dir / "original caption.jpg"),
        )

        result = sync_from_review([entry], review_dir)

        assert result.renamed == []
        assert result.deleted == []
        assert entry.new_stem == "original caption"

    def test_missing_jpeg_is_reported_deleted(self, tmp_path):
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        entry = MappingEntry(
            status="ok",
            original_files=["a.MOV"],
            new_stem="original caption",
            preview_jpeg="review/original caption.jpg",
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
            preview_jpeg="review/original caption.jpg",
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
            preview_jpeg="review/original caption.jpg",
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
            preview_jpeg="review/a caption.jpg",
            preview_jpeg_sha256=shared_hash,
        )
        entry_b = MappingEntry(
            status="ok",
            original_files=["b.MOV"],
            new_stem="b caption",
            preview_jpeg="review/b caption.jpg",
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
            preview_jpeg="review/original caption.jpg",
            preview_jpeg_sha256="deadbeef",
        )

        result = sync_from_review([entry], review_dir)

        assert result.deleted == [entry]
