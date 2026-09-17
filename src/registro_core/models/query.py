"""Query lifecycle models."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class QueryStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    STREAMING = "streaming"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class QueryHandle(BaseModel):
    """Opaque handle to an in-flight or completed query.

    Adapter-private state (driver cursor, etc.) lives off-model in the adapter.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    query_id: str = Field(default_factory=lambda: uuid4().hex)
    connection_id: str
    sql: str
    params: dict[str, Any] | None = None
    status: QueryStatus = QueryStatus.PENDING
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def mark_running(self) -> None:
        self.status = QueryStatus.RUNNING
        self.started_at = datetime.now(UTC)

    def mark_streaming(self) -> None:
        self.status = QueryStatus.STREAMING

    def mark_done(self, status: QueryStatus) -> None:
        self.status = status
        self.finished_at = datetime.now(UTC)

    @property
    def duration_ms(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at or datetime.now(UTC)
        return (end - self.started_at).total_seconds() * 1000.0


class QueryMetadata(BaseModel):
    """Metadata returned alongside results."""

    model_config = ConfigDict(frozen=True)

    column_names: list[str] = Field(default_factory=list)
    column_types: list[str] = Field(default_factory=list)
    row_count_estimate: int | None = None
    affected_rows: int | None = None
    server_message: str | None = None
