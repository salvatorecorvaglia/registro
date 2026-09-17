"""Typer-powered Registro CLI.

Commands:
    registro tui --dsn <url>         launch the TUI
    registro run --dsn <url> "<sql>" one-shot query, prints results as a Rich table
    registro connect --dsn <url>     connection sanity check
    registro plugins list            list discovered plugins
    registro version                 print version + supported drivers

``--dsn`` reads ``REGISTRO_DSN`` from the environment when omitted.
"""

from __future__ import annotations

import asyncio
import csv
import json
import sys
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from registro_adapters import create_adapter_for_dsn, default_registry
from registro_core.config import RegistroConfig, load_config
from registro_core.errors import (
    QueryCancelledError,
    QueryError,
    QueryTimeoutError,
    RegistroError,
)
from registro_core.events import EventBus
from registro_core.export import csv_safe_row
from registro_core.logging import clear_context, configure_logging, get_logger
from registro_core.models.connection import Dsn
from registro_engine.executor import QueryExecutor

logger = get_logger(__name__)
console = Console()


class OutputFormat(StrEnum):
    TABLE = "table"
    CSV = "csv"
    JSON = "json"


def _render(fmt: OutputFormat, columns: list[str], rows: list[tuple[Any, ...]]) -> None:
    """Write *rows* to stdout in the requested format."""
    if fmt is OutputFormat.CSV:
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(columns)
        # Defused so a value like ``=1+1`` is text in the spreadsheet rather
        # than a formula it evaluates on open.
        writer.writerows(csv_safe_row(r) for r in rows)
        return
    if fmt is OutputFormat.JSON:
        payload = [dict(zip(columns, row, strict=False)) for row in rows]
        # ``default=str`` so dates, Decimals and UUIDs serialize instead of
        # aborting the whole output on the last row.
        json.dump(payload, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return

    if not columns:
        return
    table = Table(show_lines=False, highlight=True)
    for name in columns:
        table.add_column(escape(name), overflow="fold")
    for row in rows:
        table.add_row(*(escape(str(v)) if v is not None else "[dim]NULL[/]" for v in row))
    console.print(table)


app = typer.Typer(
    name="registro",
    help="A terminal-first database workspace for modern engineers",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
plugins_app = typer.Typer(help="Plugin management.")
app.add_typer(plugins_app, name="plugins")


@app.callback()
def _root(
    log_level: Annotated[
        str | None, typer.Option("--log-level", help="Log level.", envvar="REGISTRO_LOG_LEVEL")
    ] = None,
) -> None:
    # A conservative default so anything logged before a command has resolved
    # its config still goes somewhere sensible. Commands re-configure with
    # ``force=True`` once the config file and env overrides are known.
    configure_logging(level=log_level or "WARNING")


# Shared ``--config`` option. Every command that reads configuration accepts it,
# so ``run`` and ``connect`` are configurable the same way ``tui`` is.
_ConfigOption = Annotated[str, typer.Option("--config", help="Path to config TOML.")]


def _resolve_config(config_path: str, log_level: str | None) -> RegistroConfig:
    """Load layered config and point logging at the level it resolves to.

    ``run`` and ``connect`` used to skip this entirely: they never called
    ``load_config``, so the config file, every ``REGISTRO_<section>__<field>``
    env override, and the configured log level applied to the TUI only — while
    the README documented the layering unconditionally.

    An explicit ``--log-level`` still wins over the file, matching the
    documented precedence (defaults -> TOML -> env -> CLI).
    """
    cfg = load_config(Path(config_path) if config_path else None)
    configure_logging(level=log_level or cfg.logging.level, json=cfg.logging.json_lines, force=True)
    return cfg


def _cli_log_level(ctx: typer.Context) -> str | None:
    """The ``--log-level`` the user passed to the root callback, if any."""
    parent = ctx.parent
    if parent is None:
        return None
    value = parent.params.get("log_level")
    return str(value) if value is not None else None


@app.command()
def version() -> None:
    """Print Registro version and discovered adapter drivers."""
    from registro_core.version import __version__  # noqa: PLC0415 - keep CLI startup fast

    console.print(f"[bold cyan]registro[/] {__version__}")
    console.print(f"adapters: {', '.join(default_registry().names()) or '[none]'}")


@app.command()
def connect(
    ctx: typer.Context,
    dsn: Annotated[
        str,
        typer.Option(
            "--dsn",
            envvar="REGISTRO_DSN",
            help="Database URL, e.g. duckdb:///:memory:",
        ),
    ],
    config_path: _ConfigOption = "",
) -> None:
    """Open a connection to verify the DSN is reachable."""
    try:
        _resolve_config(config_path, _cli_log_level(ctx))
    except RegistroError as exc:
        console.print(f"[bold red]ERROR[/] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc

    async def _go() -> int:
        try:
            parsed = Dsn.parse(dsn)
            adapter = create_adapter_for_dsn(parsed)
            await adapter.connect(parsed)
            try:
                ok = await adapter.ping()
            finally:
                await adapter.close()
        except Exception as exc:
            console.print(f"[bold red]ERROR[/] {escape(str(exc))}")
            return 1
        if not ok:
            console.print("[bold red]FAIL[/] ping failed")
            return 1
        # ``escape``: a database name is user-supplied and this line is markup.
        console.print(
            f"[bold green]OK[/] connected to "
            f"{escape(parsed.scheme)}://…/{escape(parsed.database or '')}"
        )
        return 0

    raise typer.Exit(code=asyncio.run(_go()))


@app.command("run")
def run_query(
    ctx: typer.Context,
    sql: Annotated[str, typer.Argument(help="SQL to execute.")],
    dsn: Annotated[str, typer.Option("--dsn", envvar="REGISTRO_DSN", help="Database URL.")],
    limit: Annotated[int, typer.Option("--limit", min=1, help="Max rows to print.")] = 200,
    timeout: Annotated[
        float | None,
        typer.Option(
            "--timeout",
            min=0,
            help=(
                "Abort the query after this many seconds. 0 disables the timeout. "
                "Defaults to engine.default_timeout_seconds from the config."
            ),
        ),
    ] = None,
    config_path: _ConfigOption = "",
    output: Annotated[
        OutputFormat,
        typer.Option(
            "--format",
            "-f",
            help="Output format. 'csv' and 'json' are machine-readable (no styling).",
        ),
    ] = OutputFormat.TABLE,
) -> None:
    """Execute one query and print its results.

    ``--format csv`` / ``--format json`` write plain data to stdout so the
    results can be piped into other tools; the row-count summary goes to
    stderr so it never contaminates them.
    """

    try:
        cfg = _resolve_config(config_path, _cli_log_level(ctx))
    except RegistroError as exc:
        console.print(f"[bold red]ERROR[/] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc
    # ``is not None`` rather than ``or``: ``--timeout 0`` is a deliberate
    # "no timeout", not an absent value to fill in from the config.
    effective_timeout = timeout if timeout is not None else cfg.engine.default_timeout_seconds

    async def _go() -> int:
        try:
            parsed = Dsn.parse(dsn)
            adapter = create_adapter_for_dsn(parsed)
            await adapter.connect(parsed)
        except Exception as exc:
            console.print(f"[bold red]CONNECTION ERROR[/] {escape(str(exc))}")
            return 1
        bus = EventBus()
        try:
            executor = QueryExecutor(adapter, bus)
            run_handle = await executor.run(sql, timeout=effective_timeout)
            columns: list[str] = []
            rows: list[tuple[Any, ...]] = []
            try:
                async for batch in run_handle.rows:
                    if not columns and batch.columns:
                        columns = [c.name for c in batch.columns]
                    for row in batch.rows:
                        if len(rows) >= limit:
                            break
                        rows.append(row)
                    if len(rows) >= limit:
                        break
            finally:
                # Release the adapter's cursor/transaction when --limit cut the
                # stream short, rather than leaving it to the garbage collector.
                await run_handle.aclose()

            _render(output, columns, rows)
            # Rows *printed* and rows *read* diverge whenever ``--limit`` cuts
            # the stream short. Reporting only the latter meant printing 200
            # rows under a line that claimed 1000.
            elapsed = f"{run_handle.duration_ms or 0:.1f} ms"
            if run_handle.rows_emitted > len(rows):
                summary = (
                    f"{len(rows)} of {run_handle.rows_emitted} row(s) in {elapsed} "
                    f"(--limit {limit} reached)"
                )
            else:
                summary = f"{len(rows)} row(s) in {elapsed}"
            if output is OutputFormat.TABLE:
                console.print(f"[dim]{summary}[/]")
            else:
                # stderr: stdout belongs to the data being piped.
                Console(stderr=True).print(f"[dim]{summary}[/]")
            return 0
        except QueryTimeoutError as exc:
            console.print(f"[bold red]TIMEOUT[/] {escape(str(exc))}")
            return 1
        except QueryCancelledError:
            console.print("[yellow]query cancelled[/]")
            return 1
        except (QueryError, RegistroError) as exc:
            console.print(f"[bold red]QUERY ERROR[/] {escape(str(exc))}")
            return 1
        except Exception as exc:
            logger.exception("cli_run_unexpected_error")
            console.print(f"[bold red]ERROR[/] {escape(str(exc))}")
            return 1
        finally:
            clear_context()
            await bus.close()
            await adapter.close()

    raise typer.Exit(code=asyncio.run(_go()))


@app.command()
def tui(
    ctx: typer.Context,
    dsn: Annotated[
        str,
        typer.Option("--dsn", envvar="REGISTRO_DSN", help="Initial DSN to connect to."),
    ] = "",
    config_path: Annotated[str, typer.Option("--config", help="Path to config TOML.")] = "",
) -> None:
    """Launch the Registro TUI."""
    # Lazy imports — keep `registro version` and `registro run` fast by not
    # importing Textual unless we actually launch the TUI.
    from pathlib import Path  # noqa: PLC0415

    from registro_tui.app import RegistroApp  # noqa: PLC0415

    cli_log_level = ctx.parent.params.get("log_level") if ctx.parent else None
    overrides = {}
    if cli_log_level is not None:
        overrides["logging"] = {"level": cli_log_level}

    try:
        cfg = load_config(Path(config_path) if config_path else None, **overrides)
        initial_dsn = Dsn.parse(dsn) if dsn else None
    except Exception as exc:
        console.print(f"[bold red]ERROR[/] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc
    # Ensure the terminal advertises truecolor support so Textual renders
    # the theme's hex colours correctly.  In some environments (IDE embedded
    # terminals, subprocesses, screen sessions) COLORTERM / TERM may be
    # unset, which causes Textual to degrade to basic 16-colour mode where
    # foreground and background can collapse to the same shade.
    import os  # noqa: PLC0415

    os.environ.setdefault("COLORTERM", "truecolor")
    os.environ.setdefault("TERM", "xterm-256color")

    RegistroApp(config=cfg, initial_dsn=initial_dsn).run()


@plugins_app.command("list")
def plugins_list() -> None:
    """List discovered Registro plugins."""
    # Lazy: only pulled in for `registro plugins list`.
    from registro_plugins.loader import PluginLoader  # noqa: PLC0415
    from registro_plugins.registry import PluginRegistry  # noqa: PLC0415

    bus = EventBus()
    loader = PluginLoader(PluginRegistry(), bus)
    eps = loader.discover()
    if not eps:
        console.print("[yellow]no plugins found[/]")
        return
    table = Table("name", "module", title="Discovered plugins")
    for ep in eps:
        table.add_row(ep.name, ep.value)
    console.print(table)


def main() -> None:  # entry-point shim
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
