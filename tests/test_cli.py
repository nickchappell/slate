import argparse

import pytest

import slate.config as config_module
from slate import cli
from slate.cli import (
    UsageError,
    _check_mapping_version_or_exit,
    _effective_settings,
    _resolve_input_files,
    build_parser,
)
from slate.config import CONFIG_ENV_VAR
from slate.mappings import APP_VERSION, MappingEntry, load_mappings, save_mappings
from slate.review_sync import hash_file


def touch(path):
    path.write_bytes(b"")
    return path


class TestBuildParser:
    def test_requires_exactly_one_mode(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])

    def test_modes_are_mutually_exclusive(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--dry-run", "--rename-only"])

    def test_input_dir_and_input_files_are_mutually_exclusive(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(
                ["--dry-run", "--input-dir", "x", "--input-files", "a.MOV"]
            )

    def test_prepend_and_append_are_mutually_exclusive(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(
                ["--dry-run", "--prepend-generated-name", "--append-generated-name"]
            )

    def test_input_files_accepts_multiple_space_separated_values(self):
        parser = build_parser()
        args = parser.parse_args(
            ["--dry-run", "--input-files", "a.MOV", "a.MP4", "b.MP4"]
        )
        assert [str(p) for p in args.input_files] == ["a.MOV", "a.MP4", "b.MP4"]

    def test_yes_short_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--dry-run", "--input-dir", "x", "-y"])
        assert args.yes is True

    def test_valid_dry_run_with_input_dir_parses(self):
        parser = build_parser()
        args = parser.parse_args(["--dry-run", "--input-dir", "footage"])
        assert args.dry_run is True
        assert str(args.input_dir) == "footage"


class TestResolveInputFiles:
    def test_input_dir_mode_discovers_video_files(self, tmp_path):
        touch(tmp_path / "a.MOV")
        touch(tmp_path / "notes.txt")
        args = argparse.Namespace(input_dir=tmp_path, input_files=None)
        files, base_dir = _resolve_input_files(args)
        assert [f.name for f in files] == ["a.MOV"]
        assert base_dir == tmp_path

    def test_input_dir_must_exist(self, tmp_path):
        args = argparse.Namespace(input_dir=tmp_path / "nope", input_files=None)
        with pytest.raises(UsageError):
            _resolve_input_files(args)

    def test_input_files_mode_returns_files_and_common_parent(self, tmp_path):
        a = touch(tmp_path / "a.MOV")
        b = touch(tmp_path / "a.MP4")
        args = argparse.Namespace(input_dir=None, input_files=[a, b])
        files, base_dir = _resolve_input_files(args)
        assert files == [a, b]
        assert base_dir == tmp_path

    def test_input_files_missing_file_raises(self, tmp_path):
        a = touch(tmp_path / "a.MOV")
        missing = tmp_path / "ghost.MP4"
        args = argparse.Namespace(input_dir=None, input_files=[a, missing])
        with pytest.raises(UsageError, match="ghost.MP4"):
            _resolve_input_files(args)

    def test_input_files_in_different_directories_raises(self, tmp_path):
        (tmp_path / "sub").mkdir()
        a = touch(tmp_path / "a.MOV")
        b = touch(tmp_path / "sub" / "b.MOV")
        args = argparse.Namespace(input_dir=None, input_files=[a, b])
        with pytest.raises(UsageError):
            _resolve_input_files(args)

    def test_neither_input_option_raises(self):
        args = argparse.Namespace(input_dir=None, input_files=None)
        with pytest.raises(UsageError):
            _resolve_input_files(args)


class TestEffectiveSettings:
    def _base_args(self, **overrides):
        base = dict(
            model=None,
            prepend_generated_name=False,
            append_generated_name=False,
            prefix=None,
            suffix=None,
            skip_generate_undo_script=False,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_no_config_file_uses_builtin_defaults(self, tmp_path, monkeypatch):
        monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
        # Guarantee "no config file present" deterministically -- regardless
        # of whether this machine happens to have a real
        # ~/.config/slate/config.toml.
        monkeypatch.setattr(
            config_module, "DEFAULT_CONFIG_PATH", tmp_path / "config.toml"
        )
        args = self._base_args()
        config, model, prepend, prefix, suffix, generate_undo = _effective_settings(
            args
        )
        assert model == config.model
        assert prepend is False
        assert prefix == ""
        assert suffix == ""
        assert generate_undo is True

    def test_cli_model_overrides_config(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.toml"
        config_file.write_text('[defaults]\nmodel = "config-model"\n')
        monkeypatch.setenv(CONFIG_ENV_VAR, str(config_file))
        args = self._base_args(model="cli-model")
        _config, model, *_ = _effective_settings(args)
        assert model == "cli-model"

    def test_config_model_used_when_no_cli_override(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.toml"
        config_file.write_text('[defaults]\nmodel = "config-model"\n')
        monkeypatch.setenv(CONFIG_ENV_VAR, str(config_file))
        args = self._base_args()
        _config, model, *_ = _effective_settings(args)
        assert model == "config-model"

    def test_cli_prepend_flag_overrides_config(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.toml"
        config_file.write_text("[defaults]\nprepend_generated_name = false\n")
        monkeypatch.setenv(CONFIG_ENV_VAR, str(config_file))
        args = self._base_args(prepend_generated_name=True)
        _config, _model, prepend, _prefix, _suffix, _undo = _effective_settings(args)
        assert prepend is True

    def test_cli_append_flag_overrides_config_prepend_true(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.toml"
        config_file.write_text("[defaults]\nprepend_generated_name = true\n")
        monkeypatch.setenv(CONFIG_ENV_VAR, str(config_file))
        args = self._base_args(append_generated_name=True)
        _config, _model, prepend, *_ = _effective_settings(args)
        assert prepend is False

    def test_no_cli_flag_falls_back_to_config_prepend(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.toml"
        config_file.write_text("[defaults]\nprepend_generated_name = true\n")
        monkeypatch.setenv(CONFIG_ENV_VAR, str(config_file))
        args = self._base_args()
        _config, _model, prepend, *_ = _effective_settings(args)
        assert prepend is True

    def test_empty_string_prefix_is_a_valid_cli_override(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.toml"
        config_file.write_text('[defaults]\nprefix = "Boston, MA"\n')
        monkeypatch.setenv(CONFIG_ENV_VAR, str(config_file))
        args = self._base_args(prefix="")
        _config, _model, _prepend, prefix, *_ = _effective_settings(args)
        assert prefix == ""

    def test_skip_undo_script_flag_forces_off_regardless_of_config(
        self, tmp_path, monkeypatch
    ):
        config_file = tmp_path / "config.toml"
        config_file.write_text("[defaults]\ngenerate_undo_script = true\n")
        monkeypatch.setenv(CONFIG_ENV_VAR, str(config_file))
        args = self._base_args(skip_generate_undo_script=True)
        *_rest, generate_undo = _effective_settings(args)
        assert generate_undo is False

    def test_config_generate_undo_script_false_respected_without_cli_flag(
        self, tmp_path, monkeypatch
    ):
        config_file = tmp_path / "config.toml"
        config_file.write_text("[defaults]\ngenerate_undo_script = false\n")
        monkeypatch.setenv(CONFIG_ENV_VAR, str(config_file))
        args = self._base_args()
        *_rest, generate_undo = _effective_settings(args)
        assert generate_undo is False


class TestCheckMappingVersionOrExit:
    def test_missing_file_does_not_exit(self, tmp_path):
        _check_mapping_version_or_exit(tmp_path / "nonexistent.json")

    def test_matching_major_version_does_not_exit(self, tmp_path):
        path = tmp_path / "mappings.json"
        save_mappings(path, [])
        _check_mapping_version_or_exit(path)

    def test_file_with_no_app_version_does_not_exit(self, tmp_path):
        path = tmp_path / "mappings.json"
        path.write_text("[]")
        _check_mapping_version_or_exit(path)

    def test_different_major_version_exits(self, tmp_path):
        path = tmp_path / "mappings.json"
        current_major = int(APP_VERSION.split(".")[0])
        path.write_text(f'{{"app_version": "{current_major + 1}.0.0", "groups": []}}')
        with pytest.raises(SystemExit):
            _check_mapping_version_or_exit(path)


class TestRunPhase2:
    """Regression coverage for cli.run_phase2's file-naming/placement
    conventions -- these live in cli.py itself (not rename.py's lower-level
    primitives, which are already covered in test_rename.py), and have
    changed more than once, so they're worth locking in explicitly."""

    def test_writes_audit_trail_into_review_and_undo_script_at_top_level(
        self, tmp_path
    ):
        touch(tmp_path / "a.MOV")
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        mappings_path = review_dir / "rename_mappings.json"
        mappings_path.write_text("[]")
        entries = [
            MappingEntry(status="ok", original_files=["a.MOV"], new_stem="a caption")
        ]

        cli.run_phase2(
            entries,
            tmp_path,
            mappings_path,
            generate_undo_script=True,
            assume_yes=True,
        )

        assert (tmp_path / "a caption.MOV").is_file()
        assert not mappings_path.exists()

        applied_files = list(review_dir.glob("applied_renames_*.json"))
        assert len(applied_files) == 1

        undo_files = list(tmp_path.glob("undo_renames_*.sh"))
        assert len(undo_files) == 1
        # same timestamp in both names -- that's what correlates them
        applied_timestamp = applied_files[0].stem.removeprefix("applied_renames_")
        undo_timestamp = undo_files[0].stem.removeprefix("undo_renames_")
        assert applied_timestamp == undo_timestamp

    def test_skip_generate_undo_script_omits_undo_file_but_keeps_audit_trail(
        self, tmp_path
    ):
        touch(tmp_path / "a.MOV")
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        mappings_path = review_dir / "rename_mappings.json"
        mappings_path.write_text("[]")
        entries = [
            MappingEntry(status="ok", original_files=["a.MOV"], new_stem="a caption")
        ]

        cli.run_phase2(
            entries,
            tmp_path,
            mappings_path,
            generate_undo_script=False,
            assume_yes=True,
        )

        assert list(tmp_path.glob("undo_renames_*.sh")) == []
        assert len(list(review_dir.glob("applied_renames_*.json"))) == 1

    def test_nothing_to_rename_leaves_mappings_file_untouched(self, tmp_path):
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        mappings_path = review_dir / "rename_mappings.json"
        mappings_path.write_text("[]")
        entries = [MappingEntry(status="error", original_files=["a.MOV"], error="boom")]

        cli.run_phase2(
            entries,
            tmp_path,
            mappings_path,
            generate_undo_script=True,
            assume_yes=True,
        )

        assert mappings_path.is_file()
        assert list(review_dir.glob("applied_renames_*.json")) == []
        assert list(tmp_path.glob("undo_renames_*.sh")) == []

    def test_declined_confirmation_renames_nothing_and_writes_no_audit_trail(
        self, tmp_path, monkeypatch
    ):
        touch(tmp_path / "a.MOV")
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        mappings_path = review_dir / "rename_mappings.json"
        mappings_path.write_text("[]")
        entries = [
            MappingEntry(status="ok", original_files=["a.MOV"], new_stem="a caption")
        ]

        monkeypatch.setattr(cli.Confirm, "ask", lambda *a, **k: False)

        cli.run_phase2(
            entries,
            tmp_path,
            mappings_path,
            generate_undo_script=True,
            assume_yes=False,
        )

        assert (tmp_path / "a.MOV").is_file()
        assert mappings_path.is_file()
        assert list(review_dir.glob("applied_renames_*.json")) == []
        assert list(tmp_path.glob("undo_renames_*.sh")) == []


class TestRunPhase2ReviewSync:
    """Phase 2 reconciles rename_mappings.json against human renames of
    preview JPEGs in review/ before building the rename plan -- see
    review_sync.sync_from_review."""

    def test_renamed_preview_jpeg_drives_the_actual_rename(self, tmp_path):
        touch(tmp_path / "a.MOV")
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        review_dir.joinpath("renamed by human.jpg").write_bytes(b"frame")
        preview_sha256 = hash_file(review_dir / "renamed by human.jpg")

        mappings_path = review_dir / "rename_mappings.json"
        mappings_path.write_text("[]")
        entries = [
            MappingEntry(
                status="ok",
                original_files=["a.MOV"],
                new_stem="original caption",
                preview_jpeg="original caption.jpg",
                preview_jpeg_sha256=preview_sha256,
            )
        ]

        cli.run_phase2(
            entries,
            tmp_path,
            mappings_path,
            generate_undo_script=False,
            assume_yes=True,
        )

        assert (tmp_path / "renamed by human.MOV").is_file()
        assert not (tmp_path / "original caption.MOV").exists()

        applied_files = list(review_dir.glob("applied_renames_*.json"))
        applied_entries = load_mappings(applied_files[0])
        assert applied_entries[0].new_stem == "renamed by human"

    def test_deleted_preview_jpeg_skips_that_groups_rename(self, tmp_path):
        touch(tmp_path / "a.MOV")
        review_dir = tmp_path / "review"
        review_dir.mkdir()  # preview JPEG deliberately absent

        mappings_path = review_dir / "rename_mappings.json"
        mappings_path.write_text("[]")
        entries = [
            MappingEntry(
                status="ok",
                original_files=["a.MOV"],
                new_stem="original caption",
                preview_jpeg="original caption.jpg",
                preview_jpeg_sha256="deadbeef",
            )
        ]

        cli.run_phase2(
            entries,
            tmp_path,
            mappings_path,
            generate_undo_script=False,
            assume_yes=True,
        )

        assert (tmp_path / "a.MOV").is_file()
        assert not (tmp_path / "original caption.MOV").exists()

    def test_synced_name_colliding_with_an_unrelated_file_is_skipped_not_applied(
        self, tmp_path
    ):
        """The rename a JPEG-rename resolves to still goes through the
        ordinary destination-collision check -- sync doesn't get a free
        pass around it."""
        touch(tmp_path / "a.MOV")
        touch(tmp_path / "existing target.MOV")  # unrelated, pre-existing file

        review_dir = tmp_path / "review"
        review_dir.mkdir()
        review_dir.joinpath("existing target.jpg").write_bytes(b"frame")
        preview_sha256 = hash_file(review_dir / "existing target.jpg")

        mappings_path = review_dir / "rename_mappings.json"
        mappings_path.write_text("[]")
        entries = [
            MappingEntry(
                status="ok",
                original_files=["a.MOV"],
                new_stem="original caption",
                preview_jpeg="original caption.jpg",
                preview_jpeg_sha256=preview_sha256,
            )
        ]

        cli.run_phase2(
            entries,
            tmp_path,
            mappings_path,
            generate_undo_script=False,
            assume_yes=True,
        )

        assert (tmp_path / "a.MOV").is_file()
        assert (tmp_path / "existing target.MOV").is_file()


class TestRunPhase1PreviewHash:
    """Locks in the invariant review_sync depends on: run_phase1 records
    preview_jpeg_sha256 as the actual content hash of the preview JPEG it
    writes to disk."""

    def test_preview_jpeg_sha256_matches_the_written_file(self, tmp_path, monkeypatch):
        source = touch(tmp_path / "a.MOV")
        monkeypatch.setattr(
            cli,
            "extract_frame",
            lambda source, output: output.write_bytes(b"fake-frame-bytes"),
        )
        monkeypatch.setattr(cli, "generate_caption", lambda *a, **k: "a caption")

        review_dir = tmp_path / "review"
        mappings_path = review_dir / "rename_mappings.json"

        _all, new_entries, _skipped = cli.run_phase1(
            [source],
            tmp_path,
            mappings_path,
            review_dir,
            model="fake-model",
            prompt="fake-prompt",
            prepend=False,
            prefix="",
            suffix="",
            max_file_name_length=255,
        )

        assert len(new_entries) == 1
        entry = new_entries[0]
        assert entry.preview_jpeg_sha256 == hash_file(review_dir / entry.preview_jpeg)

    def test_same_run_name_collision_does_not_clobber_the_other_preview(
        self, tmp_path, monkeypatch
    ):
        """ "a.MOV" (caption "b c") and "a b.MOV" (caption "c") both assemble
        to "a b c" -- a real, if narrow, way two clips processed in the same
        run can coincidentally collide before disambiguate() has a chance to
        tell them apart. The naive fix (rename straight to <new_stem>.jpg as
        each entry is processed) lets the second one silently overwrite the
        first's preview JPEG on disk; each entry's preview_jpeg/
        preview_jpeg_sha256 must end up pointing at *its own* frame."""
        a = touch(tmp_path / "a.MOV")
        a_b = touch(tmp_path / "a b.MOV")

        def fake_extract_frame(source, output):
            output.write_bytes(f"frame-for-{source.stem}".encode())

        def fake_generate_caption(frame_path, prompt, model):
            return "c" if frame_path.endswith(".tmp.a b.jpg") else "b c"

        monkeypatch.setattr(cli, "extract_frame", fake_extract_frame)
        monkeypatch.setattr(cli, "generate_caption", fake_generate_caption)

        review_dir = tmp_path / "review"
        mappings_path = review_dir / "rename_mappings.json"

        _all, new_entries, _skipped = cli.run_phase1(
            [a, a_b],
            tmp_path,
            mappings_path,
            review_dir,
            model="fake-model",
            prompt="fake-prompt",
            prepend=False,
            prefix="",
            suffix="",
            max_file_name_length=255,
        )

        assert len(new_entries) == 2
        by_original = {e.original_files[0]: e for e in new_entries}
        entry_a = by_original["a.MOV"]
        entry_a_b = by_original["a b.MOV"]

        # Both assembled to "a b c" -- disambiguation must have separated them.
        assert {entry_a.new_stem, entry_a_b.new_stem} == {"a b c", "a b c_2"}
        assert entry_a.new_stem != entry_a_b.new_stem

        # Each entry's on-disk preview holds *its own* frame, and its
        # recorded hash matches that file, not the other entry's.
        preview_a = review_dir / entry_a.preview_jpeg
        preview_a_b = review_dir / entry_a_b.preview_jpeg
        assert preview_a.read_bytes() == b"frame-for-a"
        assert preview_a_b.read_bytes() == b"frame-for-a b"
        assert entry_a.preview_jpeg_sha256 == hash_file(preview_a)
        assert entry_a_b.preview_jpeg_sha256 == hash_file(preview_a_b)
