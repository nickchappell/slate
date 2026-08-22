from slate.filenames import assemble_stem, normalize_caption, truncate_caption


class TestNormalizeCaption:
    def test_collapses_whitespace_and_newlines(self):
        assert normalize_caption("  waves   crashing\non  rocks  ") == "waves crashing on rocks"

    def test_replaces_slash_with_space(self):
        assert normalize_caption("before/after shot") == "before after shot"

    def test_replaces_nul_byte_with_space(self):
        assert normalize_caption("waves\0crashing") == "waves crashing"

    def test_strips_surrounding_double_quotes(self):
        assert normalize_caption('"waves crashing on rocks"') == "waves crashing on rocks"

    def test_strips_surrounding_single_quotes(self):
        assert normalize_caption("'waves crashing on rocks'") == "waves crashing on rocks"

    def test_does_not_strip_mismatched_quotes(self):
        assert normalize_caption("'waves crashing\"") == "'waves crashing\""

    def test_lowercases(self):
        assert normalize_caption("Waves Crashing") == "waves crashing"

    def test_strips_trailing_sentence_punctuation(self):
        assert normalize_caption("waves crashing on rocks.") == "waves crashing on rocks"
        assert normalize_caption("waves crashing on rocks!") == "waves crashing on rocks"
        assert normalize_caption("waves crashing on rocks?") == "waves crashing on rocks"
        assert normalize_caption("waves crashing on rocks,") == "waves crashing on rocks"

    def test_does_not_strip_internal_punctuation(self):
        assert normalize_caption("waves, then rocks.") == "waves, then rocks"

    def test_slash_newline_and_lowercase_combine(self):
        raw = "  Waves/Crashing\non ROCKS  "
        assert normalize_caption(raw) == "waves crashing on rocks"

    def test_quote_strip_requires_matching_last_character(self):
        # Quote-stripping checks text[0] == text[-1] *before* trailing
        # punctuation is stripped -- a quote followed by trailing punctuation
        # (rather than a matching closing quote) means the leading quote is
        # left in place, since '"' != '.' at the point that check runs.
        assert normalize_caption('"waves crashing.') == '"waves crashing'


class TestTruncateCaption:
    def test_under_cap_is_unchanged(self):
        assert truncate_caption("short caption", max_length=70) == "short caption"

    def test_exactly_at_cap_is_unchanged(self):
        caption = "x" * 70
        assert truncate_caption(caption, max_length=70) == caption

    def test_over_cap_truncates_at_word_boundary(self):
        caption = "one two three four five six seven eight nine ten eleven twelve"
        truncated = truncate_caption(caption, max_length=20)
        assert truncated == "one two three four"
        assert len(truncated) <= 20

    def test_no_ellipsis_appended(self):
        caption = "one two three four five six seven eight nine ten"
        truncated = truncate_caption(caption, max_length=10)
        assert "..." not in truncated

    def test_never_cuts_mid_word_when_space_available(self):
        truncated = truncate_caption("abcdefgh ijklmnop", max_length=12)
        assert truncated == "abcdefgh"

    def test_hard_cuts_when_no_space_available(self):
        # No whitespace boundary before max_length -- falls back to a hard
        # cut rather than returning something over the limit.
        truncated = truncate_caption("abcdefghijklmnop", max_length=5)
        assert truncated == "abcde"


class TestAssembleStem:
    def test_append_mode_default_order(self):
        stem = assemble_stem(original_stem="A017_C010", caption="waves crashing")
        assert stem == "A017_C010 waves crashing"

    def test_prepend_mode_order(self):
        stem = assemble_stem(
            original_stem="A017_C010", caption="waves crashing", prepend_caption=True
        )
        assert stem == "waves crashing A017_C010"

    def test_prefix_and_suffix_append_mode(self):
        stem = assemble_stem(
            original_stem="A017_C010",
            caption="waves crashing",
            prefix="Boston, MA",
            suffix="TAKE 2",
        )
        assert stem == "Boston, MA A017_C010 waves crashing TAKE 2"

    def test_prefix_and_suffix_prepend_mode(self):
        stem = assemble_stem(
            original_stem="A017_C010",
            caption="waves crashing",
            prefix="Boston, MA",
            suffix="TAKE 2",
            prepend_caption=True,
        )
        assert stem == "Boston, MA waves crashing A017_C010 TAKE 2"

    def test_empty_prefix_suffix_omitted_not_double_spaced(self):
        stem = assemble_stem(original_stem="A017_C010", caption="waves crashing")
        assert "  " not in stem

    def test_prefix_suffix_whitespace_trimmed(self):
        stem = assemble_stem(
            original_stem="A017_C010",
            caption="waves crashing",
            prefix="  Boston, MA  ",
            suffix="  TAKE 2  ",
        )
        assert stem == "Boston, MA A017_C010 waves crashing TAKE 2"

    def test_overflow_truncates_caption_not_original_stem_append_mode(self):
        stem = assemble_stem(
            original_stem="A017_C010",
            caption="a very long caption that describes a lot of things happening",
            max_length=30,
        )
        assert stem.startswith("A017_C010")
        assert len(stem) <= 29

    def test_overflow_truncates_caption_not_original_stem_prepend_mode(self):
        # This is the bug the spec called out explicitly: truncating the
        # tail of the whole assembled string would eat into original_stem
        # when the caption is prepended. The caption must be shortened
        # specifically, regardless of position.
        stem = assemble_stem(
            original_stem="A017_C010",
            caption="a very long caption that describes a lot of things happening",
            prepend_caption=True,
            max_length=30,
        )
        assert stem.endswith("A017_C010")
        assert len(stem) <= 29

    def test_overflow_preserves_suffix_in_prepend_mode(self):
        stem = assemble_stem(
            original_stem="A017_C010",
            caption="a very long caption that describes a lot of things happening",
            prepend_caption=True,
            suffix="TAKE 2",
            max_length=40,
        )
        assert stem.endswith("A017_C010 TAKE 2")

    def test_pathological_non_caption_overflow_hard_caps(self):
        # Even with the caption truncated to nothing, prefix+original_stem+
        # suffix alone still exceed max_length -- must not raise, must
        # respect max_length as a hard guarantee.
        stem = assemble_stem(
            original_stem="x" * 50,
            caption="waves crashing",
            prefix="y" * 50,
            suffix="z" * 50,
            max_length=30,
        )
        assert len(stem) <= 29
