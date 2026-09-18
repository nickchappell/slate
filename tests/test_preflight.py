import platform
import shutil

from slate.preflight import run_metadata_tool_checks, run_preflight_checks


def make_which(available: set[str]):
    return lambda name: f"/usr/bin/{name}" if name in available else None


ALL_TOOLS = {"ffmpeg", "ffprobe", "qlmanage", "sips", "exiftool"}
ALL_BENTO4_TOOLS = {"mp4dump", "mp4extract", "mp4edit"}


class TestRunPreflightChecks:
    def test_all_pass_returns_no_failures(self, monkeypatch):
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        monkeypatch.setattr(platform, "machine", lambda: "arm64")
        monkeypatch.setattr(shutil, "which", make_which(ALL_TOOLS))
        assert run_preflight_checks() == []

    def test_non_macos_reports_failure(self, monkeypatch):
        monkeypatch.setattr(platform, "system", lambda: "Linux")
        monkeypatch.setattr(platform, "machine", lambda: "arm64")
        monkeypatch.setattr(shutil, "which", make_which(ALL_TOOLS))
        failures = run_preflight_checks()
        assert any("macOS" in f for f in failures)

    def test_intel_mac_reports_apple_silicon_failure(self, monkeypatch):
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        monkeypatch.setattr(platform, "machine", lambda: "x86_64")
        monkeypatch.setattr(shutil, "which", make_which(ALL_TOOLS))
        failures = run_preflight_checks()
        assert any("Apple Silicon" in f for f in failures)

    def test_missing_ffmpeg_reports_failure_with_brew_hint(self, monkeypatch):
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        monkeypatch.setattr(platform, "machine", lambda: "arm64")
        monkeypatch.setattr(shutil, "which", make_which(ALL_TOOLS - {"ffmpeg"}))
        failures = run_preflight_checks()
        assert any("ffmpeg" in f and "brew install" in f for f in failures)

    def test_missing_ffprobe_reports_failure(self, monkeypatch):
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        monkeypatch.setattr(platform, "machine", lambda: "arm64")
        monkeypatch.setattr(shutil, "which", make_which(ALL_TOOLS - {"ffprobe"}))
        failures = run_preflight_checks()
        assert any("ffprobe" in f for f in failures)

    def test_missing_qlmanage_reports_failure(self, monkeypatch):
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        monkeypatch.setattr(platform, "machine", lambda: "arm64")
        monkeypatch.setattr(shutil, "which", make_which(ALL_TOOLS - {"qlmanage"}))
        failures = run_preflight_checks()
        assert any("qlmanage" in f for f in failures)

    def test_missing_sips_reports_failure(self, monkeypatch):
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        monkeypatch.setattr(platform, "machine", lambda: "arm64")
        monkeypatch.setattr(shutil, "which", make_which(ALL_TOOLS - {"sips"}))
        failures = run_preflight_checks()
        assert any("sips" in f for f in failures)

    def test_missing_exiftool_reports_failure_with_brew_hint(self, monkeypatch):
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        monkeypatch.setattr(platform, "machine", lambda: "arm64")
        monkeypatch.setattr(shutil, "which", make_which(ALL_TOOLS - {"exiftool"}))
        failures = run_preflight_checks()
        assert any("exiftool" in f and "brew install" in f for f in failures)

    def test_all_failures_reported_together_not_fail_fast(self, monkeypatch):
        monkeypatch.setattr(platform, "system", lambda: "Linux")
        monkeypatch.setattr(platform, "machine", lambda: "x86_64")
        monkeypatch.setattr(shutil, "which", make_which(set()))
        failures = run_preflight_checks()
        assert len(failures) == 7

    def test_ignores_bento4_availability(self, monkeypatch):
        # Bento4 is checked separately, by run_metadata_tool_checks() below
        # (gated on --add-metadata/--metadata-backfill by the caller) --
        # run_preflight_checks() must never fail on its absence.
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        monkeypatch.setattr(platform, "machine", lambda: "arm64")
        monkeypatch.setattr(shutil, "which", make_which(ALL_TOOLS))
        assert run_preflight_checks() == []


class TestRunMetadataToolChecks:
    def test_all_present_returns_no_failures(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", make_which(ALL_BENTO4_TOOLS))
        assert run_metadata_tool_checks() == []

    def test_missing_mp4dump_reports_failure_with_brew_hint(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", make_which(ALL_BENTO4_TOOLS - {"mp4dump"}))
        failures = run_metadata_tool_checks()
        assert any("mp4dump" in f and "brew install bento4" in f for f in failures)

    def test_missing_mp4extract_reports_failure(self, monkeypatch):
        monkeypatch.setattr(
            shutil, "which", make_which(ALL_BENTO4_TOOLS - {"mp4extract"})
        )
        failures = run_metadata_tool_checks()
        assert any("mp4extract" in f for f in failures)

    def test_missing_mp4edit_reports_failure(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", make_which(ALL_BENTO4_TOOLS - {"mp4edit"}))
        failures = run_metadata_tool_checks()
        assert any("mp4edit" in f for f in failures)

    def test_none_present_reports_all_three_together(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", make_which(set()))
        assert len(run_metadata_tool_checks()) == 3
