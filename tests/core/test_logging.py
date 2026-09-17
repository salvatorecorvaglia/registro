from __future__ import annotations

import json
from typing import TYPE_CHECKING

from registro_core.logging import (
    bind_context,
    clear_context,
    configure_logging,
    get_logger,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_get_logger_auto_configures() -> None:
    logger = get_logger("registro.test")
    # A bound structlog logger exposes the standard level methods.
    assert callable(logger.info)
    assert callable(logger.warning)


def test_configure_logging_is_idempotent_without_force(tmp_path: Path) -> None:
    first = tmp_path / "a.log"
    second = tmp_path / "b.log"
    configure_logging(log_file=first, force=True)
    # Without force the second call is a no-op, so b.log is never created.
    configure_logging(log_file=second, force=False)
    assert not second.exists()


def test_json_output_written_to_log_file(tmp_path: Path) -> None:
    log_file = tmp_path / "registro.log"
    configure_logging(json=True, log_file=log_file, force=True)
    get_logger("registro.test").info("hello_event", answer=42)

    lines = [line for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    record = json.loads(lines[-1])
    assert record["event"] == "hello_event"
    assert record["answer"] == 42
    assert record["level"] == "info"


def test_bind_context_merges_into_records(tmp_path: Path) -> None:
    log_file = tmp_path / "ctx.log"
    configure_logging(json=True, log_file=log_file, force=True)
    try:
        bind_context(query_id="q-123")
        get_logger("registro.test").info("with_context")
    finally:
        clear_context()

    record = json.loads(log_file.read_text(encoding="utf-8").splitlines()[-1])
    assert record["query_id"] == "q-123"


def test_clear_context_removes_bound_keys(tmp_path: Path) -> None:
    log_file = tmp_path / "cleared.log"
    configure_logging(json=True, log_file=log_file, force=True)
    bind_context(query_id="q-999")
    clear_context()
    get_logger("registro.test").info("after_clear")

    record = json.loads(log_file.read_text(encoding="utf-8").splitlines()[-1])
    assert "query_id" not in record


def test_level_filtering_drops_below_threshold(tmp_path: Path) -> None:
    log_file = tmp_path / "level.log"
    configure_logging(level="WARNING", json=True, log_file=log_file, force=True)
    logger = get_logger("registro.test")
    logger.debug("debug_dropped")
    logger.warning("warning_kept")

    text = log_file.read_text(encoding="utf-8")
    assert "warning_kept" in text
    assert "debug_dropped" not in text
