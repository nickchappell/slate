import json
import struct
import subprocess
from pathlib import Path

from slate.rotate import (
    NEEDS_FIX_LABEL,
    RotateResult,
    RotationState,
    read_rotation_state,
    rotate_file,
    rotate_to_identity,
    run_rotate,
    write_undo_script,
)

_IDENTITY = (65536, 0, 0, 0, 65536, 0, 0, 0, 1073741824)
_ROTATE_90_CW = (0, 65536, 0, -65536, 0, 0, 0, 0, 1073741824)


class FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = ""):
        self.returncode = returncode
        self.stdout = stdout


def _tkhd_bytes(matrix: tuple[int, ...], version: int = 0) -> bytes:
    """Builds a real, correctly-sized tkhd atom (header + payload) around
    the given matrix -- version 0/1 layouts per ISO/IEC 14496-12, matching
    rotate.py's own _tkhd_matrix_offset(). Every non-matrix field is a
    placeholder; only version/matrix are ever read by the code under test."""
    if version == 0:
        times = struct.pack(">5I", 0, 0, 1, 0, 1000)
    elif version == 1:
        times = struct.pack(">QQIIQ", 0, 0, 1, 0, 1000)
    else:
        raise ValueError(version)

    body = (
        times
        + b"\x00" * 8  # reserved
        + struct.pack(">hh", 0, 0)  # layer, alternate_group
        + struct.pack(">hh", 0x0100, 0)  # volume, reserved
        + struct.pack(">9i", *matrix)
        + struct.pack(">II", 0, 0)  # width, height (placeholders)
    )
    payload = struct.pack(">B", version) + b"\x00\x00\x00" + body  # version+flags
    size = 8 + len(payload)
    return struct.pack(">I4s", size, b"tkhd") + payload


def _moov_json(handler_types: list[str]) -> str:
    """One `trak` per handler_type, in order -- mirrors mp4dump --format
    json --verbosity 1's shape closely enough for _find_video_trak_index()."""
    return json.dumps(
        [
            {
                "name": "moov",
                "children": [
                    {
                        "name": "trak",
                        "children": [
                            {
                                "name": "mdia",
                                "children": [{"name": "hdlr", "handler_type": ht}],
                            }
                        ],
                    }
                    for ht in handler_types
                ],
            }
        ]
    )


def make_fake_run(
    *,
    handler_types: list[str] | None = None,
    tkhd_by_trak: dict[int, bytes] | None = None,
    mp4dump_ok: bool = True,
    mp4extract_ok: bool = True,
    mp4edit_ok: bool = True,
    on_mp4edit_replace: bytes | None = None,
):
    """Fakes Bento4's mp4dump/mp4extract/mp4edit based on argv[0], mirroring
    test_extraction.py's/test_metadata.py's argv-dispatch convention.
    tkhd_by_trak maps a trak index (parsed out of the "moov/trak[N]/tkhd"
    atom path) to the raw tkhd bytes mp4extract should hand back for it.
    on_mp4edit_replace, if given, is written to mp4edit's output path
    instead of a generic placeholder -- lets a test assert the *patched*
    atom (the one mp4edit was actually handed) decoded to identity."""
    handler_types = handler_types if handler_types is not None else ["vide"]
    tkhd_by_trak = tkhd_by_trak if tkhd_by_trak is not None else {}
    calls: list[list[str]] = []
    # Captured at call time, before rotate_to_identity's `finally` unlinks
    # its atom temp file -- reading it back afterward (from the test) would
    # otherwise always 404.
    mp4edit_atom_sources: list[bytes] = []

    def fake_run(cmd, capture_output=True, timeout=None, text=False, **kwargs):
        calls.append(cmd)
        tool = cmd[0]

        if tool == "mp4dump":
            if not mp4dump_ok:
                return FakeCompletedProcess(1)
            return FakeCompletedProcess(0, stdout=_moov_json(handler_types))

        if tool == "mp4extract":
            if not mp4extract_ok:
                return FakeCompletedProcess(1)
            atom_path, _in_path, out_path = cmd[1], cmd[2], Path(cmd[3])
            trak_index = int(atom_path.split("trak[")[1].split("]")[0])
            out_path.write_bytes(tkhd_by_trak.get(trak_index, _tkhd_bytes(_IDENTITY)))
            return FakeCompletedProcess(0)

        if tool == "mp4edit":
            if not mp4edit_ok:
                return FakeCompletedProcess(1)
            replace_arg = cmd[2]  # "--replace" is cmd[1]
            mp4edit_atom_sources.append(Path(replace_arg.split(":", 1)[1]).read_bytes())
            out_path = Path(cmd[-1])
            if on_mp4edit_replace is not None:
                out_path.write_bytes(on_mp4edit_replace)
            else:
                out_path.write_bytes(b"rotated file bytes")
            return FakeCompletedProcess(0)

        raise AssertionError(f"unexpected command: {cmd}")

    return fake_run, calls, mp4edit_atom_sources


class TestReadRotationState:
    def test_needs_fix_matrix_is_labeled_minus_90(self, tmp_path, monkeypatch):
        fake_run, _, _ = make_fake_run(tkhd_by_trak={0: _tkhd_bytes(_ROTATE_90_CW)})
        monkeypatch.setattr(subprocess, "run", fake_run)

        state = read_rotation_state(tmp_path / "clip.mov")

        assert state == RotationState(
            trak_index=0, matrix=_ROTATE_90_CW, label=NEEDS_FIX_LABEL
        )

    def test_identity_matrix_is_labeled_none(self, tmp_path, monkeypatch):
        fake_run, _, _ = make_fake_run(tkhd_by_trak={0: _tkhd_bytes(_IDENTITY)})
        monkeypatch.setattr(subprocess, "run", fake_run)

        assert read_rotation_state(tmp_path / "clip.mov").label == "none"

    def test_unrecognized_matrix_is_labeled_other(self, tmp_path, monkeypatch):
        odd_matrix = (1, 2, 3, 4, 5, 6, 7, 8, 9)
        fake_run, _, _ = make_fake_run(tkhd_by_trak={0: _tkhd_bytes(odd_matrix)})
        monkeypatch.setattr(subprocess, "run", fake_run)

        assert read_rotation_state(tmp_path / "clip.mov").label == "other"

    def test_picks_the_video_track_when_not_first(self, tmp_path, monkeypatch):
        fake_run, calls, _ = make_fake_run(
            handler_types=["soun", "vide"],
            tkhd_by_trak={1: _tkhd_bytes(_ROTATE_90_CW)},
        )
        monkeypatch.setattr(subprocess, "run", fake_run)

        state = read_rotation_state(tmp_path / "clip.mov")

        assert state.trak_index == 1
        extract_calls = [c for c in calls if c[0] == "mp4extract"]
        assert extract_calls[0][1] == "moov/trak[1]/tkhd"

    def test_no_video_track_returns_none(self, tmp_path, monkeypatch):
        fake_run, _, _ = make_fake_run(handler_types=["soun"])
        monkeypatch.setattr(subprocess, "run", fake_run)

        assert read_rotation_state(tmp_path / "clip.mov") is None

    def test_bento4_not_installed_returns_none(self, tmp_path, monkeypatch):
        fake_run, _, _ = make_fake_run(mp4dump_ok=False)
        monkeypatch.setattr(subprocess, "run", fake_run)

        assert read_rotation_state(tmp_path / "clip.mov") is None

    def test_mp4extract_failure_returns_none(self, tmp_path, monkeypatch):
        fake_run, _, _ = make_fake_run(mp4extract_ok=False)
        monkeypatch.setattr(subprocess, "run", fake_run)

        assert read_rotation_state(tmp_path / "clip.mov") is None

    def test_unrecognized_tkhd_version_returns_none(self, tmp_path, monkeypatch):
        # version 2 doesn't exist in the ISO spec -- _tkhd_matrix_offset()
        # returns None for anything but 0/1.
        bogus = bytearray(_tkhd_bytes(_ROTATE_90_CW))
        bogus[8] = 2  # version byte, right after the 8-byte size+type header
        fake_run, _, _ = make_fake_run(tkhd_by_trak={0: bytes(bogus)})
        monkeypatch.setattr(subprocess, "run", fake_run)

        assert read_rotation_state(tmp_path / "clip.mov") is None

    def test_version_1_tkhd_is_parsed(self, tmp_path, monkeypatch):
        fake_run, _, _ = make_fake_run(
            tkhd_by_trak={0: _tkhd_bytes(_ROTATE_90_CW, version=1)}
        )
        monkeypatch.setattr(subprocess, "run", fake_run)

        assert read_rotation_state(tmp_path / "clip.mov").label == NEEDS_FIX_LABEL


class TestRotateToIdentity:
    def test_patches_matrix_to_identity_before_calling_mp4edit(
        self, tmp_path, monkeypatch
    ):
        # on_mp4edit_replace left at its default (a generic byte string) --
        # this test's real assertion is on the *source* atom file mp4edit
        # was handed (captured by the fake before rotate_to_identity's
        # `finally` unlinks it), not mp4edit's own output.
        fake_run, _calls, atom_sources = make_fake_run(
            tkhd_by_trak={0: _tkhd_bytes(_ROTATE_90_CW)}
        )
        monkeypatch.setattr(subprocess, "run", fake_run)
        output_path = tmp_path / "out.mov"

        ok = rotate_to_identity(tmp_path / "clip.mov", 0, output_path)

        assert ok
        assert output_path.is_file()
        assert len(atom_sources) == 1
        patched = atom_sources[0]
        matrix = struct.unpack(">9i", patched[48 : 48 + 36])
        assert matrix == _IDENTITY

    def test_mp4extract_failure_returns_false(self, tmp_path, monkeypatch):
        fake_run, _, _ = make_fake_run(mp4extract_ok=False)
        monkeypatch.setattr(subprocess, "run", fake_run)

        assert not rotate_to_identity(tmp_path / "clip.mov", 0, tmp_path / "out.mov")

    def test_mp4edit_failure_returns_false(self, tmp_path, monkeypatch):
        fake_run, _, _ = make_fake_run(
            tkhd_by_trak={0: _tkhd_bytes(_ROTATE_90_CW)}, mp4edit_ok=False
        )
        monkeypatch.setattr(subprocess, "run", fake_run)

        assert not rotate_to_identity(tmp_path / "clip.mov", 0, tmp_path / "out.mov")


class TestRotateFile:
    def test_missing_file_is_an_error(self, tmp_path):
        result = rotate_file(tmp_path / "missing.mov", dry_run=False)
        assert result.status == "error"
        assert "not found" in result.message

    def test_undeterminable_rotation_is_an_error(self, tmp_path, monkeypatch):
        fake_run, _, _ = make_fake_run(mp4dump_ok=False)
        monkeypatch.setattr(subprocess, "run", fake_run)
        clip = tmp_path / "clip.mov"
        clip.write_bytes(b"x")

        result = rotate_file(clip, dry_run=False)

        assert result.status == "error"
        assert "Bento4" in result.message

    def test_wrong_rotation_state_dry_run_is_skipped_not_errored(
        self, tmp_path, monkeypatch
    ):
        fake_run, _, _ = make_fake_run(tkhd_by_trak={0: _tkhd_bytes(_IDENTITY)})
        monkeypatch.setattr(subprocess, "run", fake_run)
        clip = tmp_path / "clip.mov"
        clip.write_bytes(b"x")

        result = rotate_file(clip, dry_run=True)

        assert result.status == "skipped"
        assert clip.read_bytes() == b"x"  # untouched

    def test_wrong_rotation_state_apply_is_an_error(self, tmp_path, monkeypatch):
        fake_run, _, _ = make_fake_run(tkhd_by_trak={0: _tkhd_bytes(_IDENTITY)})
        monkeypatch.setattr(subprocess, "run", fake_run)
        clip = tmp_path / "clip.mov"
        clip.write_bytes(b"x")

        result = rotate_file(clip, dry_run=False)

        assert result.status == "error"
        assert clip.read_bytes() == b"x"  # untouched

    def test_needs_fix_dry_run_reports_without_writing(self, tmp_path, monkeypatch):
        fake_run, _, _ = make_fake_run(tkhd_by_trak={0: _tkhd_bytes(_ROTATE_90_CW)})
        monkeypatch.setattr(subprocess, "run", fake_run)
        clip = tmp_path / "clip.mov"
        clip.write_bytes(b"original bytes")

        result = rotate_file(clip, dry_run=True)

        assert result.status == "would_rotate"
        assert clip.read_bytes() == b"original bytes"
        assert not (tmp_path / "original_unrotated").exists()

    def test_needs_fix_apply_moves_original_and_replaces_it(
        self, tmp_path, monkeypatch
    ):
        # The fake's mp4extract always hands back the -90 tkhd regardless of
        # which file it's reading from -- rotate_file's post-patch
        # verification read_rotation_state(tmp_output) would therefore also
        # see -90 unless mp4edit's output is specifically the identity-patch
        # result. on_mp4edit_replace supplies a real, minimal but *valid*
        # container (just the moov/tkhd bytes mp4dump/mp4extract need) so
        # that verification call reads identity, matching what the real
        # tool actually produces end to end.
        verified_container = _tkhd_bytes(_IDENTITY)
        fake_run, calls, _ = make_fake_run(
            tkhd_by_trak={0: _tkhd_bytes(_ROTATE_90_CW)},
            on_mp4edit_replace=verified_container,
        )

        # The verification read (read_rotation_state on tmp_output) needs
        # its own mp4extract call to see identity, not -90 -- swap the fake
        # after mp4edit's write is captured, using a second layer keyed on
        # whether mp4edit has already run in this test's one rotate_file()
        # call.
        state = {"edited": False}
        orig_fake_run = fake_run

        def fake_run_wrapper(cmd, **kwargs):
            if cmd[0] == "mp4edit":
                state["edited"] = True
            if cmd[0] == "mp4extract" and state["edited"]:
                out_path = Path(cmd[3])
                out_path.write_bytes(_tkhd_bytes(_IDENTITY))
                return FakeCompletedProcess(0)
            return orig_fake_run(cmd, **kwargs)

        monkeypatch.setattr(subprocess, "run", fake_run_wrapper)

        clip = tmp_path / "clip.mov"
        clip.write_bytes(b"original bytes")

        result = rotate_file(clip, dry_run=False)

        assert result.status == "rotated"
        assert clip.read_bytes() == verified_container
        preserved = tmp_path / "original_unrotated" / "clip.mov"
        assert preserved.is_file()
        assert preserved.read_bytes() == b"original bytes"
        assert [c[0] for c in calls].count("mp4edit") == 1

    def test_refuses_to_overwrite_an_existing_preserved_original(
        self, tmp_path, monkeypatch
    ):
        fake_run, _, _ = make_fake_run(tkhd_by_trak={0: _tkhd_bytes(_ROTATE_90_CW)})
        monkeypatch.setattr(subprocess, "run", fake_run)
        clip = tmp_path / "clip.mov"
        clip.write_bytes(b"current bytes")
        preserved_dir = tmp_path / "original_unrotated"
        preserved_dir.mkdir()
        (preserved_dir / "clip.mov").write_bytes(b"already preserved")

        result = rotate_file(clip, dry_run=False)

        assert result.status == "error"
        assert "already exists" in result.message
        assert clip.read_bytes() == b"current bytes"  # untouched
        assert (preserved_dir / "clip.mov").read_bytes() == b"already preserved"

    def test_verification_failure_leaves_original_untouched(
        self, tmp_path, monkeypatch
    ):
        # mp4edit reports success but writes bytes that don't actually
        # decode to identity -- rotate_file's own re-read must catch this
        # rather than trusting mp4edit's exit code alone.
        fake_run, _, _ = make_fake_run(
            tkhd_by_trak={0: _tkhd_bytes(_ROTATE_90_CW)},
            on_mp4edit_replace=_tkhd_bytes(_ROTATE_90_CW),
        )
        monkeypatch.setattr(subprocess, "run", fake_run)
        clip = tmp_path / "clip.mov"
        clip.write_bytes(b"original bytes")

        result = rotate_file(clip, dry_run=False)

        assert result.status == "error"
        assert "verification" in result.message
        assert clip.read_bytes() == b"original bytes"
        assert not (tmp_path / "original_unrotated").exists()


class TestRunRotate:
    def test_dispatches_output_per_status_and_tallies_summary(
        self, tmp_path, monkeypatch, capsys
    ):
        # fake_rotate_file must respect `dry_run` (unlike a fixed
        # canned-per-file-name response) -- run_rotate's own preview pass
        # always calls with dry_run=True first, then re-calls with
        # dry_run=False only for whichever files that preview marked
        # would_rotate.
        def fake_rotate_file(path, *, dry_run):
            if path.name == "rotated.mov":
                status = "would_rotate" if dry_run else "rotated"
                return RotateResult(path=path, status=status, message="ok")
            if path.name == "skip.mov":
                return RotateResult(path=path, status="skipped", message="not it")
            return RotateResult(path=path, status="error", message="boom")

        monkeypatch.setattr("slate.rotate.rotate_file", fake_rotate_file)
        # This test's fake "rotated" result carries no preserved_path, so a
        # real write_undo_script() call would produce an empty (but real)
        # script -- isolate cwd so that never lands in the repo root.
        monkeypatch.chdir(tmp_path)

        files = [
            tmp_path / "rotated.mov",
            tmp_path / "skip.mov",
            tmp_path / "broken.mov",
        ]
        results = run_rotate(files, dry_run=False, assume_yes=True)

        assert [r.status for r in results] == ["rotated", "skipped", "error"]
        out = capsys.readouterr().out
        assert "rotated.mov" in out
        assert "skip.mov" in out
        assert "broken.mov" in out
        assert "1 rotated" in out
        assert "1 error" in out


class TestWriteUndoScript:
    def test_writes_two_mv_lines_per_rotated_result(self, tmp_path):
        preserved = tmp_path / "original_unrotated" / "clip.mov"
        results = [
            RotateResult(
                path=tmp_path / "clip.mov",
                status="rotated",
                message="ok",
                preserved_path=preserved,
            )
        ]
        script_path = tmp_path / "undo.sh"

        write_undo_script(results, script_path)

        content = script_path.read_text()
        assert content.count("mv -n --") == 2
        assert str(preserved) in content
        assert f"{tmp_path / 'clip.mov'}.rotated-backup" in content

    def test_skips_non_rotated_results(self, tmp_path):
        results = [
            RotateResult(path=tmp_path / "skip.mov", status="skipped", message="x"),
            RotateResult(path=tmp_path / "err.mov", status="error", message="x"),
        ]
        script_path = tmp_path / "undo.sh"

        write_undo_script(results, script_path)

        assert "mv" not in script_path.read_text()

    def test_existence_guard_precedes_the_mv_pair(self, tmp_path):
        preserved = tmp_path / "original_unrotated" / "clip.mov"
        results = [
            RotateResult(
                path=tmp_path / "clip.mov",
                status="rotated",
                message="ok",
                preserved_path=preserved,
            )
        ]
        script_path = tmp_path / "undo.sh"

        write_undo_script(results, script_path)

        lines = script_path.read_text().splitlines()
        guard_idx = next(i for i, line in enumerate(lines) if line.startswith("[ -f"))
        assert str(preserved) in lines[guard_idx]
        assert "exit 1" in lines[guard_idx]
        assert lines[guard_idx + 1].startswith("mv -n --")
        assert lines[guard_idx + 2].startswith("mv -n --")

    def test_script_exits_nonzero_and_touches_nothing_when_preserved_is_missing(
        self, tmp_path
    ):
        # original_unrotated/ was deleted (or never existed) by the time
        # this runs -- the guard must fire before either mv, not partway
        # through (a partial run would leave the corrected file renamed
        # aside with nothing to restore in its place).
        current = tmp_path / "clip.mov"
        current.write_bytes(b"corrected bytes")
        missing_preserved = (
            tmp_path / "original_unrotated" / "clip.mov"
        )  # never created
        results = [
            RotateResult(
                path=current,
                status="rotated",
                message="ok",
                preserved_path=missing_preserved,
            )
        ]
        script_path = tmp_path / "undo.sh"
        write_undo_script(results, script_path)

        proc = subprocess.run(
            [str(script_path)], capture_output=True, text=True, cwd=tmp_path
        )

        assert proc.returncode == 1
        assert "original_unrotated" in proc.stderr
        assert current.read_bytes() == b"corrected bytes"
        assert not (tmp_path / "clip.mov.rotated-backup").exists()

    def test_script_is_executable(self, tmp_path):
        script_path = tmp_path / "undo.sh"
        write_undo_script([], script_path)
        assert script_path.stat().st_mode & 0o111


class TestRunRotateUndoScript:
    def _stub_one_rotated_file(self, monkeypatch, tmp_path):
        clip = tmp_path / "clip.mov"

        def fake_rotate_file(path, *, dry_run):
            if dry_run:
                return RotateResult(path=path, status="would_rotate", message="x")
            return RotateResult(
                path=path,
                status="rotated",
                message="ok",
                preserved_path=tmp_path / "original_unrotated" / path.name,
            )

        monkeypatch.setattr("slate.rotate.rotate_file", fake_rotate_file)
        return [clip]

    def test_writes_undo_script_by_default(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        files = self._stub_one_rotated_file(monkeypatch, tmp_path)

        run_rotate(files, dry_run=False, assume_yes=True)

        scripts = list(tmp_path.glob("undo_rotations_*.sh"))
        assert len(scripts) == 1
        assert "Undo script written" in capsys.readouterr().out

    def test_skip_generate_undo_script_suppresses_it(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.chdir(tmp_path)
        files = self._stub_one_rotated_file(monkeypatch, tmp_path)

        run_rotate(files, dry_run=False, generate_undo_script=False, assume_yes=True)

        assert list(tmp_path.glob("undo_rotations_*.sh")) == []
        assert "Undo script" not in capsys.readouterr().out

    def test_dry_run_never_writes_an_undo_script(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        files = self._stub_one_rotated_file(monkeypatch, tmp_path)

        run_rotate(files, dry_run=True)

        assert list(tmp_path.glob("undo_rotations_*.sh")) == []

    def test_no_undo_script_when_nothing_was_rotated(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            "slate.rotate.rotate_file",
            lambda path, *, dry_run: RotateResult(
                path=path, status="skipped", message="x"
            ),
        )

        run_rotate([tmp_path / "clip.mov"], dry_run=False)

        assert list(tmp_path.glob("undo_rotations_*.sh")) == []


class TestRunRotateConfirmation:
    def _stub(self, monkeypatch, tmp_path, *, label):
        # Every test in this class exercises dry_run=False -- isolate cwd
        # unconditionally so a real write_undo_script() call (whether or
        # not the test in question expects one) can never land in the
        # repo root.
        monkeypatch.chdir(tmp_path)

        def fake_rotate_file(path, *, dry_run):
            if label != NEEDS_FIX_LABEL:
                return RotateResult(
                    path=path,
                    status="skipped" if dry_run else "error",
                    message="not it",
                )
            if dry_run:
                return RotateResult(path=path, status="would_rotate", message="x")
            return RotateResult(
                path=path,
                status="rotated",
                message="ok",
                preserved_path=tmp_path / "original_unrotated" / path.name,
            )

        monkeypatch.setattr("slate.rotate.rotate_file", fake_rotate_file)
        return [tmp_path / "clip.mov"]

    def test_preview_line_prints_before_the_prompt(self, tmp_path, monkeypatch):
        files = self._stub(monkeypatch, tmp_path, label=NEEDS_FIX_LABEL)
        seen = {}

        def spy_ask(message, **kwargs):
            seen["message"] = message
            return False

        monkeypatch.setattr("slate.rotate.Confirm.ask", spy_ask)

        run_rotate(files, dry_run=False)

        assert "message" in seen, "prompt was never reached"
        assert "1 file(s) to rotate" in seen["message"]

    def test_declined_confirmation_rotates_nothing(self, tmp_path, monkeypatch):
        files = self._stub(monkeypatch, tmp_path, label=NEEDS_FIX_LABEL)
        monkeypatch.setattr("slate.rotate.Confirm.ask", lambda *a, **k: False)

        results = run_rotate(files, dry_run=False)

        assert [r.status for r in results] == ["would_rotate"]
        assert not (tmp_path / "original_unrotated").exists()

    def test_confirmed_prompt_rotates(self, tmp_path, monkeypatch):
        files = self._stub(monkeypatch, tmp_path, label=NEEDS_FIX_LABEL)
        monkeypatch.setattr("slate.rotate.Confirm.ask", lambda *a, **k: True)

        results = run_rotate(files, dry_run=False, generate_undo_script=False)

        assert [r.status for r in results] == ["rotated"]

    def test_assume_yes_skips_the_prompt_entirely(self, tmp_path, monkeypatch):
        files = self._stub(monkeypatch, tmp_path, label=NEEDS_FIX_LABEL)

        def fail_if_asked(*a, **k):
            raise AssertionError("Confirm.ask must not run with assume_yes=True")

        monkeypatch.setattr("slate.rotate.Confirm.ask", fail_if_asked)

        results = run_rotate(
            files, dry_run=False, generate_undo_script=False, assume_yes=True
        )

        assert [r.status for r in results] == ["rotated"]

    def test_no_prompt_when_nothing_needs_rotating(self, tmp_path, monkeypatch):
        files = self._stub(monkeypatch, tmp_path, label="none")

        def fail_if_asked(*a, **k):
            raise AssertionError("Confirm.ask must not run when nothing would rotate")

        monkeypatch.setattr("slate.rotate.Confirm.ask", fail_if_asked)

        results = run_rotate(files, dry_run=False)

        assert [r.status for r in results] == ["skipped"]

    def test_dry_run_never_prompts(self, tmp_path, monkeypatch):
        files = self._stub(monkeypatch, tmp_path, label=NEEDS_FIX_LABEL)

        def fail_if_asked(*a, **k):
            raise AssertionError("Confirm.ask must not run under --dry-run")

        monkeypatch.setattr("slate.rotate.Confirm.ask", fail_if_asked)

        results = run_rotate(files, dry_run=True)

        assert [r.status for r in results] == ["would_rotate"]
