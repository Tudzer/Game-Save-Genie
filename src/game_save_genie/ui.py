"""The `gsg ui` dashboard.

Restoring a save is the one thing in this product that is genuinely a
browse-and-select task, and the CLI makes you do it by eye:

    gsg versions cyberpunk-2077
    gsg pull cyberpunk-2077 --version 20260725-174814-438884

Copying a timestamp between two commands at the exact moment you have just
lost progress is the worst possible time to ask someone to be careful. Here
you arrow onto the version you want and press a key.

Three rules this module holds to:

* **No duplicated safety logic.** Backup and restore call the same functions
  the CLI does (`_run_backup`, `restore_local_version`, `_apply_cloud_version`),
  so verification, the pre-restore safety backup, and the never-under-a-live-
  game rule cannot drift between the two front ends.
* **Never block the UI thread.** Every rclone or Ludusavi call happens in a
  worker thread. Those helpers print to a Rich console bound to stdout, so
  workers capture stdout and forward it to the activity pane instead of
  letting it tear through the layout.
* **Empty is never blank.** A pane with nothing in it says why, and what key
  would change that. A blank panel reads as a broken program.
"""

from __future__ import annotations

import io
import logging
import threading
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import DataTable, Footer, Header, RichLog, Static

from .config import get_data_dir, load_config, load_games
from .database import Database
from .models import Game, SaveVersion

logger = logging.getLogger(__name__)

LOCAL = "local"
CLOUD = "cloud"

# Arrowing through the games list must not fire a network call per keypress.
CLOUD_DEBOUNCE = 0.4


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
        with Horizontal(id="confirm-box"):
            yield Static(
                f"[b]{self._question}[/b]\n\n{self._detail}\n\n"
                "[b]y[/b] confirm     [b]n[/b] or Esc cancel",
                id="confirm-text",
            )

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
    #games { width: 48%; border: round $primary; padding: 0 1; }
    #versions { width: 1fr; border: round $primary; padding: 0 1; }
    #log { height: 9; border: round $panel; padding: 0 1; }
    DataTable { height: 1fr; }
    ConfirmScreen { align: center middle; }
    #confirm-box {
        width: 66; height: auto; padding: 1 2;
        background: $surface; border: thick $warning;
    }
    #confirm-text { width: 100%; }
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
        # Guards against a slow cloud listing landing after the selection has
        # already moved on — otherwise one game's versions appear under
        # another's name.
        self._cloud_token = 0
        self._debounce: Timer | None = None
        self._stdout_lock = threading.Lock()

    # ---------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="tables"):
            yield DataTable(id="games")
            yield DataTable(id="versions")
        # highlight=False: the auto-highlighter colours stray digits inside
        # game titles, which reads as meaningful and is not.
        yield RichLog(id="log", highlight=False, markup=True, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        games = self.query_one("#games", DataTable)
        games.cursor_type = "row"
        games.zebra_stripes = True
        games.add_columns("Game", "Backups", "Last backup", "Cloud target")
        games.border_title = "Games"

        versions = self.query_one("#versions", DataTable)
        versions.cursor_type = "row"
        versions.zebra_stripes = True
        versions.add_columns("Version", "When", "Size", "Files", "Cloud")
        versions.border_title = "Versions"

        self.query_one("#log", RichLog).border_title = "Activity"
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

        table = self.query_one("#games", DataTable)
        # Refresh runs after every job, so losing the cursor would silently
        # move the selection to the first game — and the next `r` would target
        # a game the user never chose.
        previous_id = None
        if self.games and 0 <= table.cursor_row < len(self.games):
            previous_id = self.games[table.cursor_row].id

        try:
            config = load_config(self.config_path)
            self.games = load_games(self.config_path)
            db = Database(get_data_dir() / "versions.db")
        except Exception as exc:
            # A hand-edited games.yaml or a locked database must be a red line
            # in the Activity pane, not the end of the application.
            logger.exception("Could not load games")
            self.log_line(f"[red]Could not load your games: {exc}[/red]")
            return

        table.clear()
        for game in self.games:
            versions = [v for v in db.get_versions(game.id) if v.origin != "safety"]
            table.add_row(
                game.title,
                str(len(versions)),
                versions[0].created_at.astimezone().strftime("%Y-%m-%d %H:%M")
                if versions
                else "[yellow]never[/yellow]",
                _cloud_target(game, config) or "[dim]off[/dim]",
            )
        table.border_title = f"Games ({len(self.games)})"

        if not self.games:
            table.add_row("[dim]No games tracked yet.[/dim]", "", "", "")
            self.rows = []
            if self._debounce is not None:
                self._debounce.stop()
                self._debounce = None
            self._set_versions_title(None, self.source)
            self._render_versions(
                placeholder="Run 'gsg scan' to find your games, then 'gsg add <title>'."
            )
            return

        if previous_id is not None:
            for index, game in enumerate(self.games):
                if game.id == previous_id:
                    table.move_cursor(row=index)
                    break
        self.load_versions()

    @on(DataTable.RowHighlighted, "#games")
    def _game_highlighted(self, _event: DataTable.RowHighlighted) -> None:
        self.load_versions()

    def action_toggle_source(self) -> None:
        self.source = CLOUD if self.source == LOCAL else LOCAL
        self.load_versions()

    def load_versions(self) -> None:
        """Refresh the versions pane for whatever game is selected.

        Local reads are a SQLite query, so they happen immediately. Cloud
        listings are debounced: holding an arrow key down would otherwise
        launch one rclone process per row.
        """
        if self._debounce is not None:
            self._debounce.stop()
            self._debounce = None

        game = self._selected_game()
        if game is None:
            return
        # Any in-flight cloud result is now stale.
        self._cloud_token += 1

        if self.source == LOCAL:
            self._show_local_versions(game)
            return

        self._set_versions_title(game, "cloud")
        # Drop the previous rows before showing the placeholder: leaving the
        # local list in `self.rows` would let `r` restore a local snapshot
        # while the pane says it is loading cloud versions.
        self.rows = []
        self._render_versions(placeholder="Loading from cloud…")
        token = self._cloud_token
        self._debounce = self.set_timer(
            CLOUD_DEBOUNCE, lambda: self._load_cloud_versions(game, token)
        )

    def _set_versions_title(self, game: Game | None, source: str) -> None:
        table = self.query_one("#versions", DataTable)
        table.border_title = (
            f"Versions — {game.title} · {source}" if game else "Versions"
        )

    def _show_local_versions(self, game: Game) -> None:
        from .cli import _human_size, _sync_display

        config = load_config(self.config_path)
        db = Database(get_data_dir() / "versions.db")
        self.rows = [
            VersionRow(
                version_id=v.id,
                when=v.created_at.astimezone().strftime("%Y-%m-%d %H:%M"),
                size=_human_size(v.size_bytes),
                files=str(v.file_count),
                state=_sync_display(v, game, config),
                local=v,
            )
            for v in db.get_versions(game.id)
        ]
        self._set_versions_title(game, "local")
        self._render_versions(
            placeholder=None
            if self.rows
            else "No backups yet — press [b]b[/b] to create one."
        )

    def _render_versions(self, placeholder: str | None = None) -> None:
        """Draw the versions table, or an explanation of why it is empty."""
        table = self.query_one("#versions", DataTable)
        table.clear()
        if placeholder is not None:
            table.add_row(f"[dim]{placeholder}[/dim]", "", "", "", "")
            return
        for row in self.rows:
            table.add_row(row.version_id, row.when, row.size, row.files, row.state)

    # --------------------------------------------------------------- workers

    def _capture(self, work_fn: Any) -> tuple[Any, str]:
        """Run a chatty helper, returning its result and whatever it printed.

        The CLI helpers write to a Rich console bound to stdout. Left alone
        that output would be painted straight over the TUI.

        Serialised because ``redirect_stdout`` swaps a process-global. Two
        workers overlapping (a cloud listing during a backup — they are in
        different worker groups, so they can) would restore each other's
        buffer and leave sys.stdout pointing at a dead StringIO for the rest
        of the session, silently swallowing everything after it.
        """
        buffer = io.StringIO()
        with self._stdout_lock, redirect_stdout(buffer):
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

    @work(thread=True, exclusive=True, group="cloud", exit_on_error=False)
    def _load_cloud_versions(self, game: Game, token: int) -> None:
        from .cli import _cloud_target, _effective_remote
        from .cloud import get_rclone_path, list_remote_version_entries

        try:
            config = load_config(self.config_path)
            remote = _effective_remote(game, config)
            if not _cloud_target(game, config) or not remote:
                self.call_from_thread(
                    self._set_cloud_rows, token, [], "No cloud configured for this game."
                )
                return
            (entries, _out) = self._capture(
                lambda: list_remote_version_entries(
                    # This can DOWNLOAD rclone on a fresh install, so it raises
                    # far more than RuntimeError: ConnectionError, OSError,
                    # PermissionError. Catching only RuntimeError tore the whole
                    # dashboard down over an offline machine.
                    get_rclone_path(self.config_path), game, remote, config.remote_root
                )
            )
        except Exception as exc:
            # Errors DO belong in the activity log — unlike routine listings,
            # which used to log a line per keypress and drowned everything.
            logger.exception("Cloud listing failed for %s", game.title)
            self.call_from_thread(self.log_line, f"[red]Cloud listing failed: {exc}[/red]")
            self.call_from_thread(
                self._set_cloud_rows, token, [], "Could not reach the cloud (see Activity)."
            )
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
            self._set_cloud_rows,
            token,
            rows,
            "No cloud versions yet — press [b]b[/b] to back up and upload.",
        )

    def _set_cloud_rows(self, token: int, rows: list[VersionRow], empty_note: str) -> None:
        if token != self._cloud_token:
            return  # the selection moved on while this listing was in flight
        self.rows = rows
        self._render_versions(placeholder=None if rows else empty_note)

    @work(thread=True, exclusive=True, group="job", exit_on_error=False)
    def _run_backup_job(self, game: Game) -> None:
        from .cli import _cloud_upload, _run_backup
        from .ludusavi import get_ludusavi_path

        def job() -> tuple[bool, str]:
            # Resolved inside the try below, not above it: get_ludusavi_path
            # downloads the binary on a fresh install and raises on an offline
            # or proxied machine. Outside the guard that killed the app with a
            # traceback and left _busy stuck True.
            config = load_config(self.config_path)
            db = Database(get_data_dir() / "versions.db")
            ludusavi = None if game.custom else get_ludusavi_path(self.config_path)
            result = _run_backup(game, config, db, ludusavi, label="Backup from gsg ui")
            if not result.success:
                return False, f"[red]{result.message}[/red]"
            if result.version is None:
                return True, f"[dim]{game.title}: {result.message}[/dim]"
            uploaded = _cloud_upload(self.config_path, game, result.version, dry_run=False)
            if not uploaded:
                return False, f"[red]{game.title}: backed up locally, but the upload failed.[/red]"
            return True, f"[green]{result.message}[/green]"

        try:
            (outcome, output) = self._capture(job)
        except Exception as exc:  # pragma: no cover - defensive
            self.call_from_thread(self._finish, f"[red]Backup crashed: {exc}[/red]", "", False)
            return
        _ok, message = outcome
        self.call_from_thread(self._finish, message, output, True)

    @work(thread=True, exclusive=True, group="job", exit_on_error=False)
    def _run_restore_job(self, game: Game, row: VersionRow) -> None:
        from .cli import _apply_cloud_version, restore_local_version
        from .cloud import get_rclone_path
        from .ludusavi import get_ludusavi_path

        def job() -> tuple[bool, str]:
            config = load_config(self.config_path)
            db = Database(get_data_dir() / "versions.db")
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

    # exclusive: holding `r` down would otherwise stack two identical confirm
    # dialogs, and answering both starts two restores sharing one staging
    # directory — one wipes the other mid-extract.
    @work(exclusive=True, group="confirm")
    async def _confirm_restore(self) -> None:
        game = self._selected_game()
        row = self._selected_row()
        if game is None or self._reject_if_busy():
            return
        if row is None:
            self.log_line(
                "[yellow]No version selected. Pick one on the right, "
                "or press b to create a backup first.[/yellow]"
            )
            return
        source = "local snapshot" if row.local is not None else "cloud version"
        confirmed = await self.push_screen_wait(
            ConfirmScreen(
                f"Restore {game.title}?",
                f"This overwrites the current save files with {source} "
                f"{row.version_id} ({row.when}).\n"
                f"A safety backup of your current saves is taken first, "
                f"so this can be undone.",
            )
        )
        if not confirmed:
            self.log_line("[dim]Restore cancelled — nothing changed.[/dim]")
            return
        # Re-check: the gate above was evaluated before the dialog awaited, so
        # a backup may have started while it was open.
        if self._reject_if_busy():
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


def run(config_path: Path | None = None) -> bool:
    """Launch the dashboard. False if it ended on an unhandled error.

    Textual catches exceptions raised once its message loop is up: it renders
    a traceback and sets return_code, but ``App.run()`` still returns
    normally. Reporting that as success would exit 0 on a screen full of
    traceback and skip the caller's fallback.
    """
    app = GameSaveGenieApp(config_path=config_path)
    app.run()
    return app.return_code in (0, None)
