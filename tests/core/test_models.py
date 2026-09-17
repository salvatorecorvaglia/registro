from __future__ import annotations

import pytest

from registro_core.errors import ConfigError
from registro_core.models.connection import Dsn, mask_dsn_password
from registro_core.models.query import QueryHandle, QueryStatus


def test_dsn_parse_sqlite() -> None:
    d = Dsn.parse("sqlite:///:memory:")
    assert d.scheme == "sqlite"
    assert d.database == ":memory:"


def test_dsn_parse_postgres() -> None:
    d = Dsn.parse("postgres://user:pw@localhost:5432/mydb?sslmode=require")
    assert d.scheme == "postgres"
    assert d.username == "user"
    assert d.password is not None
    assert d.password.get_secret_value() == "pw"
    assert d.host == "localhost"
    assert d.port == 5432
    assert d.database == "mydb"
    assert d.params["sslmode"] == "require"


def test_dsn_unknown_scheme_raises() -> None:
    # ConfigError, not pydantic's ValidationError: every other DSN failure in
    # ``parse`` raises ConfigError, and callers render the message straight to
    # the user. A leaked ValidationError put a multi-line pydantic dump on
    # screen — in a modal that parses its text as Rich markup.
    with pytest.raises(ConfigError) as excinfo:
        Dsn.parse("oracle://foo")
    assert "unsupported scheme: oracle" in str(excinfo.value)
    # The message must stay free of square brackets, which Rich would eat.
    assert "[" not in excinfo.value.message


def test_dsn_missing_scheme_raises() -> None:
    with pytest.raises(ConfigError):
        Dsn.parse("just-a-path")


def test_dsn_render_masks_password() -> None:
    d = Dsn.parse("postgres://u:secret@h:5432/db")
    out = d.render(mask_password=True)
    assert "secret" not in out
    assert "***" in out


def test_mask_dsn_password_complex() -> None:
    masked = mask_dsn_password("postgres://user:p@ss@localhost:5432/db?email=a@b.com")
    assert "p@ss" not in masked
    assert "***" in masked
    assert "email=a@b.com" in masked

    masked_simple = mask_dsn_password("sqlite:///:memory:")
    assert masked_simple == "sqlite:///:memory:"


def test_query_handle_lifecycle() -> None:
    h = QueryHandle(connection_id="c", sql="select 1")
    assert h.status == QueryStatus.PENDING
    h.mark_running()
    assert h.started_at is not None
    h.mark_done(QueryStatus.SUCCEEDED)
    assert h.finished_at is not None
    assert h.duration_ms is not None


# ---- DSN round-trip (absolute paths used to degrade to relative ones) ----


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///:memory:",
        "duckdb:///:memory:",
        "sqlite:///relative.db",
        "sqlite:////abs/path/foo.db",
        "duckdb:////abs/path/foo.duckdb",
        "postgres://registro:registro@localhost:5432/registro",
        "mysql://registro:registro@localhost:3306/registro",
        "postgres://user@host/db",
    ],
)
def test_dsn_render_round_trips(url: str) -> None:
    """``parse -> render -> parse`` must be a fixed point.

    ``render`` used to special-case which schemes got a leading slash, so
    ``sqlite:////abs/foo.db`` (database ``/abs/foo.db``) re-rendered as
    ``sqlite:///abs/foo.db`` and reparsed as the *relative* ``abs/foo.db`` —
    silently a different database. The rendered form is what the status bar
    shows and what query history stores, so the drift was user-visible.
    """
    first = Dsn.parse(url)
    rendered = first.render(mask_password=False)
    second = Dsn.parse(rendered)

    assert second.scheme == first.scheme
    assert second.database == first.database
    assert second.host == first.host
    assert second.port == first.port
    assert second.username == first.username
    assert second.render(mask_password=False) == rendered


# ---- secrets carried in DSN query parameters ----


@pytest.mark.parametrize(
    "param",
    ["password", "passwd", "pwd", "sslpassword", "auth_token", "client_secret", "PASSWORD"],
)
def test_secret_query_params_are_masked(param: str) -> None:
    url = f"postgres://alice@db.example.com:5432/app?{param}=s3cr3t&sslmode=require"

    assert "s3cr3t" not in mask_dsn_password(url)
    assert "s3cr3t" not in Dsn.parse(url).render(mask_password=True)
    # Non-secret parameters must survive so a TLS misconfiguration stays legible.
    assert "sslmode=require" in mask_dsn_password(url)


def test_non_secret_params_are_untouched() -> None:
    url = "sqlite:///foo.db?mode=memory&cache=shared"
    assert mask_dsn_password(url) == url


def test_render_unmasked_still_yields_the_real_secret() -> None:
    """Adapters connect with ``mask_password=False``; masking must not leak into it."""
    url = "postgres://alice@h:5432/app?password=s3cr3t"
    assert "s3cr3t" in Dsn.parse(url).render(mask_password=False)


def test_invalid_port_raises_config_error() -> None:
    """``urlparse`` defers port parsing to attribute access, inside ``parse``."""
    with pytest.raises(ConfigError):
        Dsn.parse("postgres://host:notaport/db")
