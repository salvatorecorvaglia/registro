"""RegistroApp — the top-level Textual application.

Responsibilities:
* Build the dependency graph (event bus, plugin registry, adapter, executor).
* Wire keyboard actions to engine operations.
* React to engine events (status bar updates, results streaming).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
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
    QueryFailed,
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
        self._palette_task: asyncio.Task[None] | None = None
        self._session: Session | None = None
        self._connect_lock = asyncio.Lock()
        if self.cfg.ui.keymap == "vim":
            for key, action, desc in VIM_BINDINGS:
                if (key, action, desc) in DEFAULT_BINDINGS:
                    continue
                self.bind(key, action, description=desc)

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

    async def on_unmount(self) -> None:
        if self._current_run is not None:
            with contextlib.suppress(Exception):
                await self.action_cancel_query()
        if self._palette_task is not None and not self._palette_task.done():
            self._palette_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._palette_task
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

        async def on_failed(ev: QueryFailed) -> None:
            self._report_error(ev.error_code, ev.message)

        async def on_action(ev: RunActionRequested) -> None:
            await self.run_action(ev.action)

        self.state.bus.subscribe(QueryStarted, on_started)
        self.state.bus.subscribe(QueryCompleted, on_completed)
        self.state.bus.subscribe(QueryFailed, on_failed)
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
            await self.action_refresh_schema()
            # Clear the transient message so the footer shows "Database Connected."
            bar.notify_status("")

    async def _adopt(self, session: Session | None) -> None:
        """Install *session* as the current one, closing whatever it replaces."""
        previous = self._session
        self._session = session
        self.state.dsn = session.dsn if session else None
        self.state.adapter = session.adapter if session else None
        self.state.pool = session.pool if session else None
        self.state.introspector = session.introspector if session else None
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
        detail = str(exc)
        # Plain text: StatusBar builds its own Content and never parses this,
        # so a driver message full of brackets needs no escaping here.
        self._status().notify_status(f"{headline}: {detail}", "error")
        # ``markup=False`` rather than ``escape(detail)``: a toast is plain error
        # text, and a driver message routinely contains brackets — ``TEXT[]``, a
        # bracket-quoted identifier, a Python repr. Parsed as markup, ``[/]``
        # raised MarkupError mid-render and ``near [col]`` silently deleted the
        # one identifier the user needed to see.
        self.notify(detail, title=headline, severity="error", timeout=10, markup=False)

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
        if editor is None or not editor.sql.strip():
            self._status().notify_status("nothing to run", "warning")
            return
        self._query_in_flight = True
        self.run_worker(
            self._stream_query(pool, editor.sql),
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
                    self._report_error("Query failed", exc)
                    await self._record_history(sql)
                    return
                cancelled = False
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
                    self._report_error("Query failed", exc)
                finally:
                    # Close the stream before the adapter goes back to the pool.
                    # Abandoning it (the ``truncated`` break above) would leave a
                    # server-side cursor — and, on Postgres, an open transaction
                    # — on a connection the next query is about to reuse.
                    with contextlib.suppress(Exception):
                        await run.aclose()
                    results.loading = False
                    results.mark_done(cancelled=cancelled)
                    self._set_results_summary(results.summary)
                    self._status().truncated = results.truncated
                    await self._record_history(sql)
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
        path = Path.home() / ".registro" / "exports" / f"export-{int(time.time())}.csv"
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
        if self.state.introspector is None:
            return
        self._status().notify_status("refreshing schema…")
        try:
            snap = await self.state.introspector.refresh(lazy=True)
        except RegistroError as exc:
            self._report_error("Schema refresh failed", exc)
            return
        self._tree().populate(snap)
        self._status().notify_status(f"schema refreshed · {snap.object_count} object(s)")

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
            self._palette_task = task

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

    async def _record_history(self, sql: str) -> None:
        if self.state.history is None:
            return
        if self._current_run is not None:
            run = self._current_run
            query_id = run.query_id
            status_val = run.status.value
            duration = run.duration_ms
            rows = run.rows_emitted
        else:
            query_id = "-"
            status_val = "failed"
            duration = None
            rows = 0

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
            )
        )
