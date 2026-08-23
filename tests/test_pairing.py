from pathlib import Path

from slate.pairing import (
    FileGroup,
    _extract_duration_and_frames,
    build_group,
    build_groups,
    discover_input_dir,
    group_by_stem,
    verify_pair,
)


def touch(path: Path, size: int = 0) -> Path:
    path.write_bytes(b"\0" * size)
    return path


class TestDiscoverInputDir:
    def test_filters_to_video_extensions_case_insensitively(self, tmp_path):
        touch(tmp_path / "a.MOV")
        touch(tmp_path / "b.mp4")
        touch(tmp_path / "c.txt")
        touch(tmp_path / "readme.md")
        found = discover_input_dir(tmp_path)
        assert [p.name for p in found] == ["a.MOV", "b.mp4"]

    def test_ignores_subdirectories(self, tmp_path):
        touch(tmp_path / "a.MOV")
        (tmp_path / "subdir").mkdir()
        touch(tmp_path / "subdir" / "b.MOV")
        found = discover_input_dir(tmp_path)
        assert [p.name for p in found] == ["a.MOV"]

    def test_returns_sorted(self, tmp_path):
        touch(tmp_path / "b.MOV")
        touch(tmp_path / "a.MOV")
        found = discover_input_dir(tmp_path)
        assert [p.name for p in found] == ["a.MOV", "b.MOV"]


class TestGroupByStem:
    def test_groups_same_stem_different_extensions(self):
        files = [Path("a.MOV"), Path("a.MP4"), Path("b.MP4")]
        groups = group_by_stem(files)
        assert [sorted(p.name for p in g) for g in groups] == [
            ["a.MOV", "a.MP4"],
            ["b.MP4"],
        ]

    def test_stable_sorted_output_order(self):
        files = [Path("z.MOV"), Path("a.MOV")]
        groups = group_by_stem(files)
        assert [g[0].name for g in groups] == ["a.MOV", "z.MOV"]


class TestExtractDurationAndFrames:
    def test_reads_duration_from_format(self):
        info = {"format": {"duration": "2.5"}, "streams": []}
        assert _extract_duration_and_frames(info) == (2.5, None)

    def test_falls_back_to_stream_duration(self):
        info = {
            "format": {},
            "streams": [{"codec_type": "video", "duration": "3.0", "nb_frames": "30"}],
        }
        assert _extract_duration_and_frames(info) == (3.0, 30)

    def test_ignores_non_video_streams(self):
        info = {
            "format": {},
            "streams": [
                {"codec_type": "audio", "duration": "3.0"},
                {"codec_type": "video", "duration": "2.0", "nb_frames": "20"},
            ],
        }
        assert _extract_duration_and_frames(info) == (2.0, 20)

    def test_missing_fields_return_none(self):
        info = {"format": {}, "streams": [{"codec_type": "video"}]}
        assert _extract_duration_and_frames(info) == (None, None)

    def test_malformed_values_are_ignored(self):
        info = {"format": {"duration": "not-a-number"}, "streams": []}
        assert _extract_duration_and_frames(info) == (None, None)


class TestVerifyPair:
    def test_matching_duration_within_tolerance(self, monkeypatch):
        monkeypatch.setattr(
            "slate.pairing._ffprobe_stream_info",
            lambda p: {
                "format": {"duration": "2.0" if p.name == "a.MOV" else "2.05"},
                "streams": [],
            },
        )
        matches, message = verify_pair(Path("a.MOV"), Path("a.MP4"))
        assert matches is True
        assert message is None

    def test_mismatched_duration_beyond_tolerance(self, monkeypatch):
        monkeypatch.setattr(
            "slate.pairing._ffprobe_stream_info",
            lambda p: {
                "format": {"duration": "2.0" if p.name == "a.MOV" else "6.2"},
                "streams": [],
            },
        )
        matches, message = verify_pair(Path("a.MOV"), Path("a.MP4"))
        assert matches is False
        assert "durations differ by 4.2s" in message

    def test_falls_back_to_frame_count_when_duration_close_call(self, monkeypatch):
        # Duration missing entirely -- fall back to nb_frames comparison.
        def fake_info(p):
            frames = "100" if p.name == "a.MOV" else "101"
            return {
                "format": {},
                "streams": [{"codec_type": "video", "nb_frames": frames}],
            }

        monkeypatch.setattr("slate.pairing._ffprobe_stream_info", fake_info)
        matches, message = verify_pair(Path("a.MOV"), Path("a.MP4"))
        assert matches is True

    def test_mismatched_frame_count_beyond_tolerance(self, monkeypatch):
        def fake_info(p):
            frames = "100" if p.name == "a.MOV" else "110"
            return {
                "format": {},
                "streams": [{"codec_type": "video", "nb_frames": frames}],
            }

        monkeypatch.setattr("slate.pairing._ffprobe_stream_info", fake_info)
        matches, message = verify_pair(Path("a.MOV"), Path("a.MP4"))
        assert matches is False
        assert "frame counts differ" in message

    def test_unreadable_file_falls_back_to_trusting_filename_match(self, monkeypatch):
        monkeypatch.setattr(
            "slate.pairing._ffprobe_stream_info",
            lambda p: (
                None
                if p.name == "a.MOV"
                else {"format": {"duration": "2.0"}, "streams": []}
            ),
        )
        matches, message = verify_pair(Path("a.MOV"), Path("a.MP4"))
        assert matches is True
        assert "trusting filename match" in message

    def test_no_duration_or_frame_data_falls_back_to_trusting_filename_match(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            "slate.pairing._ffprobe_stream_info",
            lambda p: {"format": {}, "streams": []},
        )
        matches, message = verify_pair(Path("a.MOV"), Path("a.MP4"))
        assert matches is True
        assert "trusting filename match" in message


class TestBuildGroup:
    def test_single_file_is_ok_with_itself_as_source(self, tmp_path):
        f = touch(tmp_path / "a.MOV")
        group = build_group([f])
        assert group.status == "ok"
        assert group.source_file == f

    def test_verified_pair_selects_smaller_file_as_source(self, tmp_path, monkeypatch):
        monkeypatch.setattr("slate.pairing.verify_pair", lambda a, b: (True, None))
        small = touch(tmp_path / "a.MP4", size=10)
        large = touch(tmp_path / "a.MOV", size=1000)
        group = build_group([large, small])
        assert group.status == "ok"
        assert group.source_file == small

    def test_mismatched_pair_is_an_error_group(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "slate.pairing.verify_pair", lambda a, b: (False, "durations differ")
        )
        a = touch(tmp_path / "a.MOV")
        b = touch(tmp_path / "a.MP4")
        group = build_group([a, b])
        assert group.status == "error"
        assert group.error == "durations differ"
        assert group.source_file is None

    def test_more_than_two_files_sharing_stem_is_an_error(self, tmp_path):
        files = [touch(tmp_path / n) for n in ("a.MOV", "a.MP4", "a.WAV")]
        group = build_group(files)
        assert group.status == "error"
        assert "expected 1 or 2" in group.error


class TestBuildGroups:
    def test_builds_one_group_per_stem(self, tmp_path, monkeypatch):
        monkeypatch.setattr("slate.pairing.verify_pair", lambda a, b: (True, None))
        touch(tmp_path / "a.MOV")
        touch(tmp_path / "a.MP4")
        touch(tmp_path / "b.MP4")
        groups = build_groups(discover_input_dir(tmp_path))
        assert len(groups) == 2
        assert all(isinstance(g, FileGroup) for g in groups)
