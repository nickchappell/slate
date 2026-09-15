import json
import subprocess

from slate.metadata import (
    EmbedOutcome,
    ExistingMetadata,
    embed_metadata,
    read_existing_metadata,
)


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
