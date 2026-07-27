"""Tests for the `gsg ui` dashboard.

Driven headlessly through Textual's pilot, so they run on CI with no terminal.
`asyncio.run` is used directly rather than adding pytest-asyncio as a
dependency for a handful of tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

import pytest
from textual.coordinate import Coordinate
from textual.widgets import DataTable
from typer.testing import CliRunner

from game_save_genie.cli import app as cli_app
from game_save_genie.ui import (
    CLOUD_DEBOUNCE,
    GameSaveGenieApp,
    VersionRow,
    _format_version_id,
)

runner = CliRunner()
T = TypeVar("T")


def _run(coro: Awaitable[T]) -> T:
    return asyncio.run(coro)  # type: ignore[arg-type]


@pytest.fixture
def seeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated config with one custom game and two real backups.

    The second backup holds b"level-99", the first b"level-50", so a restore
    of the older version is observable in the bytes on disk.
    """
    monkeypatch.setattr("game_save_genie.cli.get_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr("game_save_genie.ui.get_data_dir", lambda: tmp_path / "data")

    cfg = tmp_path / "c.yaml"
    saves = tmp_path / "saves"
    saves.mkdir()
    (saves / "hero.srm").write_bytes(b"level-50")

    runner.invoke(cli_app, ["--config", str(cfg), "config", "--backup-dir", str(tmp_path / "bk")])
    assert runner.invoke(
        cli_app, ["--config", str(cfg), "add", "Smoke Game", "--path", str(saves)]
    ).exit_code == 0
    assert runner.invoke(
        cli_app, ["--config", str(cfg), "backup", "smoke-game", "--no-cloud"]
    ).exit_code == 0

    (saves / "hero.srm").write_bytes(b"level-99")
    assert runner.invoke(
        cli_app, ["--config", str(cfg), "backup", "smoke-game", "--no-cloud"]
    ).exit_code == 0
    return cfg


async def _wait_idle(app: GameSaveGenieApp, pilot: Any, timeout: float = 20.0) -> None:
    """Wait for the worker to finish, so assertions see the final state."""
    waited = 0.0
    while app._busy and waited < timeout:
        await pilot.pause(0.1)
        waited += 0.1


def _drive(cfg: Path, body: Callable[[GameSaveGenieApp, Any], Awaitable[None]]) -> None:
    async def main() -> None:
        app = GameSaveGenieApp(config_path=cfg)
        async with app.run_test() as pilot:
            await pilot.pause()
            await body(app, pilot)

    _run(main())


def test_dashboard_lists_tracked_games_and_versions(seeded: Path) -> None:
    async def body(app: GameSaveGenieApp, pilot: Any) -> None:
        assert app.query_one("#games", DataTable).row_count == 1
        assert app.query_one("#versions", DataTable).row_count == 2
        game = app._selected_game()
        assert game is not None and game.title == "Smoke Game"

    _drive(seeded, body)


def test_versions_are_newest_first(seeded: Path) -> None:
    async def body(app: GameSaveGenieApp, pilot: Any) -> None:
        ids = [row.version_id for row in app.rows]
        assert ids == sorted(ids, reverse=True)

    _drive(seeded, body)


def test_cloud_toggle_is_graceful_without_a_remote(seeded: Path) -> None:
    """Switching to cloud with nothing configured must report, not explode."""

    async def body(app: GameSaveGenieApp, pilot: Any) -> None:
        await pilot.press("c")
        assert app.source == "cloud"
        # Nothing is selectable the instant we switch — the local rows must
        # not stay live behind the "loading" placeholder.
        assert app.rows == []
        assert app._selected_row() is None

        await pilot.pause(CLOUD_DEBOUNCE + 0.6)  # past the debounce and the listing
        assert app.rows == []

        # ...and switching back restores the local list immediately.
        await pilot.press("c")
        await pilot.pause(0.2)
        assert len(app.rows) == 2

    _drive(seeded, body)


def test_an_empty_versions_pane_explains_itself(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A blank panel reads as a broken program. It must say why it is empty
    and which key changes that."""
    monkeypatch.setattr("game_save_genie.cli.get_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr("game_save_genie.ui.get_data_dir", lambda: tmp_path / "data")
    cfg = tmp_path / "c.yaml"
    saves = tmp_path / "saves"
    saves.mkdir()
    (saves / "s.dat").write_bytes(b"x")
    runner.invoke(cli_app, ["--config", str(cfg), "config", "--backup-dir", str(tmp_path / "bk")])
    runner.invoke(cli_app, ["--config", str(cfg), "add", "Nothing Yet", "--path", str(saves)])

    async def body(app: GameSaveGenieApp, pilot: Any) -> None:
        table = app.query_one("#versions", DataTable)
        assert app.rows == []
        # One placeholder row, carrying guidance rather than nothing at all.
        assert table.row_count == 1
        cell = str(table.get_cell_at(Coordinate(0, 0)))
        assert "press" in cell.lower()

    _drive(cfg, body)


def test_navigation_does_not_fire_a_cloud_listing_per_keypress(seeded: Path) -> None:
    """Arrowing down the games list used to launch one rclone process per row
    and log a line for each, burying everything else."""
    calls: list[str] = []

    async def body(app: GameSaveGenieApp, pilot: Any) -> None:
        original = app._load_cloud_versions

        def spy(game: Any, token: int) -> None:
            calls.append(game.id)
            original(game, token)

        app._load_cloud_versions = spy  # type: ignore[assignment]
        await pilot.press("c")
        # Move around faster than the debounce window.
        for _ in range(4):
            await pilot.press("down")
            await pilot.pause(0.02)
        await pilot.pause(CLOUD_DEBOUNCE + 0.5)
        assert len(calls) <= 1, f"debounce failed: {len(calls)} listings fired"

    _drive(seeded, body)


def test_a_stale_cloud_listing_cannot_overwrite_the_current_selection(seeded: Path) -> None:
    """A slow listing landing late must not paint one game's versions under
    another game's name."""

    async def body(app: GameSaveGenieApp, pilot: Any) -> None:
        before = list(app.rows)
        stale = app._cloud_token
        app._cloud_token += 1  # the selection has moved on since that request
        arriving = [VersionRow("v1", "when", "1 B", "1", "cloud", None)]

        app._set_cloud_rows(stale, arriving, "empty")
        assert app.rows == before, "a stale listing overwrote the current rows"

        # The same payload with a current token is accepted.
        app._set_cloud_rows(app._cloud_token, arriving, "empty")
        assert app.rows == arriving

    _drive(seeded, body)


def test_restore_asks_before_touching_save_files(seeded: Path) -> None:
    """The whole point of the picker is one keypress from a restore, so the
    confirmation is what stands between a stray 'r' and overwritten saves."""
    save = seeded.parent / "saves" / "hero.srm"

    async def body(app: GameSaveGenieApp, pilot: Any) -> None:
        before = save.read_bytes()
        await pilot.press("r")
        await pilot.pause(0.3)
        assert type(app.screen).__name__ == "ConfirmScreen"
        await pilot.press("n")
        await pilot.pause(0.3)
        assert save.read_bytes() == before, "cancelling a restore changed the save"

    _drive(seeded, body)


def test_confirmed_restore_rolls_the_save_back(seeded: Path) -> None:
    save = seeded.parent / "saves" / "hero.srm"
    assert save.read_bytes() == b"level-99"

    async def body(app: GameSaveGenieApp, pilot: Any) -> None:
        # Select the OLDEST version (last row) and restore it.
        table = app.query_one("#versions", DataTable)
        table.move_cursor(row=len(app.rows) - 1)
        await pilot.pause(0.2)
        await pilot.press("r")
        await pilot.pause(0.3)
        await pilot.press("y")
        await _wait_idle(app, pilot)

    _drive(seeded, body)
    assert save.read_bytes() == b"level-50"


def test_unsaved_progress_is_captured_before_the_restore(seeded: Path, tmp_path: Path) -> None:
    """The state on disk at restore time must survive the restore.

    This is the property the pre-restore safety backup exists for: the save is
    put into a state that has never been backed up, so restoring an older
    version would otherwise destroy it irrecoverably.
    """
    from game_save_genie.database import Database

    save = tmp_path / "saves" / "hero.srm"
    save.write_bytes(b"level-77-never-backed-up")

    async def body(app: GameSaveGenieApp, pilot: Any) -> None:
        table = app.query_one("#versions", DataTable)
        table.move_cursor(row=len(app.rows) - 1)
        await pilot.pause(0.2)
        await pilot.press("r")
        await pilot.pause(0.3)
        await pilot.press("y")
        await _wait_idle(app, pilot)

    _drive(seeded, body)

    assert save.read_bytes() == b"level-50", "the restore did not apply"

    db = Database(tmp_path / "data" / "versions.db")
    safety = [v for v in db.get_versions("smoke-game") if v.origin == "safety"]
    assert safety, "unsaved progress was overwritten with no safety backup"

    # And that snapshot really holds the pre-restore bytes, not just a row.
    import zipfile

    with zipfile.ZipFile(safety[0].local_path) as zf:
        captured = {zf.read(n) for n in zf.namelist() if n.endswith("hero.srm")}
    assert b"level-77-never-backed-up" in captured


def test_second_action_is_refused_while_one_is_running(seeded: Path) -> None:
    async def body(app: GameSaveGenieApp, pilot: Any) -> None:
        app._busy = True
        assert app._reject_if_busy() is True
        app._busy = False
        assert app._reject_if_busy() is False

    _drive(seeded, body)


def test_ui_command_refuses_a_non_tty(tmp_path: Path) -> None:
    """CliRunner gives a non-tty stdout; the app would garble it."""
    result = runner.invoke(cli_app, ["--config", str(tmp_path / "c.yaml"), "ui"])
    assert result.exit_code == 1
    assert "interactive terminal" in result.output


@pytest.mark.parametrize(
    ("version_id", "expected"),
    [
        ("20260725-174814-438884", "2026-07-25 17:48"),
        ("20260101-000000-000000", "2026-01-01 00:00"),
        ("not-a-version", "not-a-version"),
        ("2026-17", "2026-17"),
        ("", ""),
    ],
)
def test_version_ids_render_as_readable_timestamps(version_id: str, expected: str) -> None:
    assert _format_version_id(version_id) == expected
