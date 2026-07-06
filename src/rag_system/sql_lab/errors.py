"""Exception hierarchy for SQL Lab.

These errors carry human-readable, secret-free messages. In particular,
``SqlLabConfigError`` identifies missing configuration by key name and never
embeds a credential value, satisfying the "keyed, value-free" requirement
(R1.5). The errors deliberately import nothing from ``rag_system.config`` so
that ``Settings`` can raise them without a circular import.
"""


class SqlLabError(Exception):
    """Base class for all SQL Lab backend errors."""


class SqlLabConfigError(SqlLabError):
    """Raised when required SQL Lab configuration is missing or invalid.

    The message names the offending configuration key(s) and never includes the
    (possibly secret) configuration value.
    """


class SqlLabConnectionError(SqlLabError):
    """Raised when the executor cannot establish a viewer-role connection.

    The message indicates that the viewer database connection failed and never
    includes the (secret) credential values (R1.6).
    """


class SqlLabTimeoutError(SqlLabError):
    """Raised when a query exceeds the transaction-local ``statement_timeout``.

    The message describes that the configured Statement_Timeout was exceeded
    (R4.9).
    """


class SqlLabExecutionError(SqlLabError):
    """Raised when query execution fails for a reason other than timeout.

    The message carries the underlying database error message (R4.13).
    """


class SqlLabAuditError(SqlLabError):
    """Raised when an audit record cannot be persisted.

    Unlike the best-effort observability log/trace stores, SQL Lab audit
    persistence is *mandatory*: if a record cannot be written the request must
    not return result rows to the caller (R8.6). The store therefore surfaces a
    persistence failure as this error rather than swallowing it, so the service
    layer can convert it into an error response and withhold the Result_Set.
    """


class SqlLabAnalysisError(SqlLabError):
    """Raised when the AI auto-dashboard analysis cannot be completed.

    Covers the language model being unavailable or not responding within the
    60-second budget (enforced at the Gemini client's ``HttpOptions`` level).
    The analyzer maps any transport/timeout failure from the structured-output
    client to this error so the ``POST /sql/analyze`` route can return an
    "analysis could not be completed" response while leaving the source
    Result_Set unchanged (R9.10). The message never embeds the sampled rows.
    """


__all__ = [
    "SqlLabError",
    "SqlLabConfigError",
    "SqlLabConnectionError",
    "SqlLabTimeoutError",
    "SqlLabExecutionError",
    "SqlLabAuditError",
    "SqlLabAnalysisError",
]
