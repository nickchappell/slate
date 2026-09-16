import json
import subprocess
from pathlib import Path

from slate.metadata import (
    EmbedOutcome,
    ExistingMetadata,
    embed_metadata,
    read_existing_metadata,
)

TERMINATOR_ERROR = "Error: [minor] Terminator found in Meta with 478 bytes remaining"


class FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def make_fake_run(
    *, read_json: object = None, read_ok: bool = True, write_ok: bool = True
):
    """Fakes exiftool based on the presence of -j (read) vs
    -overwrite_original (write) in argv, mirroring test_extraction.py's
    argv-dispatch convention."""

    def fake_run(cmd, capture_output=True, timeout=None, text=False, **kwargs):
        assert cmd[0] == "exiftool"

        if "-j" in cmd:
            if not read_ok:
                return FakeCompletedProcess(1, stderr="exiftool: read error")
            return FakeCompletedProcess(0, stdout=json.dumps(read_json or []))

        if "-overwrite_original" in cmd:
            if not write_ok:
                return FakeCompletedProcess(1, stderr="exiftool: write error")
            return FakeCompletedProcess(0)

        raise AssertionError(f"unexpected exiftool invocation: {cmd}")

    return fake_run


class TestReadExistingMetadata:
    def test_empty_fields_returns_all_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(subprocess, "run", make_fake_run(read_json=[{}]))
        result = read_existing_metadata(tmp_path / "clip.mov")
        assert result == ExistingMetadata(
            title=None, description=None, keywords=None, has_slate_provenance=False
        )

    def test_non_empty_fields_are_returned(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            make_fake_run(
                read_json=[
                    {
                        "Title": "Edited in FCPX",
                        "Description": "Edited in FCPX 10.7",
                        "Keywords": "fcpx, edited",
                    }
                ]
            ),
        )
        result = read_existing_metadata(tmp_path / "clip.mov")
        assert result.title == "Edited in FCPX"
        assert result.description == "Edited in FCPX 10.7"
        assert result.keywords == "fcpx, edited"

    def test_list_type_keywords_are_joined_to_a_string(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            make_fake_run(read_json=[{"Keywords": ["fcpx", "edited"]}]),
        )
        result = read_existing_metadata(tmp_path / "clip.mov")
        assert result.keywords == "fcpx, edited"

    def test_provenance_detected_when_present(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            make_fake_run(read_json=[{"SlateAppVersion": "0.2.2"}]),
        )
        result = read_existing_metadata(tmp_path / "clip.mov")
        assert result.has_slate_provenance is True

    def test_provenance_false_when_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(subprocess, "run", make_fake_run(read_json=[{}]))
        result = read_existing_metadata(tmp_path / "clip.mov")
        assert result.has_slate_provenance is False

    def test_nonzero_exit_is_safe(self, tmp_path, monkeypatch):
        monkeypatch.setattr(subprocess, "run", make_fake_run(read_ok=False))
        result = read_existing_metadata(tmp_path / "clip.mov")
        assert result == ExistingMetadata(
            title=None, description=None, keywords=None, has_slate_provenance=False
        )

    def test_subprocess_exception_is_safe(self, tmp_path, monkeypatch):
        def raising_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 60)

        monkeypatch.setattr(subprocess, "run", raising_run)
        result = read_existing_metadata(tmp_path / "clip.mov")
        assert result.has_slate_provenance is False

    def test_malformed_json_is_safe(self, tmp_path, monkeypatch):
        def fake_run(cmd, **kwargs):
            return FakeCompletedProcess(0, stdout="not json")

        monkeypatch.setattr(subprocess, "run", fake_run)
        result = read_existing_metadata(tmp_path / "clip.mov")
        assert result == ExistingMetadata(
            title=None, description=None, keywords=None, has_slate_provenance=False
        )

    def test_empty_json_array_is_safe(self, tmp_path, monkeypatch):
        monkeypatch.setattr(subprocess, "run", make_fake_run(read_json=[]))
        result = read_existing_metadata(tmp_path / "clip.mov")
        assert result.has_slate_provenance is False


class TestEmbedMetadata:
    def _embed(self, path, monkeypatch, cmds, **overrides):
        def fake_run(cmd, capture_output=True, timeout=None, text=False, **kwargs):
            cmds.append(cmd)
            if "-j" in cmd:
                return FakeCompletedProcess(
                    0, stdout=json.dumps([overrides.get("read_json", {})])
                )
            if overrides.get("write_fails"):
                return FakeCompletedProcess(1, stderr="exiftool: permission denied")
            return FakeCompletedProcess(0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        return embed_metadata(
            path,
            title="A017_C015 red kayak at sunset",
            description="A red kayak drifts across a calm lake at sunset.",
            keywords=["kayak", "lake", "sunset"],
            original_filename="A017_C015_0806GQ.MOV",
            app_version="0.2.2",
            caption_model="mlx-community/Qwen2-VL-2B-Instruct-4bit",
            generated_at="2026-09-14T18:32:07Z",
        )

    def test_clean_write_has_no_preserved_fields(self, tmp_path, monkeypatch):
        cmds: list[list[str]] = []
        outcome = self._embed(tmp_path / "clip.mov", monkeypatch, cmds)
        assert outcome == EmbedOutcome(embedded=True, preserved_fields=[])

    def test_clean_write_includes_all_tag_families(self, tmp_path, monkeypatch):
        cmds: list[list[str]] = []
        self._embed(tmp_path / "clip.mov", monkeypatch, cmds)
        write_cmd = cmds[-1]
        joined = " ".join(write_cmd)
        assert "-ItemList:Title=A017_C015 red kayak at sunset" in joined
        assert "-Keys:Title=A017_C015 red kayak at sunset" in joined
        assert "-XMP-dc:Title=A017_C015 red kayak at sunset" in joined
        assert (
            "-ItemList:Description=A red kayak drifts across a calm lake at "
            "sunset." in joined
        )
        assert (
            "-Keys:Description=A red kayak drifts across a calm lake at "
            "sunset." in joined
        )
        assert "-XMP-dc:Description=" in joined

    def test_keywords_written_as_repeated_flags_only_for_xmp_subject(
        self, tmp_path, monkeypatch
    ):
        # ItemList:Keyword and Keys:Keywords are confirmed NOT list-type --
        # only XMP-dc:Subject genuinely accepts repeated flags as a list.
        cmds: list[list[str]] = []
        self._embed(tmp_path / "clip.mov", monkeypatch, cmds)
        write_cmd = cmds[-1]
        assert write_cmd.count("-XMP-dc:Subject=kayak") == 1
        assert write_cmd.count("-XMP-dc:Subject=lake") == 1
        assert write_cmd.count("-XMP-dc:Subject=sunset") == 1
        assert "-ItemList:Keyword=kayak" not in write_cmd
        assert "-Keys:Keywords=kayak" not in write_cmd

    def test_itemlist_and_keys_keywords_are_joined_strings(self, tmp_path, monkeypatch):
        cmds: list[list[str]] = []
        self._embed(tmp_path / "clip.mov", monkeypatch, cmds)
        write_cmd = cmds[-1]
        assert "-ItemList:Keyword=kayak, lake, sunset" in write_cmd
        assert "-Keys:Keywords=kayak, lake, sunset" in write_cmd

    def test_provenance_keys_are_written(self, tmp_path, monkeypatch):
        cmds: list[list[str]] = []
        self._embed(tmp_path / "clip.mov", monkeypatch, cmds)
        write_cmd = cmds[-1]
        assert "-Keys:SlateOriginalFilename=A017_C015_0806GQ.MOV" in write_cmd
        assert "-Keys:SlateAppVersion=0.2.2" in write_cmd
        assert (
            "-Keys:SlateCaptionModel=mlx-community/Qwen2-VL-2B-Instruct-4bit"
            in write_cmd
        )
        assert "-Keys:SlateGeneratedAt=2026-09-14T18:32:07Z" in write_cmd

    def test_config_flag_always_present(self, tmp_path, monkeypatch):
        # Required for the custom com.slate.* tags to be recognized -- see
        # metadata.py's module docstring for the empirical verification.
        cmds: list[list[str]] = []
        self._embed(tmp_path / "clip.mov", monkeypatch, cmds)
        assert "-config" in cmds[-1]

    def test_overwrite_original_always_present(self, tmp_path, monkeypatch):
        cmds: list[list[str]] = []
        self._embed(tmp_path / "clip.mov", monkeypatch, cmds)
        assert "-overwrite_original" in cmds[-1]

    def test_never_uses_a_blanket_clear_flag(self, tmp_path, monkeypatch):
        cmds: list[list[str]] = []
        self._embed(tmp_path / "clip.mov", monkeypatch, cmds)
        assert not any(arg.startswith("-all=") for arg in cmds[-1])

    def test_preexisting_title_only_is_preserved(self, tmp_path, monkeypatch):
        cmds: list[list[str]] = []
        outcome = self._embed(
            tmp_path / "clip.mov",
            monkeypatch,
            cmds,
            read_json={"Title": "Edited in FCPX"},
        )
        assert outcome.preserved_fields == ["Title"]
        assert "-Keys:SlateOriginalTitle=Edited in FCPX" in cmds[-1]
        assert not any("SlateOriginalDescription" in arg for arg in cmds[-1])
        assert not any("SlateOriginalKeywords" in arg for arg in cmds[-1])

    def test_all_preexisting_fields_are_preserved(self, tmp_path, monkeypatch):
        cmds: list[list[str]] = []
        outcome = self._embed(
            tmp_path / "clip.mov",
            monkeypatch,
            cmds,
            read_json={
                "Title": "old title",
                "Description": "old description",
                "Keywords": "old, keywords",
            },
        )
        assert outcome.preserved_fields == ["Title", "Description", "Keywords"]
        assert "-Keys:SlateOriginalTitle=old title" in cmds[-1]
        assert "-Keys:SlateOriginalDescription=old description" in cmds[-1]
        assert "-Keys:SlateOriginalKeywords=old, keywords" in cmds[-1]

    def test_empty_preexisting_fields_are_not_preserved(self, tmp_path, monkeypatch):
        cmds: list[list[str]] = []
        outcome = self._embed(tmp_path / "clip.mov", monkeypatch, cmds, read_json={})
        assert outcome.preserved_fields == []

    def test_write_failure_reports_error_outcome(self, tmp_path, monkeypatch):
        cmds: list[list[str]] = []
        outcome = self._embed(
            tmp_path / "clip.mov", monkeypatch, cmds, write_fails=True
        )
        assert outcome.embedded is False
        assert "permission denied" in outcome.error

    def test_write_subprocess_exception_reports_error_outcome(
        self, tmp_path, monkeypatch
    ):
        def fake_run(cmd, capture_output=True, timeout=None, text=False, **kwargs):
            if "-j" in cmd:
                return FakeCompletedProcess(0, stdout="[{}]")
            raise OSError("exiftool not found")

        monkeypatch.setattr(subprocess, "run", fake_run)

        outcome = embed_metadata(
            tmp_path / "clip.mov",
            title="t",
            description="d",
            keywords=["k"],
            original_filename="orig.MOV",
            app_version="0.2.2",
            caption_model="model",
            generated_at="2026-09-14T18:32:07Z",
        )
        assert outcome.embedded is False
        assert "exiftool not found" in outcome.error

    def test_write_failure_still_reports_preserved_fields(self, tmp_path, monkeypatch):
        cmds: list[list[str]] = []
        outcome = self._embed(
            tmp_path / "clip.mov",
            monkeypatch,
            cmds,
            read_json={"Title": "old title"},
            write_fails=True,
        )
        assert outcome.embedded is False
        assert outcome.preserved_fields == ["Title"]

    def test_read_call_happens_before_write_call(self, tmp_path, monkeypatch):
        cmds: list[list[str]] = []
        self._embed(tmp_path / "clip.mov", monkeypatch, cmds)
        assert "-j" in cmds[0]
        assert "-overwrite_original" in cmds[1]

    def test_operates_on_the_given_path(self, tmp_path, monkeypatch):
        cmds: list[list[str]] = []
        target = tmp_path / "clip.mov"
        self._embed(target, monkeypatch, cmds)
        assert cmds[-1][-1] == str(target)


class TestEmbedMetadataFfmpegKeysFamilySplit:
    """Covers the "Terminator found in Meta" atom-family split -- see
    spec/metadata-write-corruption.md's "Follow-up: two-pass ffmpeg +
    exiftool" section. An earlier version of this fix retried the exact
    same combined exiftool write after a structural-only ffmpeg repair;
    that was found (via a real fixture + ffprobe, not just exiftool reads)
    to silently concatenate slate's new values into the vendor's
    com.apple.quicktime.* tags, because exiftool appending new Keys
    entries onto a Keys/mdta atom it didn't build lands on existing
    low-numbered vendor slots instead of fresh ones. The fix here is an
    ownership split, not a retry: ffmpeg (_ffmpeg_write_keys_family) is
    the only writer of the entire Keys/mdta family (repair + new values +
    provenance, all via fully-qualified -metadata keys), and a second,
    narrower exiftool call -- never touching -Keys: -- handles only
    ItemList/XMP-dc afterward.

    fake_run distinguishes exiftool's three possible invocations by argv
    shape: -j is the pre-write read, -config only appears on the first
    (combined, all-families) write attempt, and its absence marks the
    second (ItemList/XMP-dc-only) write. ffmpeg fakes the repair by
    writing bytes to its output path (the last argv element), mirroring
    how the real function swaps the file in via Path.replace().

    mebx_repair (None by default) fakes Bento4's mp4dump/mp4extract/
    mp4edit for _repair_mislabeled_data_tracks -- see "Follow-up: fixing
    the mebx mislabeling with Bento4" in spec/metadata-write-corruption.md.
    None mirrors Bento4 not being installed (mp4dump exits non-zero, the
    repair silently no-ops); passing a dict with 'original_fourccs' /
    'remuxed_fourccs' (parallel lists, one entry per trak) fakes the two
    mp4dump calls _repair_mislabeled_data_tracks makes, in that order."""

    def _make_fake_run(
        self,
        *,
        combined_write_error: str = "",
        ffmpeg_ok: bool = True,
        itemlist_write_error: str = "",
        read_json: object = None,
        mebx_repair: dict | None = None,
    ):
        calls: list[list[str]] = []
        mp4dump_calls: list[int] = []

        def _mp4dump_json(fourccs: list[str | None]) -> str:
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
                                        "children": [
                                            {
                                                "name": "minf",
                                                "children": [
                                                    {
                                                        "name": "stbl",
                                                        "children": [
                                                            {
                                                                "name": "stsd",
                                                                "children": (
                                                                    [{"name": fc}]
                                                                    if fc
                                                                    else []
                                                                ),
                                                            }
                                                        ],
                                                    }
                                                ],
                                            }
                                        ],
                                    }
                                ],
                            }
                            for fc in fourccs
                        ],
                    }
                ]
            )

        def fake_run(cmd, capture_output=True, timeout=None, text=False, **kwargs):
            calls.append(cmd)

            if cmd[0] == "exiftool" and "-j" in cmd:
                return FakeCompletedProcess(0, stdout=json.dumps([read_json or {}]))

            if cmd[0] == "exiftool" and "-config" in cmd:
                if combined_write_error:
                    return FakeCompletedProcess(1, stderr=combined_write_error)
                return FakeCompletedProcess(0)

            if cmd[0] == "exiftool":
                if itemlist_write_error:
                    return FakeCompletedProcess(1, stderr=itemlist_write_error)
                return FakeCompletedProcess(0)

            if cmd[0] == "ffmpeg":
                if not ffmpeg_ok:
                    return FakeCompletedProcess(1, stderr="ffmpeg: decode error")
                Path(cmd[-1]).write_bytes(b"repaired bytes")
                return FakeCompletedProcess(0)

            if cmd[0] == "mp4dump":
                if mebx_repair is None:
                    return FakeCompletedProcess(1, stderr="mp4dump: not found")
                mp4dump_calls.append(1)
                key = (
                    "original_fourccs" if len(mp4dump_calls) == 1 else "remuxed_fourccs"
                )
                return FakeCompletedProcess(0, stdout=_mp4dump_json(mebx_repair[key]))

            if cmd[0] == "mp4extract":
                Path(cmd[-1]).write_bytes(b"atom")
                return FakeCompletedProcess(0)

            if cmd[0] == "mp4edit":
                if mebx_repair is not None and not mebx_repair.get("mp4edit_ok", True):
                    return FakeCompletedProcess(1, stderr="mp4edit: failed")
                Path(cmd[-1]).write_bytes(b"mebx repaired bytes")
                return FakeCompletedProcess(0)

            raise AssertionError(f"unexpected invocation: {cmd}")

        return fake_run, calls

    def _embed(self, path, monkeypatch, fake_run):
        monkeypatch.setattr(subprocess, "run", fake_run)
        return embed_metadata(
            path,
            title="A017_C015 red kayak at sunset",
            description="A red kayak drifts across a calm lake at sunset.",
            keywords=["kayak", "lake", "sunset"],
            original_filename="A017_C015_0806GQ.MOV",
            app_version="0.2.2",
            caption_model="mlx-community/Qwen2-VL-2B-Instruct-4bit",
            generated_at="2026-09-14T18:32:07Z",
        )

    def test_terminator_error_triggers_split_write_and_succeeds(
        self, tmp_path, monkeypatch
    ):
        target = tmp_path / "clip.mov"
        target.write_bytes(b"original bytes")
        fake_run, calls = self._make_fake_run(combined_write_error=TERMINATOR_ERROR)

        outcome = self._embed(target, monkeypatch, fake_run)

        assert outcome == EmbedOutcome(embedded=True, preserved_fields=[])
        assert any(c[0] == "ffmpeg" for c in calls)
        # ffmpeg's rewritten bytes ended up at the real path -- Path.replace()
        # swapped them in rather than leaving a stray temp file.
        assert target.read_bytes() == b"repaired bytes"

    def test_ffmpeg_keys_family_write_uses_fully_qualified_names(
        self, tmp_path, monkeypatch
    ):
        # Regression guard: bare -metadata title=/description=/keywords=
        # writes a differently-named key that collides with ItemList's own
        # same-named key on read (shows up joined with ";" to itself).
        target = tmp_path / "clip.mov"
        target.write_bytes(b"original bytes")
        fake_run, calls = self._make_fake_run(combined_write_error=TERMINATOR_ERROR)

        self._embed(target, monkeypatch, fake_run)

        ffmpeg_cmd = next(c for c in calls if c[0] == "ffmpeg")
        joined = " ".join(ffmpeg_cmd)
        assert "com.apple.quicktime.title=A017_C015 red kayak at sunset" in joined
        assert (
            "com.apple.quicktime.description=A red kayak drifts across a "
            "calm lake at sunset." in joined
        )
        assert "com.apple.quicktime.keywords=kayak, lake, sunset" in joined
        assert "-metadata title=" not in joined
        assert "-metadata description=" not in joined
        assert "-metadata keywords=" not in joined

    def test_ffmpeg_keys_family_write_includes_provenance(self, tmp_path, monkeypatch):
        target = tmp_path / "clip.mov"
        target.write_bytes(b"original bytes")
        fake_run, calls = self._make_fake_run(combined_write_error=TERMINATOR_ERROR)

        self._embed(target, monkeypatch, fake_run)

        ffmpeg_cmd = next(c for c in calls if c[0] == "ffmpeg")
        joined = " ".join(ffmpeg_cmd)
        assert "com.slate.original-filename=A017_C015_0806GQ.MOV" in joined
        assert "com.slate.app-version=0.2.2" in joined
        assert (
            "com.slate.caption-model=mlx-community/Qwen2-VL-2B-Instruct-4bit" in joined
        )
        assert "com.slate.generated-at=2026-09-14T18:32:07Z" in joined

    def test_itemlist_followup_never_touches_keys_family(self, tmp_path, monkeypatch):
        target = tmp_path / "clip.mov"
        target.write_bytes(b"original bytes")
        fake_run, calls = self._make_fake_run(combined_write_error=TERMINATOR_ERROR)

        self._embed(target, monkeypatch, fake_run)

        itemlist_cmd = calls[-1]
        assert itemlist_cmd[0] == "exiftool"
        assert "-config" not in itemlist_cmd
        assert not any(arg.startswith("-Keys:") for arg in itemlist_cmd)
        assert any(arg.startswith("-ItemList:Title=") for arg in itemlist_cmd)
        assert any(arg.startswith("-XMP-dc:Title=") for arg in itemlist_cmd)

    def test_non_terminator_error_does_not_trigger_split(self, tmp_path, monkeypatch):
        target = tmp_path / "clip.mov"
        target.write_bytes(b"original bytes")
        fake_run, calls = self._make_fake_run(
            combined_write_error="exiftool: permission denied"
        )

        outcome = self._embed(target, monkeypatch, fake_run)

        assert outcome.embedded is False
        assert "permission denied" in outcome.error
        assert not any(c[0] == "ffmpeg" for c in calls)
        assert target.read_bytes() == b"original bytes"

    def test_ffmpeg_failure_falls_back_to_original_error(self, tmp_path, monkeypatch):
        target = tmp_path / "clip.mov"
        target.write_bytes(b"original bytes")
        fake_run, calls = self._make_fake_run(
            combined_write_error=TERMINATOR_ERROR, ffmpeg_ok=False
        )

        outcome = self._embed(target, monkeypatch, fake_run)

        assert outcome.embedded is False
        assert "Terminator found in Meta" in outcome.error
        # read, combined write, failed ffmpeg attempt -- no ItemList/XMP
        # call, since that only ever follows a successful ffmpeg write.
        assert len(calls) == 3
        assert calls[-1][0] == "ffmpeg"
        assert target.read_bytes() == b"original bytes"

    def test_ffmpeg_succeeds_but_itemlist_followup_fails(self, tmp_path, monkeypatch):
        target = tmp_path / "clip.mov"
        target.write_bytes(b"original bytes")
        fake_run, calls = self._make_fake_run(
            combined_write_error=TERMINATOR_ERROR,
            itemlist_write_error="exiftool: disk full",
        )

        outcome = self._embed(target, monkeypatch, fake_run)

        assert outcome.embedded is False
        assert "disk full" in outcome.error
        assert any(c[0] == "ffmpeg" for c in calls)
        # ffmpeg's Keys/mdta write still landed even though the follow-up
        # ItemList/XMP write failed.
        assert target.read_bytes() == b"repaired bytes"

    def test_preserved_fields_survive_and_are_written_via_ffmpeg(
        self, tmp_path, monkeypatch
    ):
        target = tmp_path / "clip.mov"
        target.write_bytes(b"original bytes")
        fake_run, calls = self._make_fake_run(
            combined_write_error=TERMINATOR_ERROR,
            read_json={"Title": "old title"},
        )

        outcome = self._embed(target, monkeypatch, fake_run)

        assert outcome == EmbedOutcome(embedded=True, preserved_fields=["Title"])
        ffmpeg_cmd = next(c for c in calls if c[0] == "ffmpeg")
        assert "com.slate.original-title=old title" in " ".join(ffmpeg_cmd)

    def test_mebx_repair_skipped_when_bento4_not_installed(self, tmp_path, monkeypatch):
        # mebx_repair=None (the default) fakes mp4dump exiting non-zero, as
        # if Bento4 weren't installed -- confirms the repair step is a
        # silent no-op, not a hard dependency for the write itself.
        target = tmp_path / "clip.mov"
        target.write_bytes(b"original bytes")
        fake_run, calls = self._make_fake_run(combined_write_error=TERMINATOR_ERROR)

        outcome = self._embed(target, monkeypatch, fake_run)

        assert outcome == EmbedOutcome(embedded=True, preserved_fields=[])
        assert not any(c[0] in ("mp4extract", "mp4edit") for c in calls)
        assert target.read_bytes() == b"repaired bytes"

    def test_mebx_repair_skipped_when_no_fourcc_mismatch(self, tmp_path, monkeypatch):
        target = tmp_path / "clip.mov"
        target.write_bytes(b"original bytes")
        fake_run, calls = self._make_fake_run(
            combined_write_error=TERMINATOR_ERROR,
            mebx_repair={
                "original_fourccs": ["apcn", "mp4a"],
                "remuxed_fourccs": ["apcn", "mp4a"],
            },
        )

        outcome = self._embed(target, monkeypatch, fake_run)

        assert outcome == EmbedOutcome(embedded=True, preserved_fields=[])
        assert any(c[0] == "mp4dump" for c in calls)
        assert not any(c[0] in ("mp4extract", "mp4edit") for c in calls)
        assert target.read_bytes() == b"repaired bytes"

    def test_mebx_repair_applied_on_fourcc_mismatch(self, tmp_path, monkeypatch):
        target = tmp_path / "clip.mov"
        target.write_bytes(b"original bytes")
        fake_run, calls = self._make_fake_run(
            combined_write_error=TERMINATOR_ERROR,
            mebx_repair={
                "original_fourccs": ["apcn", "mp4a", "mebx"],
                "remuxed_fourccs": ["apcn", "mp4a", "stts"],
            },
        )

        outcome = self._embed(target, monkeypatch, fake_run)

        assert outcome == EmbedOutcome(embedded=True, preserved_fields=[])
        extract_cmds = [c for c in calls if c[0] == "mp4extract"]
        assert any("moov/trak[2]/mdia/hdlr" in c for c in extract_cmds)
        assert any("moov/trak[2]/mdia/minf/stbl/stsd" in c for c in extract_cmds)
        # Video/audio (index 0, 1) never mismatch under -c copy -- only the
        # one mismatched track's atoms should be touched.
        assert not any("trak[0]" in " ".join(c) for c in extract_cmds)
        assert not any("trak[1]" in " ".join(c) for c in extract_cmds)
        edit_cmd = next(c for c in calls if c[0] == "mp4edit")
        assert "moov/trak[2]/mdia/hdlr:" in " ".join(edit_cmd)
        assert "moov/trak[2]/mdia/minf/stbl/stsd:" in " ".join(edit_cmd)
        # mp4edit's output replaces ffmpeg's raw output before the final
        # swap into the real path -- confirms the repair actually lands.
        assert target.read_bytes() == b"mebx repaired bytes"

    def test_mebx_repair_skipped_when_track_counts_differ(self, tmp_path, monkeypatch):
        target = tmp_path / "clip.mov"
        target.write_bytes(b"original bytes")
        fake_run, calls = self._make_fake_run(
            combined_write_error=TERMINATOR_ERROR,
            mebx_repair={
                "original_fourccs": ["apcn", "mp4a", "mebx"],
                "remuxed_fourccs": ["apcn", "mp4a"],
            },
        )

        outcome = self._embed(target, monkeypatch, fake_run)

        assert outcome == EmbedOutcome(embedded=True, preserved_fields=[])
        assert not any(c[0] in ("mp4extract", "mp4edit") for c in calls)
        assert target.read_bytes() == b"repaired bytes"

    def test_mebx_repair_failure_leaves_ffmpeg_output_in_place(
        self, tmp_path, monkeypatch
    ):
        # mp4edit failing (e.g. a malformed atom) must not fail the whole
        # embed -- the Keys/mdta write ffmpeg already completed is still
        # good; the mebx repair is strictly an enhancement on top of it.
        target = tmp_path / "clip.mov"
        target.write_bytes(b"original bytes")
        fake_run, calls = self._make_fake_run(
            combined_write_error=TERMINATOR_ERROR,
            mebx_repair={
                "original_fourccs": ["apcn", "mp4a", "mebx"],
                "remuxed_fourccs": ["apcn", "mp4a", "stts"],
                "mp4edit_ok": False,
            },
        )

        outcome = self._embed(target, monkeypatch, fake_run)

        assert outcome == EmbedOutcome(embedded=True, preserved_fields=[])
        assert any(c[0] == "mp4edit" for c in calls)
        assert target.read_bytes() == b"repaired bytes"
