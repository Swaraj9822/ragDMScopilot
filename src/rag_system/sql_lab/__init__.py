"""SQL Lab (operator-only read-only data explorer) backend package.

Slice 1 exposes the secondary guardrail (:class:`SqlLabGuard`) that decides
whether a submitted statement is an allowed single read-only ``SELECT`` before
it can ever reach the database. The dedicated read-only Postgres role remains
the *primary* security boundary; this guard is defense-in-depth.

``SqlLabGuard``/``SqlLabValidationError`` are exported lazily (PEP 562) so that
importing lightweight leaf modules such as :mod:`rag_system.sql_lab.errors`
during :class:`rag_system.config.Settings` construction does not eagerly pull in
``guard`` -> ``copilot`` -> ``config`` and create an import cycle. The guard is
imported on first attribute access, by which time ``Settings`` is fully defined.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from rag_system.sql_lab.guard import SqlLabGuard, SqlLabValidationError
    from rag_system.sql_lab.service import SqlLabService, SqlRunResult

__all__ = ["SqlLabGuard", "SqlLabValidationError", "SqlLabService", "SqlRunResult"]


def __getattr__(name: str) -> object:
    if name in {"SqlLabGuard", "SqlLabValidationError"}:
        from rag_system.sql_lab import guard

        return getattr(guard, name)
    if name in {"SqlLabService", "SqlRunResult"}:
        from rag_system.sql_lab import service

        return getattr(service, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
