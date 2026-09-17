"""Structured logging via structlog.

Two output modes:
* TTY: pretty, human-readable.
* Non-TTY (CI, Docker, files): line-delimited JSON.

Use :func:`get_logger` everywhere. Inject per-request context with
:func:`bind_context`.
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import sys
from contextvars import ContextVar, Token
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from structlog.types import EventDict, Processor

_request_context: ContextVar[dict[str, Any] | None] = ContextVar(
    "registro_request_context", default=None
)

_configured = False
# Open log-file streams, keyed by resolved path. Reconfiguring must never close
# a stream that is still live: structlog caches bound loggers on first use, and
# those cached loggers keep writing to the file object they were built with. A
# closed stream would turn every subsequent log call into ValueError. Streams
# are therefore reused across reconfigurations and closed only at exit.
_log_file_streams: dict[str, Any] = {}


class _LiveStderr:
    """A stderr handle that resolves ``sys.stderr`` at write time.

    ``configure_logging`` used to hand structlog ``sys.stderr`` *by value*, and
    structlog caches bound loggers on first use — so once anything swapped the
    real stream (pytest's per-test capture, a reconfiguration, a TUI taking over
    the terminal), those cached loggers kept writing to a handle that had since
    been closed and every subsequent log call raised
    ``ValueError: I/O operation on closed file``. Resolving the attribute per
    call costs nothing and makes reconfiguration safe from any host.
    """

    # ``__weakref__`` is required: ``logging`` registers stream handlers in a
    # weak set, so a slotted class without it fails with "cannot create weak
    # reference" the first time this is handed to ``basicConfig``.
    __slots__ = ("__weakref__",)

    def write(self, data: str) -> int:
        return sys.stderr.write(data)

    def flush(self) -> None:
        with contextlib.suppress(ValueError, OSError):
            sys.stderr.flush()

    def isatty(self) -> bool:
        try:
            return bool(sys.stderr.isatty())
        except (ValueError, AttributeError, OSError):
            return False


_LIVE_STDERR = _LiveStderr()


def _merge_request_context(_logger: Any, _method_name: str, event_dict: EventDict) -> EventDict:
    ctx = _request_context.get()
    if ctx:
        for key, value in ctx.items():
            event_dict.setdefault(key, value)
    return event_dict


def _open_log_stream(log_file: Path) -> Any:
    """Return an append stream for *log_file*, reusing one already open for it."""
    key = str(log_file)
    existing = _log_file_streams.get(key)
    if existing is not None and not getattr(existing, "closed", False):
        return existing
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        stream = log_file.open("a", encoding="utf-8")
    except OSError:
        # Caller falls back to stderr if the log file cannot be opened.
        return None
    _log_file_streams[key] = stream
    atexit.register(_close_log_streams)
    return stream


def _close_log_streams() -> None:
    for stream in _log_file_streams.values():
        with contextlib.suppress(Exception):
            stream.close()
    _log_file_streams.clear()


def configure_logging(
    *,
    level: str = "INFO",
    json: bool | None = None,
    log_file: Path | None = None,
    force: bool = False,
) -> None:
    """Configure structlog + stdlib logging. Idempotent unless force=True."""
    global _configured  # noqa: PLW0603 - module-level idempotency flag
    if _configured and not force:
        return

    stream: Any = _LIVE_STDERR
    if log_file is not None:
        stream = _open_log_stream(log_file) or _LIVE_STDERR

    if json is None:
        json = not stream.isatty() if hasattr(stream, "isatty") else True

    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        _merge_request_context,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    if json:
        renderer: Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[*shared_processors, structlog.processors.format_exc_info, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=stream),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        format="%(message)s",
        stream=stream,
        level=level.upper(),
        force=True,
    )

    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    if not _configured:
        configure_logging()
    bound: structlog.stdlib.BoundLogger = (
        structlog.get_logger(name) if name else structlog.get_logger()
    )
    return bound


def bind_context(**kwargs: Any) -> Token[dict[str, Any] | None]:
    """Bind keys onto the current logging context (request/query scope).

    Returns the :class:`~contextvars.Token` for the previous value. Pass it to
    :func:`reset_context` to restore exactly what was bound before, instead of
    wiping context that belongs to an unrelated caller.
    """
    current = _request_context.get() or {}
    ctx = {**current, **kwargs}
    return _request_context.set(ctx)


def reset_context(token: Token[dict[str, Any] | None]) -> None:
    """Restore the logging context captured by :func:`bind_context`."""
    with contextlib.suppress(ValueError):
        # ValueError when the token was created in a different Context — the
        # scope already ended, so there is nothing to restore.
        _request_context.reset(token)


@contextlib.contextmanager
def log_context(**kwargs: Any) -> Iterator[None]:
    """Scope ``kwargs`` onto the logging context for the duration of the block."""
    token = bind_context(**kwargs)
    try:
        yield
    finally:
        reset_context(token)


def clear_context() -> None:
    """Drop the entire request-scoped logging context.

    Prefer :func:`log_context` / :func:`reset_context`; this wipes keys bound by
    any caller and exists for process-level teardown (e.g. CLI exit).
    """
    _request_context.set(None)
