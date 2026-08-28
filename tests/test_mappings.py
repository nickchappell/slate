from slate.mappings import (
    MappingEntry,
    disambiguate,
    find_existing_match,
    load_mappings,
    save_mappings,
    sort_key,
)


class TestMappingEntryDictConversion:
    def test_ok_entry_round_trip(self):
        entry = MappingEntry(
            status="ok",
            original_files=["a.MOV", "a.MP4"],
            new_stem="a caption",
            preview_jpeg="review/a caption.jpg",
            preview_jpeg_sha256="deadbeef",
            source_used_for_caption="a.MP4",
        )
        assert MappingEntry.from_dict(entry.to_dict()) == entry

    def test_error_entry_round_trip(self):
        entry = MappingEntry(
            status="error", original_files=["b.MOV"], error="decode failed"
        )
        assert MappingEntry.from_dict(entry.to_dict()) == entry

    def test_ok_entry_dict_omits_error_key(self):
        entry = MappingEntry(status="ok", original_files=["a.MOV"], new_stem="a")
        assert "error" not in entry.to_dict()

    def test_error_entry_dict_omits_ok_only_keys(self):
        entry = MappingEntry(status="error", original_files=["a.MOV"], error="boom")
        d = entry.to_dict()
        assert "new_stem" not in d
        assert "preview_jpeg" not in d
        assert "source_used_for_caption" not in d


class TestLoadSaveMappings:
    def test_missing_file_returns_empty_list(self, tmp_path):
        assert load_mappings(tmp_path / "nonexistent.json") == []

    def test_round_trip(self, tmp_path):
        path = tmp_path / "mappings.json"
        entries = [
            MappingEntry(
                status="ok", original_files=["a.MOV", "a.MP4"], new_stem="a caption"
            ),
            MappingEntry(status="error", original_files=["b.MOV"], error="boom"),
        ]
        save_mappings(path, entries)
        assert load_mappings(path) == entries


class TestFindExistingMatch:
    def test_matches_regardless_of_order(self):
        existing = [
            MappingEntry(status="ok", original_files=["a.MOV", "a.MP4"], new_stem="x")
        ]
        assert find_existing_match(existing, ["a.MP4", "a.MOV"]) is not None

    def test_matches_regardless_of_status(self):
        existing = [
            MappingEntry(status="error", original_files=["a.MOV"], error="boom")
        ]
        assert find_existing_match(existing, ["a.MOV"]) is not None

    def test_no_match_returns_none(self):
        existing = [MappingEntry(status="ok", original_files=["a.MOV"], new_stem="x")]
        assert find_existing_match(existing, ["b.MOV"]) is None

    def test_partial_overlap_is_not_a_match(self):
        existing = [
            MappingEntry(status="ok", original_files=["a.MOV", "a.MP4"], new_stem="x")
        ]
        assert find_existing_match(existing, ["a.MOV"]) is None


class TestSortKey:
    def test_uses_smallest_original_file_name(self):
        entry = MappingEntry(
            status="ok", original_files=["b.MP4", "a.MOV"], new_stem="x"
        )
        assert sort_key(entry) == "a.MOV"


class TestDisambiguate:
    def test_no_collision_leaves_stems_unchanged(self):
        e1 = MappingEntry(status="ok", original_files=["a.MOV"], new_stem="caption one")
        e2 = MappingEntry(status="ok", original_files=["b.MOV"], new_stem="caption two")
        disambiguated = disambiguate([e1, e2])
        assert disambiguated == []
        assert e1.new_stem == "caption one"
        assert e2.new_stem == "caption two"

    def test_two_way_collision_appends_suffix_to_second_in_sorted_order(self):
        e1 = MappingEntry(
            status="ok", original_files=["b.MOV"], new_stem="same caption"
        )
        e2 = MappingEntry(
            status="ok", original_files=["a.MOV"], new_stem="same caption"
        )
        disambiguated = disambiguate([e1, e2])
        # a.MOV sorts before b.MOV, so a.MOV keeps the unsuffixed name
        assert e2.new_stem == "same caption"
        assert e1.new_stem == "same caption_2"
        assert disambiguated == [e1]

    def test_three_way_collision_increments_suffix(self):
        e1 = MappingEntry(
            status="ok", original_files=["a.MOV"], new_stem="same caption"
        )
        e2 = MappingEntry(
            status="ok", original_files=["b.MOV"], new_stem="same caption"
        )
        e3 = MappingEntry(
            status="ok", original_files=["c.MOV"], new_stem="same caption"
        )
        disambiguate([e1, e2, e3])
        assert [e1.new_stem, e2.new_stem, e3.new_stem] == [
            "same caption",
            "same caption_2",
            "same caption_3",
        ]

    def test_only_compares_ok_groups(self):
        e1 = MappingEntry(
            status="ok", original_files=["a.MOV"], new_stem="same caption"
        )
        e2 = MappingEntry(status="error", original_files=["b.MOV"], error="boom")
        disambiguated = disambiguate([e1, e2])
        assert disambiguated == []
        assert e1.new_stem == "same caption"

    def test_pair_shares_disambiguated_stem_across_both_files(self):
        e1 = MappingEntry(
            status="ok", original_files=["b.MOV", "b.MP4"], new_stem="same caption"
        )
        e2 = MappingEntry(
            status="ok", original_files=["a.MOV", "a.MP4"], new_stem="same caption"
        )
        disambiguate([e1, e2])
        assert e1.new_stem == "same caption_2"

    def test_different_extensions_do_not_falsely_collide(self):
        # Final-name comparison is new_stem + extension, so identical
        # new_stem values with different extensions are not a real collision.
        e1 = MappingEntry(
            status="ok", original_files=["a.MOV"], new_stem="same caption"
        )
        e2 = MappingEntry(
            status="ok", original_files=["a.WAV"], new_stem="same caption"
        )
        disambiguated = disambiguate([e1, e2])
        assert disambiguated == []

    def test_suffix_pushes_over_max_length_re_truncates_base(self):
        e1 = MappingEntry(status="ok", original_files=["b.MOV"], new_stem="x" * 20)
        e2 = MappingEntry(status="ok", original_files=["a.MOV"], new_stem="x" * 20)
        disambiguate([e1, e2], max_file_name_length=22)
        # base(20 chars) + "_2" (2 chars) = 22 chars, but max_length=22 means
        # the limit is 21 (one under) -- base must shrink by 1, suffix intact.
        assert e1.new_stem.endswith("_2")
        assert len(e1.new_stem) == 21
