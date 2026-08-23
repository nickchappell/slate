import pytest
from huggingface_hub.errors import LocalEntryNotFoundError

import slate.inference as inference


class FakeGenerationResult:
    def __init__(self, text: str):
        self.text = text


@pytest.fixture(autouse=True)
def clear_model_cache():
    # _load_model is lru_cache'd across the whole process -- reset between
    # tests so each test's fakes are actually exercised.
    inference._load_model.cache_clear()
    yield
    inference._load_model.cache_clear()


def _patch_generation_chain(monkeypatch, model_repo_to_path=None):
    """Bypasses huggingface_hub/mlx_vlm entirely -- model_repo_to_path lets a
    test control what _resolve_model_path returns (defaults to identity)."""
    resolve = model_repo_to_path or (lambda repo, **kwargs: repo)
    monkeypatch.setattr(inference, "_resolve_model_path", resolve)
    monkeypatch.setattr(
        inference, "vlm_load", lambda path: (path + "-model", path + "-processor")
    )
    monkeypatch.setattr(inference, "load_config", lambda path: {"path": path})
    monkeypatch.setattr(
        inference,
        "apply_chat_template",
        lambda processor, config, prompt, num_images: f"templated:{prompt}",
    )


class TestGenerateCaption:
    def test_wires_prompt_image_and_model_through_to_generate(self, monkeypatch):
        calls = {}
        _patch_generation_chain(monkeypatch)

        def fake_generate(model, processor, prompt, image, **kwargs):
            calls["model"] = model
            calls["processor"] = processor
            calls["prompt"] = prompt
            calls["image"] = image
            calls["kwargs"] = kwargs
            return FakeGenerationResult("a caption")

        monkeypatch.setattr(inference, "vlm_generate", fake_generate)

        result = inference.generate_caption(
            "/tmp/frame.jpg", "describe this", "some/model"
        )

        assert result == "a caption"
        assert calls["model"] == "some/model-model"
        assert calls["processor"] == "some/model-processor"
        assert calls["prompt"] == "templated:describe this"
        assert calls["image"] == "/tmp/frame.jpg"
        assert calls["kwargs"]["max_tokens"] == inference.MAX_CAPTION_TOKENS

    def test_model_is_loaded_once_and_cached_across_calls(self, monkeypatch):
        resolve_calls = []
        _patch_generation_chain(
            monkeypatch,
            model_repo_to_path=lambda repo, **kwargs: (
                resolve_calls.append(repo) or repo
            ),
        )
        monkeypatch.setattr(
            inference, "vlm_generate", lambda *a, **k: FakeGenerationResult("caption")
        )

        inference.generate_caption("/tmp/a.jpg", "prompt", "same/model")
        inference.generate_caption("/tmp/b.jpg", "prompt", "same/model")

        assert resolve_calls == ["same/model"]

    def test_different_models_are_loaded_separately(self, monkeypatch):
        resolve_calls = []
        _patch_generation_chain(
            monkeypatch,
            model_repo_to_path=lambda repo, **kwargs: (
                resolve_calls.append(repo) or repo
            ),
        )
        monkeypatch.setattr(
            inference, "vlm_generate", lambda *a, **k: FakeGenerationResult("caption")
        )

        inference.generate_caption("/tmp/a.jpg", "prompt", "model-a")
        inference.generate_caption("/tmp/b.jpg", "prompt", "model-b")

        assert resolve_calls == ["model-a", "model-b"]


class TestResolveModelPath:
    def test_uses_local_cache_without_network_when_check_for_updates_false(
        self, monkeypatch
    ):
        calls = []

        def fake_snapshot_download(*, repo_id, local_files_only, allow_patterns):
            calls.append(local_files_only)
            return "/cache/model-path"

        monkeypatch.setattr(inference, "snapshot_download", fake_snapshot_download)

        path = inference._resolve_model_path("some/model", check_for_updates=False)

        assert path == "/cache/model-path"
        assert calls == [True]  # only the offline/cache-only attempt was made

    def test_falls_back_to_network_when_not_cached(self, monkeypatch):
        calls = []

        def fake_snapshot_download(*, repo_id, local_files_only, allow_patterns):
            calls.append(local_files_only)
            if local_files_only:
                raise LocalEntryNotFoundError("not cached")
            return "/downloaded/model-path"

        monkeypatch.setattr(inference, "snapshot_download", fake_snapshot_download)

        path = inference._resolve_model_path("some/model", check_for_updates=False)

        assert path == "/downloaded/model-path"
        assert calls == [True, False]

    def test_check_for_updates_skips_the_cache_only_attempt(self, monkeypatch):
        calls = []

        def fake_snapshot_download(*, repo_id, local_files_only, allow_patterns):
            calls.append(local_files_only)
            return "/refreshed/model-path"

        monkeypatch.setattr(inference, "snapshot_download", fake_snapshot_download)

        path = inference._resolve_model_path("some/model", check_for_updates=True)

        assert path == "/refreshed/model-path"
        assert calls == [False]  # went straight to the network-enabled call


class TestCheckForModelUpdates:
    def test_reports_not_updated_when_path_unchanged(self, monkeypatch):
        monkeypatch.setattr(
            inference,
            "snapshot_download",
            lambda *, repo_id, local_files_only, allow_patterns: "/same/path",
        )

        updated, path = inference.check_for_model_updates("some/model")

        assert updated is False
        assert path == "/same/path"

    def test_reports_updated_when_not_previously_cached(self, monkeypatch):
        def fake_snapshot_download(*, repo_id, local_files_only, allow_patterns):
            if local_files_only:
                raise LocalEntryNotFoundError("not cached")
            return "/newly-downloaded/path"

        monkeypatch.setattr(inference, "snapshot_download", fake_snapshot_download)

        updated, path = inference.check_for_model_updates("some/model")

        assert updated is True
        assert path == "/newly-downloaded/path"

    def test_reports_updated_when_a_newer_snapshot_is_fetched(self, monkeypatch):
        def fake_snapshot_download(*, repo_id, local_files_only, allow_patterns):
            return "/old/path" if local_files_only else "/new/path"

        monkeypatch.setattr(inference, "snapshot_download", fake_snapshot_download)

        updated, path = inference.check_for_model_updates("some/model")

        assert updated is True
        assert path == "/new/path"
