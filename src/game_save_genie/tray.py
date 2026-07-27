"""System tray icon for the background watcher.

The whole point of `gsg auto` is that you forget it exists — which is also its
worst failure mode. On Windows it is launched hidden (window style 0), so every
message it prints goes to a console nobody can see. A user whose backups have
been failing for three weeks has no way to find out.

The tray icon is the fix: a persistent, always-visible statement of whether
your saves are actually safe, colour-coded, with the last event in the tooltip.

It is strictly optional. If pystray or Pillow are missing, or there is no tray
to attach to (headless Linux, no GTK/AppIndicator), `create_tray` returns a
no-op object and the watcher runs exactly as before. Backups must never depend
on the decoration.
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Health states, worst first. `escalate` relies on this order.
STATE_OK = "ok"
STATE_WARN = "warn"
STATE_ERROR = "error"
STATE_PAUSED = "paused"

_SEVERITY = {STATE_OK: 0, STATE_PAUSED: 1, STATE_WARN: 2, STATE_ERROR: 3}


def asset_path(*parts: str) -> Path:
    """Resolve a packaged asset, both from source and from a PyInstaller exe.

    The build bundles the glyphs under ``game_save_genie/assets``, which is
    where ``__file__`` already points inside a onefile bundle — the _MEIPASS
    branch is only a guard for layouts where it does not.
    """
    local = Path(__file__).resolve().parent / "assets" / Path(*parts)
    if local.exists():
        return local
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "game_save_genie" / "assets" / Path(*parts)
    return local


class NullTray:
    """Stand-in used whenever a real tray icon cannot be created."""

    available = False

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def set_state(self, state: str, detail: str = "") -> None:
        pass

    def escalate(self, state: str, detail: str = "") -> None:
        pass

    def notify(self, title: str, message: str) -> bool:
        return False


class Tray:
    """A pystray icon reflecting watcher health.

    Menu actions are invoked on pystray's own thread. Each one is handed
    straight to the caller's callback, which is responsible for taking the
    backup lock — the watcher and the tray must never back up at once.
    """

    available = True

    def __init__(
        self,
        icon_cls: Any,
        menu_cls: Any,
        item_cls: Any,
        images: dict[str, Any],
        actions: dict[str, Callable[[], None]],
    ) -> None:
        self._images = images
        self._state = STATE_OK
        self._detail = "Watching for games"
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

        def _run(action: Callable[[], None]) -> Callable[[Any, Any], None]:
            def handler(_icon: Any, _item: Any) -> None:
                try:
                    action()
                except Exception:
                    # Deliberately broad: an exception escaping here kills the
                    # tray thread, taking the icon down and leaving the user
                    # with a daemon that looks uninstalled.
                    logger.exception("Tray action failed")

            return handler

        menu_items = [
            item_cls("Back up now", _run(actions["backup_now"]), default=True),
            item_cls("Show status", _run(actions["status"])),
            item_cls("Open log folder", _run(actions["open_logs"])),
            menu_cls.SEPARATOR,
            item_cls("Quit Game Save Genie", _run(actions["quit"])),
        ]
        self._icon = icon_cls(
            "game-save-genie",
            images[STATE_OK],
            self._title(),
            menu_cls(*menu_items),
        )

    def _title(self) -> str:
        # Windows truncates tooltips around 127 chars; keep it short.
        head = "Game Save Genie"
        return f"{head} — {self._detail}"[:120] if self._detail else head

    def start(self) -> None:
        # pystray's win32 backend builds its message loop in whichever thread
        # calls run(), so a daemon thread is fine and keeps the watcher on the
        # main thread where Ctrl+C still reaches it.
        self._thread = threading.Thread(
            target=self._icon.run, name="gsg-tray", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        try:
            self._icon.stop()
        except Exception:
            logger.debug("Tray stop failed", exc_info=True)

    def set_state(self, state: str, detail: str = "") -> None:
        if state not in self._images:
            return
        with self._lock:
            self._state = state
            if detail:
                self._detail = detail
            try:
                self._icon.icon = self._images[state]
                self._icon.title = self._title()
            except Exception:
                logger.debug("Tray repaint failed", exc_info=True)

    def escalate(self, state: str, detail: str = "") -> None:
        """Raise the state only if `state` is worse than the current one.

        A single failed game should leave the icon red even if the next game
        in the same sweep succeeds — the user still needs to look.
        """
        if _SEVERITY.get(state, 0) >= _SEVERITY.get(self._state, 0):
            self.set_state(state, detail)
        elif detail:
            with self._lock:
                self._detail = detail
                try:
                    self._icon.title = self._title()
                except Exception:
                    logger.debug("Tray tooltip update failed", exc_info=True)

    def notify(self, title: str, message: str) -> bool:
        """Show a balloon from the tray icon. False if the backend refused."""
        try:
            self._icon.notify(message, title)
            return True
        except Exception:
            logger.debug("Tray notification failed", exc_info=True)
            return False


def create_tray(actions: dict[str, Callable[[], None]]) -> NullTray | Tray:
    """Build a tray icon, or a no-op stand-in when one is not possible.

    Never raises: a missing optional dependency, a headless session, or a
    desktop with no notification area must not stop a backup daemon.
    """
    try:
        import pystray
        from PIL import Image
    except ImportError as exc:
        logger.info("Tray icon unavailable (%s); running without it.", exc)
        return NullTray()

    images: dict[str, Any] = {}
    for state in (STATE_OK, STATE_WARN, STATE_ERROR, STATE_PAUSED):
        path = asset_path("tray", f"{state}.png")
        try:
            with Image.open(path) as handle:
                images[state] = handle.convert("RGBA").copy()
        except OSError as exc:
            logger.warning("Tray glyph %s missing (%s); running without a tray.", path, exc)
            return NullTray()

    try:
        return Tray(pystray.Icon, pystray.Menu, pystray.MenuItem, images, actions)
    except Exception as exc:
        # Backend selection happens here and can fail in ways specific to the
        # desktop (no AppIndicator, no notification area, no display). None of
        # them are a reason to stop backing up saves.
        logger.info("Tray icon could not be created (%s); running without it.", exc)
        return NullTray()
