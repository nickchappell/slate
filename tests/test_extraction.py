import subprocess
from pathlib import Path

import pytest

from slate.extraction import ExtractionError, extract_frame


class FakeCompletedProcess:
    def __init__(self, returncode: int):
        self.returncode = returncode


def make_fake_run(*, ffmpeg_ok: bool, qlmanage_ok: bool, sips_ok: bool = True):
    """Builds a subprocess.run replacement that fakes ffmpeg/qlmanage/sips
    based on argv[0], writing whatever output file each real tool would have
    produced -- so extraction.py's is_file()/size checks still pass, with no
    real ffmpeg/qlmanage/media involved."""

    def fake_run(cmd, capture_output=True, timeout=None, **kwargs):
        tool = cmd[0]

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


class TestExtractFrame:
    def test_ffmpeg_success_produces_output(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run", make_fake_run(ffmpeg_ok=True, qlmanage_ok=True)
        )
        output = tmp_path / "frame.jpg"
        extract_frame(tmp_path / "source.MP4", output)
        assert output.is_file()
        assert output.stat().st_size > 0

    def test_falls_back_to_qlmanage_when_ffmpeg_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run", make_fake_run(ffmpeg_ok=False, qlmanage_ok=True)
        )
        output = tmp_path / "frame.jpg"
        extract_frame(tmp_path / "source.MOV", output)
        assert output.is_file()
        assert output.stat().st_size > 0

    def test_raises_when_both_ffmpeg_and_qlmanage_fail(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run", make_fake_run(ffmpeg_ok=False, qlmanage_ok=False)
        )
        with pytest.raises(ExtractionError, match="source.MOV"):
            extract_frame(tmp_path / "source.MOV", tmp_path / "frame.jpg")

    def test_raises_when_qlmanage_succeeds_but_sips_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            make_fake_run(ffmpeg_ok=False, qlmanage_ok=True, sips_ok=False),
        )
        with pytest.raises(ExtractionError):
            extract_frame(tmp_path / "source.MOV", tmp_path / "frame.jpg")

    def test_creates_parent_directory_for_output(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run", make_fake_run(ffmpeg_ok=True, qlmanage_ok=True)
        )
        output = tmp_path / "nested" / "dir" / "frame.jpg"
        extract_frame(tmp_path / "source.MP4", output)
        assert output.is_file()

    def test_subprocess_exception_treated_as_failure_not_propagated(
        self, tmp_path, monkeypatch
    ):
        def raising_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=60)

        monkeypatch.setattr(subprocess, "run", raising_run)
        with pytest.raises(ExtractionError):
            extract_frame(tmp_path / "source.MOV", tmp_path / "frame.jpg")
