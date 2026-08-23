import argparse

import pytest

import slate.config as config_module
from slate.cli import (
    UsageError,
    _effective_settings,
    _resolve_input_files,
    build_parser,
)
from slate.config import CONFIG_ENV_VAR


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
