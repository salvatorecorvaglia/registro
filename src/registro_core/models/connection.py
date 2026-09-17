"""Connection / DSN models."""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlparse, urlsplit, urlunsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from registro_core.errors import ConfigError

Scheme = Literal["duckdb", "sqlite", "postgres", "postgresql", "mysql"]
_VALID_SCHEMES = {"duckdb", "sqlite", "postgres", "postgresql", "mysql"}


# Query-parameter names whose *value* is a credential. Matched case-insensitively
# as a substring, so ``sslpassword`` and ``auth_token`` are both caught.
#
# Deliberately excludes ``sslkey``/``sslcert``/``sslrootcert``: those are file
# paths, not secrets, and redacting them turns a TLS misconfiguration into an
# unreadable error. The private key itself never travels in a DSN.
_SECRET_PARAM_SUBSTRINGS: tuple[str, ...] = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "credential",
)

_REDACTED = "***"


def is_secret_param(name: str) -> bool:
    """True when a DSN query parameter carries a credential in its value."""
    lowered = name.lower()
    return any(marker in lowered for marker in _SECRET_PARAM_SUBSTRINGS)


def _mask_query(query: str) -> str:
    """Redact the values of secret-bearing pairs in a URL query string."""
    if not query:
        return query
    pairs = parse_qsl(query, keep_blank_values=True)
    if not any(is_secret_param(k) for k, _ in pairs):
        return query
    # ``safe="*"`` keeps the redaction readable as ``***`` rather than
    # percent-encoding it to ``%2A%2A%2A`` in every log line.
    return urlencode([(k, _REDACTED if is_secret_param(k) else v) for k, v in pairs], safe="*")


def mask_dsn_password(url: str) -> str:
    """Mask credentials in a DSN URL to prevent password leaks in logs/errors.

    Covers *both* places a DSN can carry a secret: the userinfo segment
    (``scheme://user:pw@host``) and the query string (``?password=pw``). Only
    masking the former left ``?password=`` verbatim in the log file, the status
    bar, and the query-history store.
    """
    try:
        parsed = urlsplit(url)
        query = _mask_query(parsed.query)
        netloc = parsed.netloc
        if parsed.password:
            user = quote(unquote(parsed.username), safe="") if parsed.username else ""
            host = parsed.hostname or ""
            port = f":{parsed.port}" if parsed.port is not None else ""
            netloc = f"{user}:{_REDACTED}@{host}{port}"
        if netloc == parsed.netloc and query == parsed.query:
            return url
        return urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))
    except Exception:
        # Unparseable URL — fall back to a textual substitution of the userinfo.
        return re.sub(r"([^:]+://[^:]+:)[^@]+(@)", r"\1***\2", url)


class Dsn(BaseModel):
    """Parsed DSN. Use :meth:`parse` to build from a URL string."""

    model_config = ConfigDict(frozen=True)

    scheme: str
    username: str | None = None
    password: SecretStr | None = None
    host: str | None = None
    port: int | None = None
    database: str | None = None
    params: dict[str, str] = Field(default_factory=dict)

    @field_validator("scheme")
    @classmethod
    def _check_scheme(cls, v: str) -> str:
        v = v.lower()
        if v not in _VALID_SCHEMES:
            raise ValueError(f"unsupported scheme: {v}; valid: {', '.join(sorted(_VALID_SCHEMES))}")
        return v

    @classmethod
    def parse(cls, url: str) -> Dsn:
        masked = mask_dsn_password(url)
        try:
            parsed = urlparse(url)
        except ValueError as exc:
            raise ConfigError(f"invalid DSN: {masked}", context={"error": str(exc)}) from exc
        if not parsed.scheme:
            raise ConfigError(f"DSN missing scheme: {masked}")
        # path: "/dbname" or "/path/to/file.db" or "/:memory:"
        database: str | None
        if parsed.path in ("", "/"):
            database = None
        else:
            # Strip exactly one leading slash. ``sqlite:///foo.db`` → ``foo.db``;
            # ``sqlite:////abs/foo.db`` → ``/abs/foo.db``. The literal
            # ``/:memory:`` collapses to ``:memory:``.
            database = parsed.path[1:] if parsed.path.startswith("/") else parsed.path
        params: dict[str, str] = {}
        if parsed.query:
            params = dict(parse_qsl(parsed.query))
        try:
            return cls(
                scheme=parsed.scheme.lower(),
                username=unquote(parsed.username) if parsed.username else None,
                password=SecretStr(unquote(parsed.password)) if parsed.password else None,
                host=parsed.hostname,
                port=parsed.port,
                database=database,
                params=params,
            )
        except ValidationError as exc:
            # Field validation (an unsupported scheme, a bad port) must surface
            # as a Registro error like every other DSN failure. Letting pydantic's
            # own multi-line dump escape put a stack-trace-shaped message in
            # front of the user, in a UI that renders it as Rich markup.
            detail = (
                "; ".join(e["msg"].removeprefix("Value error, ") for e in exc.errors())
                or "invalid DSN"
            )
            raise ConfigError(detail, context={"dsn": masked}) from exc
        except ValueError as exc:
            # ``urlparse`` defers port parsing to attribute access, so a DSN
            # like ``postgres://h:notaport/db`` raises only once ``parsed.port``
            # is read — which happens above, inside this ``try``.
            raise ConfigError(f"invalid DSN: {exc}", context={"dsn": masked}) from exc

    def render(self, *, mask_password: bool = True) -> str:
        userinfo = ""
        if self.username or self.password:
            if self.username:
                userinfo = quote(self.username, safe="")
            if self.password:
                userinfo += ":" + (
                    "***" if mask_password else quote(self.password.get_secret_value(), safe="")
                )
            userinfo += "@"
        host = self.host or ""
        port = f":{self.port}" if self.port else ""
        # ``parse`` strips exactly one leading slash off the path, so ``render``
        # adds exactly one back. Anything more clever breaks the round trip:
        # ``sqlite:////abs/foo.db`` parses to ``/abs/foo.db``, and re-rendering
        # it without the second slash produced ``sqlite:///abs/foo.db``, which
        # reparses as the *relative* path ``abs/foo.db`` — a different database.
        db = "/" + self.database if self.database else ""
        query = ""
        if self.params:
            query = "?" + "&".join(
                f"{quote(k)}={_REDACTED if mask_password and is_secret_param(k) else quote(v)}"
                for k, v in self.params.items()
            )
        return f"{self.scheme}://{userinfo}{host}{port}{db}{query}"


class ConnectionInfo(BaseModel):
    """Runtime info about an active connection."""

    model_config = ConfigDict(frozen=True)

    connection_id: str = Field(default_factory=lambda: uuid4().hex)
    dsn: Dsn
    label: str | None = None

    @property
    def display_name(self) -> str:
        return self.label or self.dsn.render(mask_password=True)
