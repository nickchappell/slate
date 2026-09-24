import pytest
from huggingface_hub.errors import LocalEntryNotFoundError

import slate.inference as inference
from slate.inference import (
    CaptionSections,
    derive_short_from_keywords,
    derive_short_from_long,
    parse_caption_sections,
)


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
            ["/tmp/frame.jpg"], "describe this", "some/model"
        )

        assert result == "a caption"
        assert calls["model"] == "some/model-model"
        assert calls["processor"] == "some/model-processor"
        assert calls["prompt"] == "templated:describe this"
        assert calls["image"] == ["/tmp/frame.jpg"]
        assert calls["kwargs"]["max_tokens"] == inference.MAX_CAPTION_TOKENS

    def test_explicit_max_tokens_is_forwarded_to_generate(self, monkeypatch):
        calls = {}
        _patch_generation_chain(monkeypatch)

        def fake_generate(model, processor, prompt, image, **kwargs):
            calls["kwargs"] = kwargs
            return FakeGenerationResult("x")

        monkeypatch.setattr(inference, "vlm_generate", fake_generate)

        inference.generate_caption(
            ["/tmp/frame.jpg"], "prompt", "some/model", max_tokens=140
        )

        assert calls["kwargs"]["max_tokens"] == 140

    def test_passes_num_images_matching_frame_count(self, monkeypatch):
        _patch_generation_chain(monkeypatch)
        num_images_seen = {}

        def fake_apply_chat_template(processor, config, prompt, num_images):
            num_images_seen["value"] = num_images
            return f"templated:{prompt}"

        monkeypatch.setattr(inference, "apply_chat_template", fake_apply_chat_template)
        monkeypatch.setattr(
            inference,
            "vlm_generate",
            lambda *a, **k: FakeGenerationResult("a caption"),
        )

        inference.generate_caption(
            ["/tmp/a.jpg", "/tmp/b.jpg", "/tmp/c.jpg"], "describe this", "some/model"
        )

        assert num_images_seen["value"] == 3

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

        inference.generate_caption(["/tmp/a.jpg"], "prompt", "same/model")
        inference.generate_caption(["/tmp/b.jpg"], "prompt", "same/model")

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

        inference.generate_caption(["/tmp/a.jpg"], "prompt", "model-a")
        inference.generate_caption(["/tmp/b.jpg"], "prompt", "model-b")

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


class TestParseCaptionSections:
    def test_well_formed_three_section_response(self):
        raw = (
            "SHORT: red kayak at sunset\n"
            "LONG: A red kayak drifts across a calm lake at sunset.\n"
            "KEYWORDS: kayak, lake, sunset, red, calm"
        )
        result = parse_caption_sections(raw)
        assert result == CaptionSections(
            short="red kayak at sunset",
            long="A red kayak drifts across a calm lake at sunset.",
            keywords=["kayak", "lake", "sunset", "red", "calm"],
        )

    def test_per_keyword_quotes_are_stripped(self):
        # The model sometimes wraps each keyword in its own quote marks
        # even though the prompt asks for a bare comma list -- see the
        # real-world example this was reported against.
        raw = (
            "SHORT: train in urban setting\n"
            "LONG: A train is parked in an urban setting.\n"
            'KEYWORDS: "train", "urban setting", "graffiti", "flag"'
        )
        result = parse_caption_sections(raw)
        assert result.keywords == ["train", "urban setting", "graffiti", "flag"]

    def test_whole_list_quotes_are_stripped(self):
        # The model sometimes wraps the *entire* comma list in a single
        # pair of quotes rather than quoting each keyword -- naive
        # per-keyword stripping (only matched-quote-pair-per-entry) leaves
        # the quotes stuck to the first/last keyword after the comma split
        # (e.g. `"Historic Building` / `Exterior"` verbatim in JSON output)
        # -- see the real-world example this was reported against.
        raw = (
            "SHORT: historic building\n"
            "LONG: A historic building with columns and large windows.\n"
            'KEYWORDS: "Historic Building, Columns, Large Windows, Trees, Exterior"'
        )
        result = parse_caption_sections(raw)
        assert result.keywords == [
            "Historic Building",
            "Columns",
            "Large Windows",
            "Trees",
            "Exterior",
        ]

    def test_missing_keywords_falls_back_to_derivation_from_long(self):
        raw = "SHORT: red kayak\nLONG: A red kayak drifts across a calm lake."
        result = parse_caption_sections(raw)
        assert result.short == "red kayak"
        assert result.long == "A red kayak drifts across a calm lake."
        # Fallback-derived, not None -- see _derive_keywords_from_long.
        assert result.keywords == ["red", "kayak", "drifts", "calm", "lake"]

    def test_malformed_keywords_falls_back_to_derivation(self):
        # A full sentence instead of a comma list -- the "most likely to
        # break format" case the spec calls out.
        raw = (
            "SHORT: red kayak\n"
            "LONG: A red kayak drifts across a calm lake.\n"
            "KEYWORDS: There are many things happening in this scene."
        )
        result = parse_caption_sections(raw)
        assert result.keywords == ["red", "kayak", "drifts", "calm", "lake"]

    def test_two_section_response_has_no_short(self):
        # The --metadata-backfill prompt omits SHORT entirely.
        raw = (
            "LONG: Seagulls squabble over a dropped fry.\n"
            "KEYWORDS: seagulls, fry, birds"
        )
        result = parse_caption_sections(raw)
        assert result.short is None
        assert result.long == "Seagulls squabble over a dropped fry."
        assert result.keywords == ["seagulls", "fry", "birds"]

    def test_markers_out_of_order_still_parse(self):
        raw = (
            "KEYWORDS: kayak, sunset\n"
            "SHORT: red kayak\n"
            "LONG: A red kayak drifts across a calm lake."
        )
        result = parse_caption_sections(raw)
        assert result.short == "red kayak"
        assert result.long == "A red kayak drifts across a calm lake."
        assert result.keywords == ["kayak", "sunset"]

    def test_garbage_input_degrades_gracefully(self):
        result = parse_caption_sections("completely unstructured text, no markers")
        assert result == CaptionSections(short=None, long=None, keywords=None)

    def test_empty_input_degrades_gracefully(self):
        result = parse_caption_sections("")
        assert result == CaptionSections(short=None, long=None, keywords=None)

    def test_lowercase_markers_still_parse(self):
        # A quantized model observed emitting "short:"/"long:"/"keywords:"
        # instead of the prompted uppercase form -- previously this missed
        # every marker entirely, leaving `.short` None and letting the
        # full raw multi-section text leak into the caller's filename.
        raw = (
            "short: red kayak at sunset\n"
            "long: A red kayak drifts across a calm lake at sunset.\n"
            "keywords: kayak, lake, sunset"
        )
        result = parse_caption_sections(raw)
        assert result == CaptionSections(
            short="red kayak at sunset",
            long="A red kayak drifts across a calm lake at sunset.",
            keywords=["kayak", "lake", "sunset"],
        )

    def test_mixed_case_markers_still_parse(self):
        raw = "Short: red kayak\nLong: A red kayak drifts across a calm lake."
        result = parse_caption_sections(raw)
        assert result.short == "red kayak"
        assert result.long == "A red kayak drifts across a calm lake."

    def test_verbatim_placeholder_echo_treated_as_absent(self):
        # The model echoed the prompt's own <placeholder> text back for
        # every section instead of filling any of them in.
        raw = (
            "SHORT: <3-6 words, for a filename>\n"
            "LONG: <one to two sentences>\n"
            "KEYWORDS: <6-10 comma-separated single words or short phrases "
            "naming subjects, actions, and setting -- no articles, no full "
            "sentences>"
        )
        result = parse_caption_sections(raw)
        assert result.short is None
        assert result.long is None
        assert result.keywords is None

    def test_truncated_placeholder_echo_treated_as_absent(self):
        # Observed real-world case: the model echoes only a leading
        # fragment of the placeholder, with the brackets already gone.
        raw = "SHORT: 3-6 words\nLONG: a serene forest path with trees"
        result = parse_caption_sections(raw)
        assert result.short is None
        assert result.long == "a serene forest path with trees"

    def test_paraphrased_count_echo_treated_as_absent(self):
        # Observed real-world case: LONG's placeholder ("one to two
        # sentences") echoed back with digits substituted in, which has no
        # literal substring in common with the placeholder text itself.
        raw = "SHORT: 3-6 words\nLONG: 1-2 sentences\nKEYWORDS: vineyard, trees, fog"
        result = parse_caption_sections(raw)
        assert result.short is None
        assert result.long is None
        assert result.keywords == ["vineyard", "trees", "fog"]

    def test_real_content_resembling_placeholder_prefix_is_not_over_matched(self):
        # Sanity check: ordinary short captions aren't accidentally caught
        # by the placeholder-prefix check just because of shared words.
        raw = "SHORT: two boats on a lake\nLONG: Two boats drift on a lake."
        result = parse_caption_sections(raw)
        assert result.short == "two boats on a lake"
        assert result.long == "Two boats drift on a lake."


class TestDeriveShortFromLong:
    def test_takes_first_max_words(self):
        long_caption = "A red kayak drifts across a calm lake at sunset."
        assert derive_short_from_long(long_caption, max_words=6) == (
            "A red kayak drifts across a"
        )

    def test_shorter_than_max_words_is_unchanged(self):
        assert derive_short_from_long("A red kayak.", max_words=6) == "A red kayak."


class TestDeriveShortFromKeywords:
    def test_takes_first_max_keywords(self):
        keywords = ["vineyard", "trees", "fog", "rows", "green"]
        assert (
            derive_short_from_keywords(keywords, max_keywords=3) == "vineyard trees fog"
        )

    def test_fewer_than_max_keywords_is_unchanged(self):
        assert derive_short_from_keywords(["vineyard", "fog"], max_keywords=4) == (
            "vineyard fog"
        )


class TestCleanKeyword:
    def test_strips_surrounding_double_quotes(self):
        assert inference._clean_keyword('"train"') == "train"

    def test_strips_surrounding_single_quotes(self):
        assert inference._clean_keyword("'urban setting'") == "urban setting"

    def test_strips_whitespace_around_quotes(self):
        assert inference._clean_keyword('  "train"  ') == "train"

    def test_strips_whitespace_inside_quotes(self):
        assert inference._clean_keyword('" train "') == "train"

    def test_leaves_unquoted_keyword_unchanged(self):
        assert inference._clean_keyword("train") == "train"

    def test_does_not_strip_mismatched_quotes(self):
        assert inference._clean_keyword("'train\"") == "'train\""

    def test_lone_quote_character_is_not_stripped_to_empty(self):
        # len < 2, so the "surrounding pair" check can't apply.
        assert inference._clean_keyword('"') == '"'


class TestStripWholeListQuotes:
    def test_strips_quotes_wrapping_the_entire_list(self):
        raw = '"Historic Building, Columns, Large Windows, Trees, Exterior"'
        assert inference._strip_whole_list_quotes(raw) == (
            "Historic Building, Columns, Large Windows, Trees, Exterior"
        )

    def test_leaves_per_keyword_quoting_untouched(self):
        # Each keyword is individually quoted -- both the first and last
        # tokens are already a balanced quote pair on their own, so this
        # must be left for _clean_keyword() to strip per-entry, not
        # treated as one quote pair wrapping the whole string.
        raw = '"train", "urban setting", "graffiti", "flag"'
        assert inference._strip_whole_list_quotes(raw) == raw

    def test_leaves_unquoted_list_untouched(self):
        assert inference._strip_whole_list_quotes("train, urban setting") == (
            "train, urban setting"
        )

    def test_single_quoted_keyword_no_comma_is_left_for_clean_keyword(self):
        # No comma at all -- first and last token are the same (balanced)
        # string, so this is per-entry quoting of a single keyword.
        assert inference._strip_whole_list_quotes('"train"') == '"train"'


class TestDeriveKeywordsFromLong:
    def test_drops_stopwords(self):
        keywords = inference._derive_keywords_from_long(
            "A red kayak drifts across the calm lake"
        )
        assert "a" not in keywords
        assert "the" not in keywords
        assert "across" not in keywords
        assert "red" in keywords
        assert "kayak" in keywords

    def test_dedupes_preserving_first_occurrence_order(self):
        keywords = inference._derive_keywords_from_long("lake lake kayak lake")
        assert keywords == ["lake", "kayak"]

    def test_caps_at_max_keywords(self):
        words = " ".join(f"word{i}" for i in range(20))
        keywords = inference._derive_keywords_from_long(words, max_keywords=5)
        assert len(keywords) == 5

    def test_lowercases_and_strips_punctuation(self):
        keywords = inference._derive_keywords_from_long("Kayak, Lake! Sunset.")
        assert keywords == ["kayak", "lake", "sunset"]

    def test_cannot_invent_words_not_literally_in_the_text(self):
        # Deliberate limitation the spec calls out: the model asked directly
        # can name concepts it saw but never wrote (e.g. "recreation"), but
        # a mechanical word-strip of the caption text can never recover
        # anything not literally present in it.
        keywords = inference._derive_keywords_from_long(
            "A person paddles a small boat on the water"
        )
        assert "recreation" not in keywords
        assert "watercraft" not in keywords
