"""Tests for the system tray icon.

These never create a real tray: CI runs headless on Linux, where any backend
would fail. `Tray` takes its pystray classes as arguments precisely so the
behaviour can be tested against stand-ins, and the fallback path is exercised
by making the import fail.
"""

from __future__ import annotations

from typing import Any

import pytest

from game_save_genie import tray as tray_mod


class _FakeIcon:
    def __init__(self, name: str, image: Any, title: str, menu: Any) -> None:
        self.name = name
        self.icon = image
        self.title = title
        self.menu = menu
        self.notified: list[tuple[str, str]] = []
        self.stopped = False

    def notify(self, message: str, title: str) -> None:
        self.notified.append((title, message))

    def stop(self) -> None:
        self.stopped = True


class _FakeMenu:
    SEPARATOR = "---"

    def __init__(self, *items: Any) -> None:
        self.items = items


class _FakeItem:
    def __init__(self, label: str, action: Any, default: bool = False) -> None:
        self.label = label
        self.action = action
        self.default = default


def _tray(actions: dict[str, Any] | None = None) -> tray_mod.Tray:
    images = {
        state: f"<{state}>"
        for state in (
            tray_mod.STATE_OK,
            tray_mod.STATE_WARN,
            tray_mod.STATE_ERROR,
            tray_mod.STATE_PAUSED,
        )
    }
    calls: dict[str, int] = {}

    def counter(name: str) -> Any:
        def action() -> None:
            calls[name] = calls.get(name, 0) + 1

        return action

    resolved = actions or {
        key: counter(key) for key in ("backup_now", "status", "open_logs", "quit")
    }
    tray = tray_mod.Tray(_FakeIcon, _FakeMenu, _FakeItem, images, resolved)
    tray.calls = calls  # type: ignore[attr-defined]
    return tray


def test_every_state_has_a_shipped_glyph() -> None:
    """A missing PNG silently downgrades the tray to nothing at runtime."""
    for state in (
        tray_mod.STATE_OK,
        tray_mod.STATE_WARN,
        tray_mod.STATE_ERROR,
        tray_mod.STATE_PAUSED,
    ):
        path = tray_mod.asset_path("tray", f"{state}.png")
        assert path.exists(), f"missing tray glyph for '{state}': {path}"
        assert path.stat().st_size > 0


def test_null_tray_is_inert_and_reports_unavailable() -> None:
    null = tray_mod.NullTray()
    assert null.available is False
    # Every call must be safe; notify reports that nothing was shown so the
    # caller falls back to the platform notifier.
    null.start()
    null.set_state(tray_mod.STATE_ERROR, "boom")
    null.escalate(tray_mod.STATE_ERROR, "boom")
    assert null.notify("t", "m") is False
    null.stop()


def test_create_tray_falls_back_when_pystray_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing optional dependency must not stop the backup daemon."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "pystray":
            raise ImportError("No module named 'pystray'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    result = tray_mod.create_tray({k: (lambda: None) for k in
                                   ("backup_now", "status", "open_logs", "quit")})
    assert isinstance(result, tray_mod.NullTray)
    assert result.available is False


def test_menu_exposes_the_four_actions() -> None:
    tray = _tray()
    labels = [getattr(i, "label", i) for i in tray._icon.menu.items]
    assert labels == [
        "Back up now",
        "Show status",
        "Open log folder",
        _FakeMenu.SEPARATOR,
        "Quit Game Save Genie",
    ]


def test_menu_action_failure_does_not_escape() -> None:
    """An exception escaping a menu handler kills the tray thread, which looks
    to the user like the app uninstalled itself."""

    def boom() -> None:
        raise RuntimeError("action exploded")

    tray = _tray(
        {"backup_now": boom, "status": boom, "open_logs": boom, "quit": boom}
    )
    handler = tray._icon.menu.items[0].action
    handler(None, None)  # must not raise


def test_set_state_updates_icon_and_tooltip() -> None:
    tray = _tray()
    tray.set_state(tray_mod.STATE_ERROR, "Witcher 3: upload failed")
    assert tray._icon.icon == "<error>"
    assert "Witcher 3: upload failed" in tray._icon.title


def test_set_state_ignores_an_unknown_state() -> None:
    tray = _tray()
    tray.set_state("banana", "nonsense")
    assert tray._icon.icon == "<ok>"


def test_escalate_keeps_the_worst_state() -> None:
    """One game failing must leave the icon red even when the next succeeds —
    otherwise a sweep's last success hides the failure the user needs to see."""
    tray = _tray()
    tray.escalate(tray_mod.STATE_ERROR, "upload failed")
    assert tray._icon.icon == "<error>"

    tray.escalate(tray_mod.STATE_OK, "Roblox backed up")
    assert tray._icon.icon == "<error>", "a later success cleared the error state"
    # ...but the tooltip still reflects the most recent event.
    assert "Roblox backed up" in tray._icon.title


def test_escalate_raises_severity_from_ok_to_warn() -> None:
    tray = _tray()
    tray.escalate(tray_mod.STATE_WARN, "2 games never backed up")
    assert tray._icon.icon == "<warn>"


def test_notify_reports_success_and_passes_title_first() -> None:
    tray = _tray()
    assert tray.notify("Save backed up", "Cyberpunk 2077") is True
    assert tray._icon.notified == [("Save backed up", "Cyberpunk 2077")]


def test_tooltip_stays_within_the_windows_limit() -> None:
    tray = _tray()
    tray.set_state(tray_mod.STATE_OK, "x" * 400)
    assert len(tray._icon.title) <= 120


def test_stop_is_safe_when_the_backend_raises() -> None:
    tray = _tray()

    def boom() -> None:
        raise RuntimeError("no message loop")

    tray._icon.stop = boom
    tray.stop()  # must not raise
