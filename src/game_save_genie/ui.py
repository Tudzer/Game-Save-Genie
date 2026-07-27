"""The `gsg ui` dashboard.

Restoring a save is the one thing in this product that is genuinely a
browse-and-select task, and the CLI makes you do it by eye:

    gsg versions cyberpunk-2077
    gsg pull cyberpunk-2077 --version 20260725-174814-438884

Copying a timestamp between two commands at the exact moment you have just
lost progress is the worst possible time to ask someone to be careful. Here
you arrow onto the version you want and press a key.

Two rules this module holds to:

* **No duplicated safety logic.** Backup and restore call the same functions
  the CLI does (`_run_backup`, `restore_local_version`, `_apply_cloud_version`),
  so verification, the pre-restore safety backup, and the never-under-a-live-
  game rule cannot drift between the two front ends.
* **Never block the UI thread.** Every rclone or Ludusavi call happens in a
  worker thread. Those helpers print to a Rich console bound to stdout, so
  workers capture stdout and forward it to the log pane instead of letting it
  tear through the layout.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Label, RichLog, Static

from .config import get_data_dir, load_config, load_games
from .database import Database
from .models import Game, SaveVersion

LOCAL = "local"
CLOUD = "cloud"


@dataclass
class VersionRow:
    """One row in the versions table, from either source."""

    version_id: str
    when: str
    size: str
    files: str
    state: str
    local: SaveVersion | None


class ConfirmScreen(ModalScreen[bool]):
    """Yes/no gate for anything that overwrites live save files."""

    BINDINGS: ClassVar[list[Any]] = [("escape", "dismiss_false", "Cancel")]

    def __init__(self, question: str, detail: str) -> None:
        super().__init__()
        self._question = question
        self._detail = detail

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Label(self._question, id="confirm-question")
            yield Static(self._detail, id="confirm-detail")
            yield Label("[b]y[/b] confirm     [b]n[/b] / Esc cancel", id="confirm-keys")

    def on_key(self, event: Any) -> None:
        if event.key == "y":
            self.dismiss(True)
        elif event.key in ("n", "escape"):
            self.dismiss(False)

    def action_dismiss_false(self) -> None:
        self.dismiss(False)


class GameSaveGenieApp(App[None]):
    """Browse tracked games, their versions, and restore without typing an id."""

    TITLE = "Game Save Genie"

    CSS = """
    Screen { layout: vertical; }
    #tables { height: 1fr; }
    #games { width: 45%; border: solid $panel; }
    #versions { width: 1fr; border: solid $panel; }
    #log { height: 30%; border: solid $panel; }
    DataTable { height: 1fr; }
    #confirm-box {
        width: 70; height: auto; padding: 1 2;
        background: $surface; border: thick $warning;
    }
    #confirm-question { text-style: bold; width: 100%; }
    #confirm-detail { margin: 1 0; width: 100%; }
    #confirm-keys { width: 100%; }
    ConfirmScreen { align: center middle; }
    """

    BINDINGS: ClassVar[list[Any]] = [
        ("b", "backup", "Back up"),
        ("r", "restore", "Restore"),
        ("c", "toggle_source", "Local/Cloud"),
        ("f5", "refresh", "Refresh"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, config_path: Path | None = None) -> None:
        super().__init__()
        self.config_path = config_path
        self.games: list[Game] = []
        self.source = LOCAL
        self.rows: list[VersionRow] = []
        self._busy = False

    # ---------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="tables"):
            yield DataTable(id="games")
            yield DataTable(id="versions")
        yield RichLog(id="log", highlight=True, markup=True, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        games = self.query_one("#games", DataTable)
        games.cursor_type = "row"
        games.add_columns("Game", "Versions", "Last backup", "Cloud target")
        versions = self.query_one("#versions", DataTable)
        versions.cursor_type = "row"
        versions.add_columns("Version", "When", "Size", "Files", "Cloud")
        self.log_line("[dim]Loading tracked games…[/dim]")
        self.action_refresh()

    # ------------------------------------------------------------------ data

    def log_line(self, text: str) -> None:
        self.query_one("#log", RichLog).write(text)

    def _selected_game(self) -> Game | None:
        table = self.query_one("#games", DataTable)
        if table.cursor_row < 0 or table.cursor_row >= len(self.games):
            return None
        return self.games[table.cursor_row]

    def _selected_row(self) -> VersionRow | None:
        table = self.query_one("#versions", DataTable)
        if table.cursor_row < 0 or table.cursor_row >= len(self.rows):
            return None
        return self.rows[table.cursor_row]

    def action_refresh(self) -> None:
        # Imported inside the call: cli imports this module lazily for the
        # `gsg ui` command, so a module-level import here would be a cycle.
        from .cli import _cloud_target

        config = load_config(self.config_path)
        self.games = load_games(self.config_path)
        db = Database(get_data_dir() / "versions.db")

        table = self.query_one("#games", DataTable)
        table.clear()
        for game in self.games:
            versions = [v for v in db.get_versions(game.id) if v.origin != "safety"]
            last = versions[0].created_at.strftime("%Y-%m-%d %H:%M") if versions else "never"
            table.add_row(
                game.title,
                str(len(versions)),
                last if versions else "[yellow]never[/yellow]",
                _cloud_target(game, config) or "off",
            )
        if not self.games:
            self.log_line("[yellow]No games tracked yet. Run 'gsg scan' or 'gsg add'.[/yellow]")
            return
        self.sub_title = f"{len(self.games)} game(s) — {self.source} versions"
        self.load_versions()

    @on(DataTable.RowHighlighted, "#games")
    def _game_highlighted(self, _event: DataTable.RowHighlighted) -> None:
        self.load_versions()

    def action_toggle_source(self) -> None:
        self.source = CLOUD if self.source == LOCAL else LOCAL
        self.sub_title = f"{len(self.games)} game(s) — {self.source} versions"
        self.load_versions()

    def load_versions(self) -> None:
        game = self._selected_game()
        if game is None:
            return
        if self.source == LOCAL:
            self._show_local_versions(game)
        else:
            # Cloud listing is a network round-trip; never on the UI thread.
            self._load_cloud_versions(game)

    def _show_local_versions(self, game: Game) -> None:
        from .cli import _human_size, _sync_display

        config = load_config(self.config_path)
        db = Database(get_data_dir() / "versions.db")
        self.rows = [
            VersionRow(
                version_id=v.id,
                when=v.created_at.strftime("%Y-%m-%d %H:%M"),
                size=_human_size(v.size_bytes),
                files=str(v.file_count),
                state=_sync_display(v, game, config),
                local=v,
            )
            for v in db.get_versions(game.id)
        ]
        self._render_versions()

    def _render_versions(self) -> None:
        table = self.query_one("#versions", DataTable)
        table.clear()
        for row in self.rows:
            table.add_row(row.version_id, row.when, row.size, row.files, row.state)

    # --------------------------------------------------------------- workers

    def _capture(self, work_fn: Any) -> tuple[Any, str]:
        """Run a chatty helper, returning its result and whatever it printed.

        The CLI helpers write to a Rich console bound to stdout. Left alone
        that output would be painted straight over the TUI.
        """
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            result = work_fn()
        return result, buffer.getvalue().strip()

    def _finish(self, message: str, output: str, refresh: bool) -> None:
        self._busy = False
        if output:
            for line in output.splitlines():
                self.log_line(f"[dim]{line}[/dim]")
        self.log_line(message)
        if refresh:
            self.action_refresh()

    @work(thread=True, exclusive=True, group="cloud")
    def _load_cloud_versions(self, game: Game) -> None:
        from .cli import _cloud_target, _effective_remote
        from .cloud import get_rclone_path, list_remote_version_entries

        config = load_config(self.config_path)
        remote = _effective_remote(game, config)
        if not _cloud_target(game, config) or not remote:
            self.call_from_thread(self._set_cloud_rows, [], "No cloud configured for this game.")
            return
        try:
            (entries, _out) = self._capture(
                lambda: list_remote_version_entries(
                    get_rclone_path(self.config_path), game, remote, config.remote_root
                )
            )
        except RuntimeError as exc:
            self.call_from_thread(self._set_cloud_rows, [], f"[red]Cloud listing failed: {exc}[/red]")
            return
        rows = [
            VersionRow(
                version_id=vid,
                when=_format_version_id(vid),
                size="—",
                files="—",
                state="cloud",
                local=None,
            )
            for vid, _raw in entries
        ]
        rows.reverse()
        self.call_from_thread(
            self._set_cloud_rows, rows, f"[dim]{len(rows)} cloud version(s) for {game.title}[/dim]"
        )

    def _set_cloud_rows(self, rows: list[VersionRow], message: str) -> None:
        self.rows = rows
        self._render_versions()
        if message:
            self.log_line(message)

    @work(thread=True, exclusive=True, group="job")
    def _run_backup_job(self, game: Game) -> None:
        from .cli import _cloud_upload, _run_backup
        from .ludusavi import get_ludusavi_path

        config = load_config(self.config_path)
        db = Database(get_data_dir() / "versions.db")
        ludusavi = None if game.custom else get_ludusavi_path(self.config_path)

        def job() -> tuple[bool, str]:
            result = _run_backup(game, config, db, ludusavi, label="Backup from gsg ui")
            if not result.success:
                return False, f"[red]{result.message}[/red]"
            if result.version is None:
                return True, f"[dim]{result.message}[/dim]"
            uploaded = _cloud_upload(self.config_path, game, result.version, dry_run=False)
            if not uploaded:
                return False, f"[red]{game.title}: backed up locally, but the upload failed.[/red]"
            return True, f"[green]{result.message}[/green]"

        try:
            (ok_message, output) = self._capture(job)
        except Exception as exc:  # pragma: no cover - defensive
            self.call_from_thread(self._finish, f"[red]Backup crashed: {exc}[/red]", "", False)
            return
        _ok, message = ok_message
        self.call_from_thread(self._finish, message, output, True)

    @work(thread=True, exclusive=True, group="job")
    def _run_restore_job(self, game: Game, row: VersionRow) -> None:
        from .cli import _apply_cloud_version, restore_local_version
        from .cloud import get_rclone_path
        from .ludusavi import get_ludusavi_path

        config = load_config(self.config_path)
        db = Database(get_data_dir() / "versions.db")

        def job() -> tuple[bool, str]:
            if row.local is not None:
                return restore_local_version(
                    game, row.local, config, db, self.config_path, no_safety=False
                )
            ludusavi = None if game.custom else get_ludusavi_path(self.config_path)
            applied = _apply_cloud_version(
                game=game,
                config=config,
                db=db,
                rclone_path=get_rclone_path(self.config_path),
                ludusavi_path=ludusavi,
                version_id=row.version_id,
            )
            if applied:
                return True, f"Restored {game.title} from cloud version {row.version_id}"
            return False, f"Could not restore {game.title} from {row.version_id}"

        try:
            (outcome, output) = self._capture(job)
        except Exception as exc:  # pragma: no cover - defensive
            self.call_from_thread(self._finish, f"[red]Restore crashed: {exc}[/red]", "", False)
            return
        ok, message = outcome
        self.call_from_thread(
            self._finish, f"[{'green' if ok else 'red'}]{message}[/]", output, True
        )

    # --------------------------------------------------------------- actions

    def action_backup(self) -> None:
        game = self._selected_game()
        if game is None or self._reject_if_busy():
            return
        self._busy = True
        self.log_line(f"[cyan]Backing up {game.title}…[/cyan]")
        self._run_backup_job(game)

    def action_restore(self) -> None:
        self._confirm_restore()

    @work
    async def _confirm_restore(self) -> None:
        game = self._selected_game()
        row = self._selected_row()
        if game is None or row is None or self._reject_if_busy():
            return
        source = "local snapshot" if row.local is not None else "cloud version"
        confirmed = await self.push_screen_wait(
            ConfirmScreen(
                f"Restore {game.title}?",
                f"This overwrites the current save files with {source} "
                f"{row.version_id} ({row.when}).\n\n"
                f"A safety backup of your current saves is taken first, so this "
                f"can be undone.",
            )
        )
        if not confirmed:
            self.log_line("[dim]Restore cancelled.[/dim]")
            return
        self._busy = True
        self.log_line(f"[cyan]Restoring {game.title} from {row.version_id}…[/cyan]")
        self._run_restore_job(game, row)

    def _reject_if_busy(self) -> bool:
        if self._busy:
            self.log_line("[yellow]Another operation is still running.[/yellow]")
            return True
        return False


def _format_version_id(version_id: str) -> str:
    """Render a `20260725-174814-438884` id as a readable timestamp."""
    parts = version_id.split("-")
    if len(parts) < 2 or len(parts[0]) != 8 or len(parts[1]) != 6:
        return version_id
    date, clock = parts[0], parts[1]
    return f"{date[:4]}-{date[4:6]}-{date[6:]} {clock[:2]}:{clock[2:4]}"


def run(config_path: Path | None = None) -> None:
    """Launch the dashboard."""
    GameSaveGenieApp(config_path=config_path).run()
