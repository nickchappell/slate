import functools
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from slate import metadata

FOOTAGE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "footage"
KINO_FIXTURE = FOOTAGE_DIR / "iphone_17_pro_kino_prores_422.mov"
MOMENTPRO_FIXTURE = FOOTAGE_DIR / "iphone_17_pro_momentpro_prores_422.MOV"

# See spec/metadata-write-corruption.md: these two fixtures are the closest
# thing to a controlled minimal pair available -- same iPhone 17 Pro, same
# ProRes 422 video encoder, different third-party camera app. Kino hits a
# real exiftool parser bug on its top-level `meta` atom and carries a
# `mebx` timed-GPS track; Moment Pro hits neither. These tests exercise
# that contrast directly against the real fixtures/real tools, rather than
# the mocked-subprocess unit tests in tests/test_metadata.py
# (TestEmbedMetadataFfmpegKeysFamilySplit), which cover the same logic but
# can't catch a real tool disagreeing with its own docs (as ffprobe did
# during the investigation this doc records).


def _has_exiftool() -> bool:
    return shutil.which("exiftool") is not None


def _has_bento4() -> bool:
    return all(shutil.which(b) for b in ("mp4dump", "mp4extract", "mp4edit"))


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _has_exiftool(),
        reason="exiftool not found on PATH -- see preflight.py's binary checks",
    ),
]


def _ffprobe_json(path: Path, *extra_args: str) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", *extra_args, str(path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def _format_tags(path: Path) -> dict:
    return _ffprobe_json(path, "-show_format")["format"].get("tags", {})


def _data_stream_codec_tag(path: Path) -> str | None:
    """The codec_tag_string of the file's `data` (mebx-shaped) stream, or
    None if it has no such stream -- e.g. Moment Pro, which never records
    one."""
    streams = _ffprobe_json(path, "-show_streams")["streams"]
    data_streams = [s for s in streams if s.get("codec_type") == "data"]
    return data_streams[0].get("codec_tag_string") if data_streams else None


def _embed_test_metadata(path: Path) -> metadata.EmbedOutcome:
    return metadata.embed_metadata(
        path,
        title="quirk test title",
        description="quirk test description",
        keywords=["quirk", "test"],
        original_filename=path.name,
        app_version="test",
        caption_model="test",
        generated_at="2026-09-17T00:00:00Z",
    )


@pytest.mark.momentpro
@pytest.mark.skipif(
    not MOMENTPRO_FIXTURE.is_file(),
    reason=f"{MOMENTPRO_FIXTURE} not present -- see tests/fixtures/footage/README.md",
)
def test_momentpro_write_succeeds_without_ffmpeg_fallback(tmp_path, monkeypatch):
    """Moment Pro's meta atom doesn't trigger the exiftool bug -- the
    combined write should succeed on its own, and
    _ffmpeg_write_keys_family() (the Kino-only recovery path) should never
    run. See "Follow-up: is this bug specific to Kino..." in
    spec/metadata-write-corruption.md."""
    path = tmp_path / MOMENTPRO_FIXTURE.name
    shutil.copyfile(MOMENTPRO_FIXTURE, path)
    before_tags = _format_tags(path)

    called = False

    def _spy(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("_ffmpeg_write_keys_family should not run for Moment Pro")

    monkeypatch.setattr(metadata, "_ffmpeg_write_keys_family", _spy)

    outcome = _embed_test_metadata(path)

    assert outcome.embedded is True
    assert outcome.error is None
    assert called is False

    after_tags = _format_tags(path)
    for key in (
        "com.apple.quicktime.make",
        "com.apple.quicktime.model",
        "com.apple.quicktime.software",
        "com.apple.quicktime.location.ISO6709",
    ):
        assert after_tags[key] == before_tags[key]


@pytest.mark.momentpro
@pytest.mark.skipif(
    not MOMENTPRO_FIXTURE.is_file(),
    reason=f"{MOMENTPRO_FIXTURE} not present -- see tests/fixtures/footage/README.md",
)
def test_momentpro_has_no_mebx_track():
    """Sanity check on the fixture itself, guarding against drift: Moment
    Pro records no timed-metadata track at all, unlike Kino -- so the
    Bento4 repair path has nothing to engage with for this app (confirmed
    in the design doc's minimal-pair follow-up)."""
    assert _data_stream_codec_tag(MOMENTPRO_FIXTURE) is None


@pytest.mark.kino
@pytest.mark.skipif(
    not KINO_FIXTURE.is_file(),
    reason=f"{KINO_FIXTURE} not present -- see tests/fixtures/footage/README.md",
)
def test_kino_write_triggers_ffmpeg_fallback_and_preserves_vendor_tags(
    tmp_path, monkeypatch
):
    """Kino's meta atom hits the real exiftool "Terminator found in Meta"
    bug on every write -- the combined exiftool call should fail,
    _ffmpeg_write_keys_family() should take over the Keys/mdta family, and
    the five original com.apple.quicktime.* vendor tags must survive
    untouched (the design principle spec/metadata-write-corruption.md
    opens with)."""
    path = tmp_path / KINO_FIXTURE.name
    shutil.copyfile(KINO_FIXTURE, path)
    before_tags = _format_tags(path)

    calls = []
    real_fallback = metadata._ffmpeg_write_keys_family

    @functools.wraps(real_fallback)
    def _spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real_fallback(*args, **kwargs)

    monkeypatch.setattr(metadata, "_ffmpeg_write_keys_family", _spy)

    outcome = _embed_test_metadata(path)

    assert outcome.embedded is True
    assert outcome.error is None
    assert len(calls) == 1

    after_tags = _format_tags(path)
    for key in (
        "com.apple.quicktime.make",
        "com.apple.quicktime.model",
        "com.apple.quicktime.software",
        "com.apple.quicktime.location.ISO6709",
        "com.apple.quicktime.creationdate",
    ):
        assert after_tags[key] == before_tags[key]

    assert after_tags["com.apple.quicktime.title"] == "quirk test title"


@pytest.mark.kino
@pytest.mark.skipif(
    not KINO_FIXTURE.is_file(),
    reason=f"{KINO_FIXTURE} not present -- see tests/fixtures/footage/README.md",
)
@pytest.mark.skipif(
    not _has_bento4(),
    reason="bento4 (mp4dump/mp4extract/mp4edit) not found on PATH -- "
    "see metadata.py's mebx-repair comment; brew install bento4",
)
def test_kino_mebx_track_is_repaired_after_embed(tmp_path):
    """ffmpeg's mov muxer mislabels Kino's mebx timed-GPS track as `stts`
    during the Keys/mdta remux; _repair_mislabeled_data_tracks() should
    splice the real hdlr/stsd atoms back in from the pre-remux file so the
    track still reads as `mebx` afterward. See "Follow-up: fixing the mebx
    mislabeling with Bento4" in spec/metadata-write-corruption.md."""
    path = tmp_path / KINO_FIXTURE.name
    shutil.copyfile(KINO_FIXTURE, path)
    assert _data_stream_codec_tag(path) == "mebx"

    outcome = _embed_test_metadata(path)
    assert outcome.embedded is True

    assert _data_stream_codec_tag(path) == "mebx"


@pytest.mark.kino
@pytest.mark.skipif(
    not KINO_FIXTURE.is_file(),
    reason=f"{KINO_FIXTURE} not present -- see tests/fixtures/footage/README.md",
)
def test_kino_mebx_track_stays_mislabeled_without_bento4(tmp_path, monkeypatch):
    """Bento4 is optional and not preflight-checked (see CLAUDE.md's
    metadata.py entry): when it's unavailable, _dump_trak_stsd_fourccs()
    returns None and the repair silently no-ops, per its own documented
    contract -- the Keys/mdta write must still succeed even though the
    mebx track is left mislabeled. Simulates "Bento4 not installed" by
    patching that one detection call rather than hiding real binaries from
    PATH, since every other assertion here still needs a real ffmpeg
    remux to have happened."""
    path = tmp_path / KINO_FIXTURE.name
    shutil.copyfile(KINO_FIXTURE, path)

    monkeypatch.setattr(metadata, "_dump_trak_stsd_fourccs", lambda _path: None)

    outcome = _embed_test_metadata(path)

    assert outcome.embedded is True
    assert _data_stream_codec_tag(path) == "stts"
