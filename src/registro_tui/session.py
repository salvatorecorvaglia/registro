"""Database session lifecycle: connect, tear down, replace.

Extracted from :class:`~registro_tui.app.RegistroApp`, which was the only place
that knew how to build a connection *and* the only place that knew how to
dispose of one. Keeping the two together — and away from the UI — is what makes
"open a second connection while the first is still connecting" safe: the old
session is closed rather than orphaned.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from registro_adapters import create_adapter_for_dsn
from registro_core.errors import AdapterError
from registro_core.logging import get_logger
from registro_engine.history import QueryHistory
from registro_engine.introspector import Introspector
from registro_engine.pool import ConnectionPool

if TYPE_CHECKING:
    from registro_adapters.base import AbstractAdapter
    from registro_core.config import RegistroConfig
    from registro_core.events import EventBus
    from registro_core.models.connection import Dsn

logger = get_logger(__name__)


def default_history_path(cfg: RegistroConfig) -> Path:
    """Resolve where the query-history store lives.

    ``engine.history_path`` wins when set; otherwise the store sits alongside
    the log file under ``~/.registro``.
    """
    configured = cfg.engine.history_path
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".registro" / "history.db"


def is_in_memory(dsn: Dsn) -> bool:
    return dsn.scheme in {"sqlite", "duckdb"} and (not dsn.database or dsn.database == ":memory:")


def share_in_memory_sqlite(dsn: Dsn) -> Dsn:
    """Point an in-memory SQLite DSN at a named, shared-cache database.

    ``sqlite:///:memory:`` gives every connection its own private database, so
    the introspection connection would report an empty schema and the pool's
    connections would each see different data. A ``file:...?mode=memory&
    cache=shared`` URI is the supported way to let several connections address
    one in-memory database.
    """
    if dsn.scheme != "sqlite" or not is_in_memory(dsn):
        return dsn
    name = f"registro-mem-{uuid4().hex}"
    return dsn.model_copy(
        update={
            "database": f"file:{name}",
            # The adapter routes SQLite's own URI parameters back onto the
            # filename and turns on URI mode.
            "params": {**dsn.params, "mode": "memory", "cache": "shared"},
        }
    )


def is_single_connection_dsn(dsn: Dsn) -> bool:
    """True when the database can only be reached by one live connection."""
    return dsn.scheme == "duckdb" and is_in_memory(dsn)


async def _reject_extra_connections(_dsn: Dsn) -> AbstractAdapter:
    raise AdapterError(
        "this database accepts a single connection; the pool was preloaded with it",
        context={"pool": "single-connection"},
    )


@dataclass(slots=True)
class Session:
    """Everything one open database connection owns."""

    dsn: Dsn
    adapter: AbstractAdapter
    pool: ConnectionPool
    introspector: Introspector
    history: QueryHistory

    async def close(self) -> None:
        """Release every resource, continuing past individual failures."""
        for label, closer in (
            ("history", self.history.close),
            ("pool", self.pool.close),
            ("adapter", self.adapter.close),
        ):
            try:
                await closer()
            except Exception:
                logger.exception("session_close_failed", resource=label)


async def open_session(dsn: Dsn, cfg: RegistroConfig, bus: EventBus) -> Session:
    """Connect to *dsn* and build the session around it.

    Anything already opened is closed if a later step fails, so a partial
    connect never leaks a live connection or an open history database.
    """
    adapter: AbstractAdapter | None = None
    pool: ConnectionPool | None = None
    history: QueryHistory | None = None

    async def _factory(d: Dsn) -> AbstractAdapter:
        a = create_adapter_for_dsn(d)
        await a.connect(d)
        return a

    dsn = share_in_memory_sqlite(dsn)
    try:
        if is_single_connection_dsn(dsn):
            # DuckDB has no shared-cache equivalent: an in-memory database is
            # reachable only through the connection that created it. One
            # connection it is, and the pool owns it so that introspection and
            # query execution take turns instead of interleaving on a
            # connection whose result set is global state.
            adapter = await _factory(dsn)
            pool = ConnectionPool(dsn, _reject_extra_connections, size=1)
            await pool.preload(adapter)
        else:
            # Dedicated adapter for schema introspection — never shared with
            # query execution, so a long SELECT cannot block ``Ctrl+R``.
            adapter = await _factory(dsn)
            pool = ConnectionPool(dsn, _factory, size=cfg.engine.pool_size)

        history = QueryHistory(
            default_history_path(cfg),
            max_entries=cfg.engine.max_history_entries,
        )
        await history.open()

        return Session(
            dsn=dsn,
            adapter=adapter,
            pool=pool,
            introspector=Introspector(adapter, bus),
            history=history,
        )
    except Exception:
        for closer in (
            getattr(history, "close", None),
            getattr(pool, "close", None),
            getattr(adapter, "close", None),
        ):
            if closer is not None:
                with contextlib.suppress(Exception):
                    await closer()
        raise
