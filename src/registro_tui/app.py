"""RegistroApp — the top-level Textual application.

Responsibilities:
* Build the dependency graph (event bus, plugin registry, adapter, executor).
* Wire keyboard actions to engine operations.
* React to engine events (status bar updates, results streaming).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from pydantic import ValidationError
from textual.app import App, ComposeResult
from textual.binding import Binding

from registro_core.config import RegistroConfig, load_config
from registro_core.errors import QueryCancelledError, QueryError, RegistroError
from registro_core.events import (
    EventBus,
    QueryCompleted,
    QueryStarted,
)
from registro_core.logging import clear_context, configure_logging, get_logger
from registro_core.models.connection import Dsn
from registro_engine.executor import QueryExecutor, QueryRun
from registro_engine.history import HistoryEntry
from registro_plugins.context import PluginContext
from registro_plugins.loader import PluginLoader
from registro_plugins.registry import PluginRegistry
from registro_tui.builtin_commands import BUILTIN_COMMANDS
from registro_tui.events import RunActionRequested
from registro_tui.keymaps import DEFAULT_BINDINGS, VIM_BINDINGS
from registro_tui.screens.command_palette import CommandPaletteScreen
from registro_tui.screens.connection import ConnectionScreen
from registro_tui.screens.help import HelpModal
from registro_tui.screens.history import HistoryModal
from registro_tui.screens.main import MainScreen
from registro_tui.screens.row_limit import RowLimitModal
from registro_tui.session import Session, open_session
from registro_tui.state import AppState
from registro_tui.themes import THEMES
from registro_tui.widgets import ActionBar, QueryTabs, ResultsTable, SchemaTree, StatusBar

if TYPE_CHECKING:
    from textual.widget import Widget

    from registro_engine.pool import ConnectionPool
    from registro_plugins.api import Command

logger = get_logger(__name__)

# Worker name/group for the query stream. Named so ``on_unmount`` and the
# tests can find it, and so a stray second run is visible in the worker list.
_QUERY_WORKER = "registro-query"

# How long the second key of a sequence such as vim's ``gg`` may lag the first.
_SEQUENCE_TIMEOUT = 0.5

# Match only the statement prefix; leading comments are scanned separately.
_OPENS_TRANSACTION = re.compile(
    r"(?:BEGIN|START\s+TRANSACTION)\b",
    re.IGNORECASE,
)

# Driver messages meaning "one statement per execute". SQLite and Postgres
# (whose prepared statements hold exactly one command) refuse a script outright.
_MULTI_STATEMENT_ERRORS = (
    "only execute one statement at a time",
    "cannot insert multiple commands into a prepared statement",
)


def _opens_transaction(sql: str) -> bool:
    """Skip leading whitespace/comments in linear time, then check the command."""
    pos = 0
    while pos < len(sql):
        if sql[pos].isspace():
            pos += 1
        elif sql.startswith("--", pos):
            end = sql.find("\n", pos + 2)
            if end == -1:
                return False
            pos = end + 1
        elif sql.startswith("/*", pos):
            end = sql.find("*/", pos + 2)
            if end == -1:
                return False
            pos = end + 2
        else:
            break
    return _OPENS_TRANSACTION.match(sql, pos) is not None


def _hint_for(detail: str) -> str | None:
    """A next step for an error the user can fix from the editor, if any."""
    lowered = detail.lower()
    if any(marker in lowered for marker in _MULTI_STATEMENT_ERRORS):
        return (
            "This database runs one statement at a time. Select a single "
            "statement and run it — only the selection is executed."
        )
    return None


def _export_path() -> Path:
    """A fresh, never-reused path under ``~/.registro/exports``.

    The name used to be ``export-<unix seconds>.csv``, so a second export within
    the same second silently overwrote the first. The timestamp keeps names
    sortable and human-readable; the random suffix makes them unique.
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path.home() / ".registro" / "exports" / f"export-{stamp}-{uuid.uuid4().hex[:6]}.csv"


class RegistroApp(App[None]):
    """The Registro Textual application."""

    CSS_PATH = "registro.tcss"
    TITLE = "Registro"

    # Class-level bindings — Textual reads these at class-creation time.
    # Vim-mode adds extra bindings dynamically in __init__ via bind().
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding(k, a, d) for (k, a, d) in DEFAULT_BINDINGS
    ]

    def __init__(
        self,
        config: RegistroConfig | None = None,
        initial_dsn: Dsn | None = None,
    ) -> None:
        super().__init__()
        self.cfg = config or load_config()
        self.state = AppState(bus=EventBus(), plugins=PluginRegistry())
        self._loader: PluginLoader | None = None
        self._initial_dsn = initial_dsn
        self._current_run: QueryRun | None = None
        self._run_executor: QueryExecutor | None = None
        self._query_in_flight = False
        # Every in-flight command-palette handler. A set, not the last one only:
        # the event loop keeps just a weak reference to a task, so a handler
        # nobody holds can be garbage-collected mid-run, and unmount has to be
        # able to cancel all of them.
        self._palette_tasks: set[asyncio.Task[None]] = set()
        self._session: Session | None = None
        self._connect_lock = asyncio.Lock()
        # Two-key sequences (vim's ``gg``) and the first half of one, if typed.
        self._sequences: dict[tuple[str, str], str] = {}
        self._pending_key: tuple[str, float] | None = None
        if self.cfg.ui.keymap == "vim":
            for key, action, desc in VIM_BINDINGS:
                if (key, action, desc) in DEFAULT_BINDINGS:
                    continue
                if "," in key:
                    self._bind_sequence(key, action, desc)
                else:
                    self.bind(key, action, description=desc)

    def _bind_sequence(self, key: str, action: str, desc: str) -> None:
        """Bind a keymap entry written ``first,second`` as a key *sequence*.

        Textual has no sequences: in a binding, a comma means "any of these
        keys", so ``g,g`` handed straight to ``bind`` fired on a single ``g``.
        Each key of the sequence is bound to :meth:`action_sequence_key`, which
        runs *action* only when both arrive within :data:`_SEQUENCE_TIMEOUT`.
        """
        first, second = key.split(",", 1)
        self._sequences[(first, second)] = action
        for part in dict.fromkeys((first, second)):
            self.bind(part, f"sequence_key('{part}')", description=desc, show=False)

    async def action_sequence_key(self, key: str) -> None:
        now = time.monotonic()
        pending, self._pending_key = self._pending_key, None
        if pending is not None and now - pending[1] <= _SEQUENCE_TIMEOUT:
            action = self._sequences.get((pending[0], key))
            if action is not None:
                await self.run_action(action)
                return
        if any(first == key for first, _ in self._sequences):
            self._pending_key = (key, now)

    # ---- lifecycle -----------------------------------------------------

    def _setup_logging(self) -> None:
        """Point structured logging at ``~/.registro/registro.log``.

        Done on mount rather than in ``__init__``: a constructor that touches
        the filesystem cannot be built where ``$HOME`` is read-only, and it made
        the app awkward to instantiate in a test. A home we cannot write to
        falls back to stderr instead of refusing to start.
        """
        log_file: Path | None = None
        try:
            log_dir = Path.home() / ".registro"
            log_dir.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                log_dir.chmod(0o700)
            candidate = log_dir / "registro.log"
            if not candidate.exists():
                candidate.touch()
            with contextlib.suppress(OSError):
                candidate.chmod(0o600)
            log_file = candidate
        except OSError:
            logger.warning("log_file_unavailable", fallback="stderr")
        configure_logging(
            level=self.cfg.logging.level,
            json=self.cfg.logging.json_lines,
            log_file=log_file,
            force=True,
        )

    def compose(self) -> ComposeResult:
        yield MainScreen()

    async def on_mount(self) -> None:
        self._setup_logging()
        if not self.is_headless and os.environ.get("REGISTRO_DISABLE_UPDATE_CHECK") != "1":
            self.run_worker(self._check_for_updates(), group="updates")
        self._apply_theme()
        self._apply_border_titles()
        await self._load_plugins()
        await self._mount_plugin_panels()
        self._wire_event_listeners()
        self._results().max_rows = self.cfg.ui.max_display_rows
        self._status().mode = "VIM" if self.cfg.ui.keymap == "vim" else "NORMAL"
        # Wire ActionBar limit from config
        with contextlib.suppress(Exception):
            self._action_bar().limit = self.cfg.ui.max_display_rows
        if self._initial_dsn is not None:
            self.run_worker(self._connect(self._initial_dsn))
        else:
            self._show_connection_dialog()

    async def _check_for_updates(self) -> None:
        from registro_core.updates import check_update  # noqa: PLC0415

        try:
            latest = await asyncio.to_thread(check_update, cached=True)
            if latest:
                self.notify(
                    f"Registro {latest} is available. Run registro upgrade after quitting.",
                    timeout=12,
                )
        except Exception:
            logger.debug("update_check_unavailable")

    async def on_unmount(self) -> None:
        if self._current_run is not None:
            with contextlib.suppress(Exception):
                await self.action_cancel_query()
        pending = [t for t in self._palette_tasks if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        loader = self._loader
        if loader is not None:
            with contextlib.suppress(Exception):
                loader.unload_all()
        if self._session is not None:
            await self._session.close()
            self._session = None
        await self.state.bus.close()

    # ---- setup helpers ------------------------------------------------

    def _apply_theme(self) -> None:
        from textual.theme import Theme as TxTheme  # noqa: PLC0415

        theme = THEMES.get(self.cfg.ui.theme, THEMES["monokai"])
        tx = TxTheme(
            name=f"registro-{theme.name}",
            primary=theme.primary,
            accent=theme.accent,
            success=theme.success,
            warning=theme.warning,
            error=theme.error,
            background=theme.background,
            foreground=theme.foreground,
            surface=theme.surface,
            dark=True,
        )
        self.register_theme(tx)
        self.theme = tx.name
        logger.debug("theme_applied", theme=theme.name)

    def _apply_border_titles(self) -> None:
        """Set border titles on panels."""
        with contextlib.suppress(Exception):
            self.query_one("#sidebar").border_title = "Data Catalog"
        with contextlib.suppress(Exception):
            self.query_one("#editor-panel").border_title = "Query Editor"
        with contextlib.suppress(Exception):
            self.query_one("#results-panel").border_title = "Query Results"

    async def _load_plugins(self) -> None:
        # Pass eagerly-available services. Late-bound ones (executor,
        # introspector, history) are added via ``update_services`` once
        # ``_connect`` runs — the cached PluginContext is the same object.
        loader = PluginLoader(
            self.state.plugins,
            self.state.bus,
            enabled=self.cfg.plugins.enabled or None,
            disabled=self.cfg.plugins.disabled or None,
            services={
                "event_bus": self.state.bus,
                "plugins": self.state.plugins,
                "config": self.cfg,
            },
        )
        loader.load_all()
        self._loader = loader

    def _publish_connection_services(self) -> None:
        """Push connect-time services into every loaded plugin's context.

        Deliberately excludes the raw ``adapter``/``pool``: those hold the
        live DB credentials and let a plugin run arbitrary SQL or exhaust the
        pool outside the executor's event bus/history/cancellation machinery,
        which would defeat ``PluginContext``'s "intentionally narrow" surface
        (see ``registro_plugins.context``). ``introspector`` and ``history``
        are safe to share: they only expose schema metadata and past query
        records, not live connections.
        """
        loader = self._loader
        if loader is None:
            return
        late = {
            "introspector": self.state.introspector,
            "history": self.state.history,
        }
        for manifest in self.state.plugins.manifests():
            loader.context_for(manifest.id).update_services(**late)

    async def _mount_plugin_panels(self) -> None:
        from textual.containers import Vertical  # noqa: PLC0415

        loader = self._loader
        if loader is None:
            return
        rails: dict[str, list[Widget]] = {
            "left": [],
            "right": [],
            "bottom": [],
        }
        for plugin_id, panel in self.state.plugins.panels_by_plugin():
            ctx = loader.context_for(plugin_id)
            try:
                widget = panel.factory(ctx)
            except Exception:
                logger.exception("plugin_panel_factory_failed", plugin_id=plugin_id)
                continue
            placement = panel.placement if panel.placement in rails else "bottom"
            rails[placement].append(widget)

        if rails["left"]:
            sidebar = self.query_one("#sidebar")
            for w in rails["left"]:
                await sidebar.mount(w)
        # Append into the grid, rather than ``before=self._status()``.
        # ``Widget._find_mount_point`` resolves a *widget* spot to
        # ``spot.parent`` and ignores the receiver — and StatusBar is a sibling
        # of ``#main-grid`` under MainScreen, not a child of it. So mounting
        # "before the status bar" put both rails under MainScreen: the
        # ``.with-right``/``.with-bottom`` rules added grid tracks nothing
        # occupied, ``#main-grid.with-right #bottom-rail`` could never match
        # (the rail was not a descendant), and the rails rendered as stacked
        # blocks instead of side/bottom rails.
        #
        # Order is load-bearing: the grid places children in child order, so
        # the right rail must be appended before the bottom rail.
        if rails["right"]:
            grid = self.query_one("#main-grid")
            grid.add_class("with-right")
            rail = Vertical(id="right-rail")
            await grid.mount(rail)
            for w in rails["right"]:
                await rail.mount(w)
        if rails["bottom"]:
            grid = self.query_one("#main-grid")
            grid.add_class("with-bottom")
            rail = Vertical(id="bottom-rail")
            await grid.mount(rail)
            for w in rails["bottom"]:
                await rail.mount(w)

    def _wire_event_listeners(self) -> None:
        async def on_started(ev: QueryStarted) -> None:
            self._status().notify_status(f"running… ({ev.sql_preview})")

        async def on_completed(ev: QueryCompleted) -> None:
            bar = self._status()
            bar.rows = ev.rows
            bar.duration_ms = ev.duration_ms
            bar.has_run = True
            bar.notify_status("ok", "success")

        async def on_action(ev: RunActionRequested) -> None:
            await self.run_action(ev.action)

        self.state.bus.subscribe(QueryStarted, on_started)
        self.state.bus.subscribe(QueryCompleted, on_completed)
        self.state.bus.subscribe(RunActionRequested, on_action)

    async def _connect(self, dsn: Dsn) -> None:
        """Open a session for *dsn* and adopt it, replacing any current one.

        Guarded by a lock: submitting the connection dialog twice in quick
        succession used to race two connects into ``self.state``, and whichever
        lost silently leaked its connections.
        """
        async with self._connect_lock:
            bar = self._status()
            bar.notify_status("connecting…")
            try:
                session = await open_session(dsn, self.cfg, self.state.bus)
            except Exception as exc:
                logger.exception("connection_failed", dsn=dsn.render(mask_password=True))
                self._report_error("Connection failed", exc)
                return

            await self._adopt(session)
            bar.connection = session.dsn.render(mask_password=True)
            if await self._refresh_schema():
                # Clear the transient message so the footer shows "Database
                # Connected." — but only on success: clearing unconditionally
                # wiped a refresh error off the status line the moment it landed.
                bar.notify_status("")

    async def _adopt(self, session: Session | None) -> None:
        """Install *session* as the current one, closing whatever it replaces."""
        previous = self._session
        self._session = session
        self.state.dsn = session.dsn if session else None
        self.state.introspection_adapter = session.adapter if session else None
        self.state.pool = session.pool if session else None
        self.state.introspector = session.introspector if session else None
        with contextlib.suppress(Exception):
            self._tree().introspector = self.state.introspector
        self.state.history = session.history if session else None
        if previous is not None:
            await previous.close()
        if session is not None:
            self._publish_connection_services()

    def _show_connection_dialog(self) -> None:
        """Push the connection dialog and connect on result."""

        def _on_dsn(raw: str | None) -> None:
            if raw is None:
                return
            try:
                dsn = Dsn.parse(raw)
            except Exception as exc:
                self._report_error("Invalid DSN", exc)
                return
            self.run_worker(self._connect(dsn))

        self.push_screen(ConnectionScreen(), _on_dsn)

    async def action_connect(self) -> None:
        """Show the connection dialog (callable from the command palette)."""
        self._show_connection_dialog()

    # ---- widget shortcuts ---------------------------------------------

    def _status(self) -> StatusBar:
        return self.query_one(StatusBar)

    def _tabs(self) -> QueryTabs:
        return self.query_one(QueryTabs)

    def _results(self) -> ResultsTable:
        return self.query_one(ResultsTable)

    def _tree(self) -> SchemaTree:
        return self.query_one(SchemaTree)

    def _action_bar(self) -> ActionBar:
        return self.query_one(ActionBar)

    # ---- user feedback -------------------------------------------------

    def _set_results_summary(self, summary: str) -> None:
        """Show the run summary on the results panel's border.

        The grid itself has no border unless it happens to be focused, so its
        own ``border_subtitle`` was invisible most of the time. ``#results-panel``
        is the bordered, titled container the user actually looks at.
        """
        with contextlib.suppress(Exception):
            self.query_one("#results-panel").border_subtitle = summary

    def _report_error(self, headline: str, exc: BaseException | str) -> None:
        """Surface an error in the status bar *and* as a notification.

        The status bar is a single line shared with the keybinding hints, so
        it truncates exactly the long messages that matter most — a SQL error
        from the server. The toast carries the full text; the status line keeps
        the short form so the last error stays visible after the toast fades.
        """
        # ``message``, not ``str()``: a RegistroError's ``str`` carries its code
        # and context — for a query error, the entire SQL as a dict repr —
        # which is for the log, not for a one-line status or a toast.
        detail = exc.message if isinstance(exc, RegistroError) else str(exc)
        if isinstance(exc, RegistroError):
            logger.info("error_reported", headline=headline, error=str(exc))
        # Plain text: StatusBar builds its own Content and never parses this,
        # so a driver message full of brackets needs no escaping here.
        self._status().notify_status(f"{headline}: {detail}", "error")
        # ``markup=False`` rather than ``escape(detail)``: a toast is plain error
        # text, and a driver message routinely contains brackets — ``TEXT[]``, a
        # bracket-quoted identifier, a Python repr. Parsed as markup, ``[/]``
        # raised MarkupError mid-render and ``near [col]`` silently deleted the
        # one identifier the user needed to see.
        hint = _hint_for(detail)
        toast = f"{detail}\n\n{hint}" if hint else detail
        self.notify(toast, title=headline, severity="error", timeout=10, markup=False)

    def on_schema_tree_introspection_failed(self, event: SchemaTree.IntrospectionFailed) -> None:
        """Surface a failed lazy schema load the same way every other error is."""
        self._report_error(f"Failed to introspect {event.object_name}", event.error)

    # ---- actions ------------------------------------------------------

    async def action_run_query(self) -> None:
        """Validate the request, then stream it on a worker.

        The stream used to be drained inline, in the action itself. A binding's
        action is awaited *by the App's own message pump*, so that occupied the
        pump for the entire query and no further key could be dispatched until
        it finished — leaving the cancel key inert during exactly the long query
        it exists to abort. (Queries started from the Run button did not have
        this problem: a Button.Pressed handler runs on the button's pump, not
        the app's, so the two entry points behaved differently.)

        Validation stays here so the feedback is immediate and so
        ``_query_in_flight`` is set before any ``await`` — the re-entrancy guard
        has to be synchronous to be a guard at all.
        """
        if self._query_in_flight:
            self._status().notify_status("a query is already running", "warning")
            return
        pool = self.state.pool
        if pool is None:
            self._status().notify_status("not connected", "warning")
            return
        editor = self._tabs().current_editor
        # A selection runs on its own — the way to execute one statement out of
        # a script, on the databases that accept only one per call.
        sql = (editor.selected_text or editor.sql) if editor is not None else ""
        if not sql.strip():
            self._status().notify_status("nothing to run", "warning")
            return
        if _opens_transaction(sql):
            # Each run gets a pooled connection that is reset — rolled back —
            # when the run ends, so a BEGIN never survives to the next run and
            # the statements after it autocommit. Say so rather than let the
            # user believe their COMMIT will cover them.
            self.notify(
                "Each run is committed on its own: a transaction opened here is "
                "rolled back when this run finishes, and later runs autocommit.",
                title="Explicit transactions are not kept",
                severity="warning",
                timeout=10,
            )
        self._query_in_flight = True
        self.run_worker(
            self._stream_query(pool, sql),
            name=_QUERY_WORKER,
            group=_QUERY_WORKER,
        )

    async def _stream_query(self, pool: ConnectionPool, sql: str) -> None:
        """Run *sql* and feed its batches into the results grid.

        Always reached via :meth:`action_run_query`, which owns the validation
        and the ``_query_in_flight`` handshake.
        """
        results = self._results()
        results.reset()
        self._set_results_summary("")
        results.loading = True
        self._status().truncated = False

        # Acquire a dedicated adapter from the pool for this run; release
        # it in ``finally`` so a cancellation never leaks the slot.
        try:
            async with pool.acquire() as adapter:
                executor = QueryExecutor(adapter, self.state.bus)
                self._run_executor = executor
                try:
                    self._current_run = await executor.run(
                        sql,
                        batch_size=self.cfg.ui.row_batch_size,
                        timeout=self.cfg.engine.default_timeout_seconds,
                    )
                except QueryError as exc:
                    results.loading = False
                    results.mark_done(failed=True)
                    self._set_results_summary(results.summary)
                    self._report_error("Query failed", exc)
                    await self._record_history(sql, error=exc)
                    return
                cancelled = False
                failed = False
                stream_error: RegistroError | None = None
                run = self._current_run
                try:
                    async for batch in run.rows:
                        results.loading = False
                        await results.feed(batch)
                        if results.truncated:
                            break
                except QueryCancelledError:
                    cancelled = True
                    self._status().notify_status("cancelled", "warning")
                except RegistroError as exc:
                    failed = True
                    stream_error = exc
                    self._report_error("Query failed", exc)
                except BaseException:
                    failed = True
                    raise
                finally:
                    # Close the stream before the adapter goes back to the pool.
                    # Abandoning it (the ``truncated`` break above) would leave a
                    # server-side cursor — and, on Postgres, an open transaction
                    # — on a connection the next query is about to reuse.
                    # Stopping at the display cap is a *successful* run that read
                    # less, not a cancellation: ``finish_early`` reports it so.
                    with contextlib.suppress(Exception):
                        if results.truncated and not cancelled:
                            await run.finish_early()
                        else:
                            await run.aclose()
                    results.loading = False
                    meta = run.metadata
                    results.mark_done(
                        cancelled=cancelled,
                        failed=failed,
                        affected_rows=meta.affected_rows if meta else None,
                        server_message=meta.server_message if meta else None,
                    )
                    self._set_results_summary(results.summary)
                    self._status().truncated = results.truncated
                    await self._record_history(sql, error=stream_error)
        except Exception as exc:
            # Failure before/around streaming — e.g. pool acquisition or
            # connection setup. Clear the spinner so the table isn't stuck.
            logger.exception("run_query_failed")
            results.loading = False
            self._report_error("Query failed", exc)
        finally:
            self._current_run = None
            self._run_executor = None
            self._query_in_flight = False
            clear_context()

    async def action_copy_cell(self) -> None:
        text = self._results().current_cell_text()
        if text is None:
            self._status().notify_status("nothing to copy", "warning")
            return
        self.copy_to_clipboard(text)
        self._status().notify_status("copied cell", "success")

    async def action_export_csv(self) -> None:
        results = self._results()
        if results.row_count == 0:
            self._status().notify_status("nothing to export", "warning")
            return
        path = _export_path()
        try:
            n = await results.export_csv(path)
        except OSError as exc:
            self._report_error("export failed", exc)
            return
        summary = f"exported {n} row{'s' if n != 1 else ''} → {path}"
        if results.truncated:
            # The grid only holds what the display cap allowed, so the export
            # is a partial one. Saying so beats a file that quietly disagrees
            # with the query.
            summary += " (display cap reached — partial export)"
            self.notify(
                f"Only the {n} displayed rows were exported; the result set is larger. "
                "Raise the row limit to export everything.",
                title="Partial export",
                severity="warning",
                timeout=8,
            )
        self._status().notify_status(summary, "success")

    async def action_set_row_limit(self) -> None:
        current_limit = self.cfg.ui.max_display_rows

        def _on_limit(new_limit: int | None) -> None:
            if new_limit is None:
                return
            try:
                # ``RegistroConfig`` validates on assignment now, so a value the
                # modal let through but the schema rejects surfaces here rather
                # than silently installing an out-of-range cap.
                self.cfg.ui.max_display_rows = new_limit
            except ValidationError as exc:
                self._report_error("Invalid row limit", exc)
                return
            with contextlib.suppress(Exception):
                self._results().max_rows = new_limit
            with contextlib.suppress(Exception):
                self._action_bar().limit = new_limit
            if new_limit == 0:
                # Every row of a result is held in memory — twice, raw and as
                # rendered — so "unlimited" on a big table is a way to run out.
                self.notify(
                    "Every row of a result will be kept in memory. Large result "
                    "sets can exhaust it; prefer a cap or a LIMIT clause.",
                    title="Row display limit removed",
                    severity="warning",
                    timeout=8,
                )
                self._status().notify_status("Row limit removed (unlimited)", "warning")
                return
            self._status().notify_status(f"Row limit set to {new_limit}")

        await self.push_screen(RowLimitModal(current_limit), _on_limit)

    async def action_cancel_query(self) -> None:
        # Snapshot both references before awaiting: action_run_query's finally
        # block can null them concurrently between the check and the await.
        run = self._current_run
        executor = self._run_executor
        if run is not None and executor is not None:
            await executor.cancel(run)

    async def action_refresh_schema(self) -> None:
        await self._refresh_schema()

    async def _refresh_schema(self) -> bool:
        """Reload the schema tree. Returns ``False`` when the refresh failed."""
        if self.state.introspector is None:
            return False
        self._status().notify_status("refreshing schema…")
        try:
            snap = await self.state.introspector.refresh(lazy=True)
        except Exception as exc:
            # Broad on purpose: this runs inside the connect worker, and an
            # exception escaping a worker exits the whole app. The introspector
            # already maps driver errors onto RegistroError; this is the
            # backstop for anything it does not.
            if not isinstance(exc, RegistroError):
                logger.exception("schema_refresh_failed")
            self._report_error("Schema refresh failed", exc)
            return False
        self._tree().populate(snap)
        self._status().notify_status(f"schema refreshed · {snap.object_count} object(s)")
        return True

    async def action_focus_editor_top(self) -> None:
        editor = self._tabs().current_editor
        if editor is None:
            return
        editor.focus()
        editor.move_cursor((0, 0))

    async def action_focus_editor_bottom(self) -> None:
        editor = self._tabs().current_editor
        if editor is None:
            return
        editor.focus()
        editor.move_cursor(editor.document.end)

    async def action_new_tab(self) -> None:
        self._tabs().new_tab()

    async def action_close_tab(self) -> None:
        self._tabs().close_current()

    async def action_command_palette(self) -> None:  # type: ignore[override]
        # Textual supports async actions at runtime; its stubs only declare
        # the sync signature. Mypy override-suppress is intentional.
        commands = [*BUILTIN_COMMANDS, *self.state.plugins.all_commands()]

        def _on_pick(result: Command | None) -> None:
            if result is None:
                return
            plugin_id = self.state.plugins.get_plugin_for_command(result.id) or "builtin"
            if plugin_id == "builtin":
                ctx = PluginContext(
                    plugin_id="builtin",
                    event_bus=self.state.bus,
                    registry=self.state.plugins,
                )
            elif self._loader is not None:
                ctx = self._loader.context_for(plugin_id)
            else:
                return
            task = asyncio.ensure_future(result.handler(ctx))
            self._palette_tasks.add(task)
            task.add_done_callback(self._palette_tasks.discard)

            def _report(done: asyncio.Task[None]) -> None:
                if done.cancelled():
                    return
                exc = done.exception()
                if exc is not None:
                    logger.exception("command_handler_failed", exc_info=exc)
                    self._report_error("Command failed", exc)

            task.add_done_callback(_report)

        await self.push_screen(CommandPaletteScreen(commands), _on_pick)

    async def action_help(self) -> None:
        await self.push_screen(HelpModal())

    async def action_show_history(self) -> None:
        if self.state.history is None:
            self._status().notify_status("history not opened", "warning")
            return
        entries = await self.state.history.recent(limit=50)

        def _on_pick(picked: HistoryEntry | None) -> None:
            if picked is None:
                return
            editor = self._tabs().current_editor
            if editor is None:
                self._tabs().new_tab(picked.sql)
            else:
                editor.text = picked.sql

        await self.push_screen(HistoryModal(entries), _on_pick)

    # ---- history ------------------------------------------------------

    async def _record_history(self, sql: str, *, error: BaseException | None = None) -> None:
        if self.state.history is None:
            return
        if self._current_run is not None:
            run = self._current_run
            query_id = run.query_id
            status_val = run.status.value
            duration = run.duration_ms
            rows = run.rows_emitted
        else:
            # Failed before a run existed. The executor still minted an id and
            # published it on ``QueryFailed``; it rides on the error's context.
            context = error.context if isinstance(error, RegistroError) else {}
            query_id = str(context.get("query_id", "-"))
            status_val = "failed"
            duration = None
            rows = 0
        detail: str | None = None
        if error is not None:
            detail = error.message if isinstance(error, RegistroError) else str(error)

        await self.state.history.add(
            HistoryEntry(
                query_id=query_id,
                connection_label=(
                    self.state.dsn.render(mask_password=True) if self.state.dsn else "?"
                ),
                sql=sql,
                status=status_val,
                duration_ms=duration,
                rows=rows,
                error=detail,
            )
        )
