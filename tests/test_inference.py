import pytest

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


class TestGenerateCaption:
    def test_wires_prompt_image_and_model_through_to_generate(self, monkeypatch):
        calls = {}

        monkeypatch.setattr(inference, "vlm_load", lambda repo: (repo + "-model", repo + "-processor"))
        monkeypatch.setattr(inference, "load_config", lambda repo: {"repo": repo})
        monkeypatch.setattr(
            inference,
            "apply_chat_template",
            lambda processor, config, prompt, num_images: f"templated:{prompt}",
        )

        def fake_generate(model, processor, prompt, image, **kwargs):
            calls["model"] = model
            calls["processor"] = processor
            calls["prompt"] = prompt
            calls["image"] = image
            calls["kwargs"] = kwargs
            return FakeGenerationResult("a caption")

        monkeypatch.setattr(inference, "vlm_generate", fake_generate)

        result = inference.generate_caption("/tmp/frame.jpg", "describe this", "some/model")

        assert result == "a caption"
        assert calls["model"] == "some/model-model"
        assert calls["processor"] == "some/model-processor"
        assert calls["prompt"] == "templated:describe this"
        assert calls["image"] == "/tmp/frame.jpg"
        assert calls["kwargs"]["max_tokens"] == inference.MAX_CAPTION_TOKENS

    def test_model_is_loaded_once_and_cached_across_calls(self, monkeypatch):
        load_calls = []

        def fake_load(repo):
            load_calls.append(repo)
            return ("model", "processor")

        monkeypatch.setattr(inference, "vlm_load", fake_load)
        monkeypatch.setattr(inference, "load_config", lambda repo: {})
        monkeypatch.setattr(
            inference, "apply_chat_template", lambda processor, config, prompt, num_images: prompt
        )
        monkeypatch.setattr(
            inference, "vlm_generate", lambda *a, **k: FakeGenerationResult("caption")
        )

        inference.generate_caption("/tmp/a.jpg", "prompt", "same/model")
        inference.generate_caption("/tmp/b.jpg", "prompt", "same/model")

        assert load_calls == ["same/model"]

    def test_different_models_are_loaded_separately(self, monkeypatch):
        load_calls = []

        def fake_load(repo):
            load_calls.append(repo)
            return ("model", "processor")

        monkeypatch.setattr(inference, "vlm_load", fake_load)
        monkeypatch.setattr(inference, "load_config", lambda repo: {})
        monkeypatch.setattr(
            inference, "apply_chat_template", lambda processor, config, prompt, num_images: prompt
        )
        monkeypatch.setattr(
            inference, "vlm_generate", lambda *a, **k: FakeGenerationResult("caption")
        )

        inference.generate_caption("/tmp/a.jpg", "prompt", "model-a")
        inference.generate_caption("/tmp/b.jpg", "prompt", "model-b")

        assert load_calls == ["model-a", "model-b"]
