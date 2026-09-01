import subprocess
from pathlib import Path

import pytest

from slate.extraction import (
    ExtractionError,
    _frame_position_fractions,
    build_montage,
    extract_frames,
)


class FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = ""):
        self.returncode = returncode
        self.stdout = stdout


def make_fake_run(
    *,
    ffmpeg_ok: bool,
    qlmanage_ok: bool,
    sips_ok: bool = True,
    duration: float | None = 10.0,
):
    """Builds a subprocess.run replacement that fakes ffprobe/ffmpeg/qlmanage/
    sips based on argv[0], writing whatever output file each real tool would
    have produced -- so extraction.py's is_file()/size checks still pass,
    with no real ffmpeg/qlmanage/media involved."""

    def fake_run(cmd, capture_output=True, timeout=None, text=False, **kwargs):
        tool = cmd[0]

        if tool == "ffprobe":
            if duration is None:
                return FakeCompletedProcess(1)
            return FakeCompletedProcess(0, stdout=str(duration))

        if tool == "ffmpeg":
            output_path = Path(cmd[-1])
            if ffmpeg_ok:
                output_path.write_bytes(b"fake jpeg bytes")
                return FakeCompletedProcess(0)
            return FakeCompletedProcess(1)

        if tool == "qlmanage":
            # -o <dir> <source>
            out_dir = Path(cmd[cmd.index("-o") + 1])
            source = Path(cmd[-1])
            if qlmanage_ok:
                (out_dir / f"{source.name}.png").write_bytes(b"fake png bytes")
                return FakeCompletedProcess(0)
            return FakeCompletedProcess(1)

        if tool == "sips":
            output_path = Path(cmd[-1])
            if sips_ok:
                output_path.write_bytes(b"fake jpeg from png")
                return FakeCompletedProcess(0)
            return FakeCompletedProcess(1)

        raise AssertionError(f"unexpected command: {cmd}")

    return fake_run


class TestFramePositionFractions:
    def test_default_three_frames_are_5_50_90_percent(self):
        assert _frame_position_fractions(3) == [0.05, 0.5, 0.9]

    def test_single_frame_is_the_min_position(self):
        assert _frame_position_fractions(1) == [0.05]

    def test_two_frames_are_min_and_max_positions(self):
        assert _frame_position_fractions(2) == [0.05, 0.9]

    def test_five_frames_evenly_spaced_with_endpoints_clamped(self):
        assert _frame_position_fractions(5) == [0.05, 0.25, 0.5, 0.75, 0.9]


class TestExtractFrames:
    def test_ffmpeg_success_produces_requested_frame_count(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run", make_fake_run(ffmpeg_ok=True, qlmanage_ok=True)
        )
        frames = extract_frames(tmp_path / "source.MP4", tmp_path / "out", 3)
        assert len(frames) == 3
        assert all(f.is_file() and f.stat().st_size > 0 for f in frames)

    def test_num_frames_one_produces_one_frame(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run", make_fake_run(ffmpeg_ok=True, qlmanage_ok=True)
        )
        frames = extract_frames(tmp_path / "source.MP4", tmp_path / "out", 1)
        assert len(frames) == 1

    def test_falls_back_to_single_qlmanage_frame_when_ffmpeg_fails(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            subprocess, "run", make_fake_run(ffmpeg_ok=False, qlmanage_ok=True)
        )
        frames = extract_frames(tmp_path / "source.MOV", tmp_path / "out", 3)
        assert len(frames) == 1
        assert frames[0].is_file()

    def test_falls_back_to_qlmanage_when_duration_unreadable(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            subprocess,
            "run",
            make_fake_run(ffmpeg_ok=True, qlmanage_ok=True, duration=None),
        )
        frames = extract_frames(tmp_path / "source.MOV", tmp_path / "out", 3)
        assert len(frames) == 1

    def test_raises_when_both_ffmpeg_and_qlmanage_fail(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run", make_fake_run(ffmpeg_ok=False, qlmanage_ok=False)
        )
        with pytest.raises(ExtractionError, match="source.MOV"):
            extract_frames(tmp_path / "source.MOV", tmp_path / "out", 3)

    def test_raises_when_qlmanage_succeeds_but_sips_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            make_fake_run(ffmpeg_ok=False, qlmanage_ok=True, sips_ok=False),
        )
        with pytest.raises(ExtractionError):
            extract_frames(tmp_path / "source.MOV", tmp_path / "out", 3)

    def test_creates_output_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run", make_fake_run(ffmpeg_ok=True, qlmanage_ok=True)
        )
        output_dir = tmp_path / "nested" / "dir"
        frames = extract_frames(tmp_path / "source.MP4", output_dir, 3)
        assert all(f.parent == output_dir for f in frames)

    def test_subprocess_exception_treated_as_failure_not_propagated(
        self, tmp_path, monkeypatch
    ):
        def raising_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=60)

        monkeypatch.setattr(subprocess, "run", raising_run)
        with pytest.raises(ExtractionError):
            extract_frames(tmp_path / "source.MOV", tmp_path / "out", 3)


class TestBuildMontage:
    def test_single_frame_is_copied_verbatim(self, tmp_path):
        frame = tmp_path / "frame_0.jpg"
        frame.write_bytes(b"only frame")
        output_jpeg = tmp_path / "preview.jpg"

        build_montage([frame], output_jpeg)

        assert output_jpeg.read_bytes() == b"only frame"

    def test_multiple_frames_invoke_ffmpeg_hstack(self, tmp_path, monkeypatch):
        frames = [tmp_path / f"frame_{i}.jpg" for i in range(3)]
        for f in frames:
            f.write_bytes(b"x")
        output_jpeg = tmp_path / "preview.jpg"

        calls = []

        def fake_run(cmd, capture_output=True, timeout=None, **kwargs):
            calls.append(cmd)
            Path(cmd[-1]).write_bytes(b"montage bytes")
            return FakeCompletedProcess(0)

        monkeypatch.setattr(subprocess, "run", fake_run)

        build_montage(frames, output_jpeg)

        assert output_jpeg.read_bytes() == b"montage bytes"
        assert len(calls) == 1
        assert "hstack=inputs=3" in calls[0]

    def test_raises_when_ffmpeg_fails(self, tmp_path, monkeypatch):
        frames = [tmp_path / f"frame_{i}.jpg" for i in range(2)]
        for f in frames:
            f.write_bytes(b"x")

        monkeypatch.setattr(
            subprocess, "run", lambda cmd, **kwargs: FakeCompletedProcess(1)
        )

        with pytest.raises(ExtractionError):
            build_montage(frames, tmp_path / "preview.jpg")

    def test_creates_output_parent_directory(self, tmp_path):
        frame = tmp_path / "frame_0.jpg"
        frame.write_bytes(b"only frame")
        output_jpeg = tmp_path / "nested" / "dir" / "preview.jpg"

        build_montage([frame], output_jpeg)

        assert output_jpeg.is_file()
