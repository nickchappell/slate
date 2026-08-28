import argparse

import pytest

import slate.config as config_module
from slate import cli
from slate.cli import (
    UsageError,
    _effective_settings,
    _resolve_input_files,
    build_parser,
)
from slate.config import CONFIG_ENV_VAR
from slate.mappings import MappingEntry, load_mappings
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


class TestRunPhase2:
    """Regression coverage for cli.run_phase2's file-naming/placement
    conventions -- these live in cli.py itself (not rename.py's lower-level
    primitives, which are already covered in test_rename.py), and have
    changed more than once, so they're worth locking in explicitly."""

    def test_writes_audit_trail_into_review_and_undo_script_at_top_level(
        self, tmp_path
    ):
        touch(tmp_path / "a.MOV")
        mappings_path = tmp_path / "rename_mappings.json"
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

        applied_files = list((tmp_path / "review").glob("applied_renames_*.json"))
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
        mappings_path = tmp_path / "rename_mappings.json"
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
        assert len(list((tmp_path / "review").glob("applied_renames_*.json"))) == 1

    def test_nothing_to_rename_leaves_mappings_file_untouched(self, tmp_path):
        mappings_path = tmp_path / "rename_mappings.json"
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
        assert not (tmp_path / "review").exists()
        assert list(tmp_path.glob("undo_renames_*.sh")) == []

    def test_declined_confirmation_renames_nothing_and_writes_no_audit_trail(
        self, tmp_path, monkeypatch
    ):
        touch(tmp_path / "a.MOV")
        mappings_path = tmp_path / "rename_mappings.json"
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
        assert not (tmp_path / "review").exists()
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

        mappings_path = tmp_path / "rename_mappings.json"
        mappings_path.write_text("[]")
        entries = [
            MappingEntry(
                status="ok",
                original_files=["a.MOV"],
                new_stem="original caption",
                preview_jpeg="review/original caption.jpg",
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

        applied_files = list((tmp_path / "review").glob("applied_renames_*.json"))
        applied_entries = load_mappings(applied_files[0])
        assert applied_entries[0].new_stem == "renamed by human"

    def test_deleted_preview_jpeg_skips_that_groups_rename(self, tmp_path):
        touch(tmp_path / "a.MOV")
        review_dir = tmp_path / "review"
        review_dir.mkdir()  # preview JPEG deliberately absent

        mappings_path = tmp_path / "rename_mappings.json"
        mappings_path.write_text("[]")
        entries = [
            MappingEntry(
                status="ok",
                original_files=["a.MOV"],
                new_stem="original caption",
                preview_jpeg="review/original caption.jpg",
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
