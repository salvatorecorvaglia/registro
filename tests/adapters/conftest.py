"""Server-backed fixtures for the adapter conformance suite.

Postgres and MySQL need a real server. Two ways to get one, in order:

1. ``REGISTRO_PG_DSN`` / ``REGISTRO_MYSQL_DSN`` — point at your own server
   (``tests/scripts/test-integration.sh`` brings up ``docker/docker-compose.yml``
   and exports these).
2. testcontainers — started automatically when a Docker daemon is reachable.

Only when neither is available do the tests skip. Previously they were gated
on the env vars alone, which meant CI — where Docker *is* available — silently
skipped every cross-adapter conformance test.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

_PG_IMAGE = "postgres:16-alpine"
_MYSQL_IMAGE = "mysql:8.0"


def _docker_available() -> bool:
    try:
        from testcontainers.core.container import DockerClient  # noqa: PLC0415

        DockerClient().client.ping()
    except Exception:
        return False
    return True


def _normalize(url: str) -> str:
    """Strip the SQLAlchemy driver suffix testcontainers adds (``+psycopg2``)."""
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    return f"{scheme.split('+', 1)[0]}://{rest}"


@pytest.fixture(scope="session")
def postgres_dsn() -> Iterator[str]:
    env = os.getenv("REGISTRO_PG_DSN", "")
    if env:
        yield env
        return
    if not _docker_available():
        pytest.skip("no REGISTRO_PG_DSN and no Docker daemon for testcontainers")
    try:
        from testcontainers.community.postgres import PostgresContainer  # noqa: PLC0415
    except ImportError:
        from testcontainers.postgres import PostgresContainer  # noqa: PLC0415

    with PostgresContainer(_PG_IMAGE) as container:
        yield _normalize(container.get_connection_url())


@pytest.fixture(scope="session")
def mysql_dsn() -> Iterator[str]:
    env = os.getenv("REGISTRO_MYSQL_DSN", "")
    if env:
        yield env
        return
    if not _docker_available():
        pytest.skip("no REGISTRO_MYSQL_DSN and no Docker daemon for testcontainers")
    try:
        from testcontainers.community.mysql import MySqlContainer  # noqa: PLC0415
    except ImportError:
        from testcontainers.mysql import MySqlContainer  # noqa: PLC0415

    with MySqlContainer(_MYSQL_IMAGE) as container:
        yield _normalize(container.get_connection_url())
