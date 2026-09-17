"""DSN query-parameter handling shared by the adapters.

A DSN's ``?key=value`` pairs used to be splatted straight into the driver's
connect call. That let a DSN reach settings the user never meant to touch —
DuckDB's ``allow_unsigned_extensions`` is one — and turned a typo into a raw
``TypeError`` from deep inside a third-party driver. Each adapter now declares
the parameters it accepts and anything else is refused by name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from registro_core.errors import AdapterConnectionError

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping


def coerce_value(value: str) -> Any:
    """Turn a DSN string value into the bool/int a driver expects."""
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if value.isdigit():
        return int(value)
    return value


def coerce_params(
    params: Mapping[str, str],
    allowed: Collection[str],
    *,
    adapter: str,
) -> dict[str, Any]:
    """Validate and type-coerce DSN parameters against an allowlist.

    Raises :class:`AdapterConnectionError` naming the offending key — a typo
    should be a clear message at connect time, not a driver stack trace.
    """
    unknown = sorted(k for k in params if k not in allowed)
    if unknown:
        raise AdapterConnectionError(
            f"{adapter}: unsupported DSN parameter(s): {', '.join(unknown)}",
            context={"adapter": adapter, "unknown": unknown, "allowed": sorted(allowed)},
        )
    return {k: coerce_value(v) for k, v in params.items()}
