import pytest

from slate.config import (
    CONFIG_ENV_VAR,
    Config,
    load_config,
    resolve_config_path,
)


class TestResolveConfigPath:
    def test_no_env_var_returns_default_path(self, monkeypatch):
        monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
        path = resolve_config_path()
        assert path.name == "config.toml"
        assert path.parent.name == "slate"

    def test_env_var_pointing_to_existing_file(self, tmp_path, monkeypatch):
        config_file = tmp_path / "custom.toml"
        config_file.write_text("[defaults]\n")
        monkeypatch.setenv(CONFIG_ENV_VAR, str(config_file))
        assert resolve_config_path() == config_file

    def test_env_var_pointing_to_missing_file_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv(CONFIG_ENV_VAR, str(tmp_path / "does-not-exist.toml"))
        with pytest.raises(FileNotFoundError):
            resolve_config_path()


class TestLoadConfig:
    def test_missing_file_returns_defaults(self, tmp_path):
        result = load_config(tmp_path / "nonexistent.toml")
        assert result == Config()

    def test_loads_overridden_values(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            """
            [defaults]
            prepend_generated_name = true
            prefix = "Boston, MA"
            suffix = "TAKE 2"
            generate_undo_script = false
            model = "some-other/model"
            prompt = "custom prompt text"
            max_file_name_length = 100
            """
        )
        result = load_config(config_file)
        assert result.prepend_generated_name is True
        assert result.prefix == "Boston, MA"
        assert result.suffix == "TAKE 2"
        assert result.generate_undo_script is False
        assert result.model == "some-other/model"
        assert result.prompt == "custom prompt text"
        assert result.max_file_name_length == 100

    def test_partial_overrides_fall_back_to_defaults(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            """
            [defaults]
            prefix = "Boston, MA"
            """
        )
        defaults = Config()
        result = load_config(config_file)
        assert result.prefix == "Boston, MA"
        assert result.prepend_generated_name == defaults.prepend_generated_name
        assert result.model == defaults.model
        assert result.prompt == defaults.prompt
        assert result.max_file_name_length == defaults.max_file_name_length

    def test_missing_defaults_table_returns_defaults(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("# empty config, no [defaults] table\n")
        assert load_config(config_file) == Config()
