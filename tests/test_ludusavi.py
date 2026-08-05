"""Tests for Ludusavi binary discovery and per-platform asset selection.

These invoke no binary, so they must stay out of test_cloud_cas.py, which is
skipped whole whenever rclone is missing - which is every CI run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from game_save_genie import ludusavi

# Copied from a real Ludusavi release. Note what is NOT here: no architecture
# appears in any name, so a wrong-arch download cannot be caught by name the
# way rclone's could. Upstream builds x86_64 Linux and arm64 macOS only.
_RELEASE = {
    "assets": [
        {"name": "ludusavi-v0.31.0-legal.zip"},
        {"name": "ludusavi-v0.31.0-linux.tar.gz"},
        {"name": "ludusavi-v0.31.0-mac.tar.gz"},
        {"name": "ludusavi-v0.31.0-win32.zip"},
        {"name": "ludusavi-v0.31.0-win64.zip"},
    ]
}


def _platform(monkeypatch: pytest.MonkeyPatch, system: str, machine: str) -> None:
    import platform as platform_module

    monkeypatch.setattr(platform_module, "system", lambda: system)
    monkeypatch.setattr(platform_module, "machine", lambda: machine)
    monkeypatch.setattr("os.name", "nt" if system == "Windows" else "posix")


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Windows", "AMD64", "ludusavi-v0.31.0-win64.zip"),
        ("Linux", "x86_64", "ludusavi-v0.31.0-linux.tar.gz"),
        ("Darwin", "arm64", "ludusavi-v0.31.0-mac.tar.gz"),
    ],
)
def test_asset_matches_the_platforms_upstream_builds(
    system: str, machine: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _platform(monkeypatch, system, machine)
    assert ludusavi._ludusavi_asset_name(_RELEASE) == expected


@pytest.mark.parametrize(
    ("system", "machine"),
    [
        ("Linux", "aarch64"),  # Raspberry Pi, ARM handhelds
        ("Linux", "armv7l"),
        ("Darwin", "x86_64"),  # Intel Mac
    ],
)
def test_refuses_to_download_a_binary_this_machine_cannot_run(
    system: str, machine: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The asset name carries no architecture, so the download would succeed
    and only fail at exec - and the unrunnable binary would then be cached and
    returned forever. Refuse before fetching, and say what to do instead."""
    _platform(monkeypatch, system, machine)
    with pytest.raises(RuntimeError, match="checks PATH"):
        ludusavi._ludusavi_asset_name(_RELEASE)


def test_the_refusal_names_the_actual_architecture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _platform(monkeypatch, "Linux", "aarch64")
    assert "aarch64" in (ludusavi.unsupported_architecture_reason() or "")


def test_supported_platforms_have_no_complaint(monkeypatch: pytest.MonkeyPatch) -> None:
    for system, machine in (("Linux", "x86_64"), ("Darwin", "arm64"), ("Windows", "AMD64")):
        _platform(monkeypatch, system, machine)
        assert ludusavi.unsupported_architecture_reason() is None


def test_a_ludusavi_on_path_wins_over_downloading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The escape hatch for every architecture upstream does not build for.
    get_rclone_path has always done this; this one did not."""
    installed = tmp_path / "usr-bin-ludusavi"
    installed.write_text("", encoding="utf-8")
    monkeypatch.setattr("shutil.which", lambda name: str(installed))

    def explode(_: Path) -> Path:
        raise AssertionError("downloaded despite a Ludusavi being on PATH")

    monkeypatch.setattr(ludusavi, "download_ludusavi", explode)
    assert ludusavi.get_ludusavi_path(tmp_path / "config.yaml") == installed


def test_an_explicit_config_path_still_beats_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Someone who pointed gsg at a specific build meant it."""
    from game_save_genie.config import load_config, save_config

    chosen = tmp_path / "chosen-ludusavi"
    chosen.write_text("", encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config = load_config(config_path)
    config.ludusavi_path = chosen
    save_config(config, config_path)

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ludusavi")
    assert ludusavi.get_ludusavi_path(config_path) == chosen


def test_falls_through_to_download_when_nothing_is_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(ludusavi, "get_default_binary_dir", lambda: tmp_path / "bin")
    sentinel = tmp_path / "downloaded"
    monkeypatch.setattr(ludusavi, "download_ludusavi", lambda target: sentinel)
    assert ludusavi.get_ludusavi_path(tmp_path / "config.yaml") == sentinel
