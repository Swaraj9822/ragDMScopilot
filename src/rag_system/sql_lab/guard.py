"""SQL Lab parser guardrail (secondary defense-in-depth).

The dedicated read-only Postgres role is the *primary* security boundary; this
``sqlglot``-based guard is a **secondary guardrail** that catches obvious
mistakes and injection attempts before a statement reaches the database. It is
deliberately fail-closed: anything that is not provably a single read-only
``SELECT`` is rejected, and every rejection names the specific reason.

The guard reuses :func:`rag_system.copilot._strip_sql_comments` for
string-literal-aware comment removal so that comment markers hiding inside a
string literal (e.g. ``'a--b'`` or ``'/*x*/'``) are never mistaken for real
comments and cannot be used to smuggle a statement terminator or write keyword
past the checks below.

Unlike :class:`rag_system.copilot.CopilotSqlGuard` (which *requires* an
aggregate and rejects ``SELECT *``), SQL Lab must *allow* ``SELECT`` and
``SELECT *`` detail queries, so this is a distinct guard with SQL-Lab-specific
rules (single statement, no CTE in v1, no writes/DDL/administrative commands,
no denylisted sensitive-table references).
"""

from __future__ import annotations

from collections.abc import Iterable

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from rag_system.copilot import _strip_sql_comments, find_denied_function


class SqlLabValidationError(ValueError):
    """Raised when a submitted statement is not an allowed read-only SELECT.

    The message always names the specific rejection reason (disallowed
    operation, multiple statements, ``WITH`` clause, parse failure, empty
    input, or sensitive-table reference).
    """


# Data-modifying and DDL/administrative statement nodes, paired with the
# human-readable operation name reported in the rejection message. These may
# appear at the statement root (e.g. ``INSERT ...``) or nested inside a
# subquery/derived table of an otherwise ``SELECT`` root (e.g.
# ``SELECT * FROM (DELETE ... RETURNING *) t``), so the guard scans the whole
# parsed tree for them.
_FORBIDDEN_NODES: tuple[tuple[type[exp.Expression], str], ...] = (
    (exp.Insert, "INSERT"),
    (exp.Update, "UPDATE"),
    (exp.Delete, "DELETE"),
    (exp.Merge, "MERGE"),
    (exp.TruncateTable, "TRUNCATE"),
    (exp.Create, "CREATE"),
    (exp.Alter, "ALTER"),
    (exp.Drop, "DROP"),
    (exp.Grant, "GRANT"),
    (exp.Revoke, "REVOKE"),
    (exp.Copy, "COPY"),
    (exp.Set, "SET"),
)

_FORBIDDEN_TYPES: tuple[type[exp.Expression], ...] = tuple(
    node_type for node_type, _ in _FORBIDDEN_NODES
) + (exp.Command,)

# The subset of forbidden nodes that actually *modify data* (as opposed to DDL /
# administrative commands). When one of these is found anywhere in an otherwise
# read-only tree (e.g. a data-modifying CTE such as
# ``WITH x AS (INSERT ... RETURNING *) SELECT * FROM x`` or a data-modifying
# sub-select), the rejection message explicitly states that data-modifying
# operations are not permitted (Requirement 11.7).
_DATA_MODIFYING_TYPES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.TruncateTable,
)


class SqlLabGuard:
    """Fail-closed allowlist: a single read-only ``SELECT`` and nothing else.

    ``sensitive_tables`` is a denylist of table names (matched
    case-insensitively) that must never be referenced.

    ``allow_cte`` selects between two read-only allow-lists:

    * ``allow_cte=False`` (v1 default): any ``WITH`` clause (common table
      expression) is rejected outright, so only a bare single read-only
      ``SELECT`` is permitted.
    * ``allow_cte=True`` (Slice 5 read-only CTE support): a single ``SELECT``
      whose ``WITH`` clause and all nested sub-queries at every level of the
      parsed tree are read-only sub-selects is allowed. Because ``validate``
      scans the *entire* parsed tree for data-modifying and DDL/administrative
      nodes, any data-modifying operation nested at any depth — including a
      Postgres data-modifying CTE such as
      ``WITH x AS (INSERT ... RETURNING *) SELECT * FROM x`` — is still
      rejected with a message stating that data-modifying operations are not
      permitted. The statement root must still be a ``SELECT``; a
      ``WITH ... INSERT/UPDATE/DELETE`` whose root is the write node is rejected
      because the root is not a ``SELECT``.
    """

    def __init__(self, sensitive_tables: Iterable[str], allow_cte: bool = False) -> None:
        self._sensitive_tables = frozenset(
            name.strip().lower() for name in sensitive_tables if name and name.strip()
        )
        self._allow_cte = allow_cte

    def validate(self, sql: str) -> str:
        """Return the normalized SQL when allowed; raise otherwise.

        Fail-closed pipeline:

        1. strip comments (string-literal aware); reject if empty/whitespace.
        2. parse with ``sqlglot``; reject on parse failure.
        3. reject if more than one statement (ignoring one optional trailing ``;``).
        4. reject if the root is not a ``SELECT``.
        5. reject any write/DDL/administrative node anywhere in the tree; a
           data-modifying node (``INSERT``/``UPDATE``/``DELETE``/``MERGE``/
           ``TRUNCATE``) found at any depth — including inside a data-modifying
           CTE or sub-select — is rejected with a message stating that
           data-modifying operations are not permitted.
        6. when ``allow_cte`` is ``False`` (v1), reject a ``WITH`` clause (CTE);
           when ``allow_cte`` is ``True``, a read-only ``WITH``/sub-select is
           allowed (step 5 already rejected any nested data modification).
        7. reject any reference to a denylisted sensitive table.
        8. ``SELECT`` and ``SELECT *`` are both allowed.
        """
        # 1. Strip comments (string-literal aware), whitespace, and a single
        # optional trailing terminator before classifying.
        normalized = _strip_sql_comments(sql).strip().rstrip(";").strip()
        if not normalized:
            raise SqlLabValidationError(
                "Empty input: the statement is empty or contains only whitespace "
                "after comment removal."
            )

        # 2. Parse; a parse failure is a rejection reason of its own.
        try:
            statements = [
                statement
                for statement in sqlglot.parse(normalized, read="postgres")
                if statement is not None
            ]
        except SqlglotError as exc:
            raise SqlLabValidationError(f"Parse failure: could not parse SQL ({exc}).") from exc

        # 3. Exactly one statement (a single trailing ``;`` was already removed).
        if len(statements) != 1:
            raise SqlLabValidationError(
                "Multiple statements: only a single SQL statement is allowed."
            )
        statement = statements[0]

        # 4. The root must be a SELECT. A non-SELECT root (INSERT/UPDATE/DELETE/
        # DDL/administrative command) is rejected naming the disallowed operation.
        if not isinstance(statement, exp.Select):
            raise SqlLabValidationError(
                f"Disallowed operation: {self._operation_name(statement)} statements "
                "are not allowed; only a single read-only SELECT is permitted."
            )

        # 5. No write/DDL/administrative node anywhere in the tree (e.g. hidden
        # inside a derived table / subquery of the SELECT, or inside a
        # data-modifying CTE such as ``WITH x AS (INSERT ... RETURNING *) ...``).
        # This tree-wide scan is what makes the ``allow_cte=True`` path safe: a
        # read-only ``WITH``/sub-select is allowed, but any data-modifying node
        # at any depth is still caught here.
        forbidden = next(statement.find_all(*_FORBIDDEN_TYPES), None)
        if forbidden is not None:
            operation = self._operation_name(forbidden)
            if isinstance(forbidden, _DATA_MODIFYING_TYPES):
                raise SqlLabValidationError(
                    f"Disallowed operation: {operation} is a data-modifying "
                    "operation, which is not permitted in a read-only query."
                )
            raise SqlLabValidationError(
                f"Disallowed operation: {operation} is not allowed in a "
                "read-only query."
            )

        # 6. Reject WITH (CTE) unless read-only CTE support is enabled. When
        # ``allow_cte`` is True the ``WITH`` and its nested sub-selects are
        # permitted; step 5 above has already rejected any data modification
        # nested anywhere in the tree.
        if not self._allow_cte and next(statement.find_all(exp.With), None) is not None:
            raise SqlLabValidationError(
                "WITH clause: common table expressions are not permitted in this version."
            )

        # 6b. Reject dangerous function calls (resource-exhaustion, filesystem,
        # large-object, network, session-config) anywhere in the tree. The
        # read-only role and statement timeout do not fully neutralise these
        # (e.g. pg_sleep burns a connection regardless of grants), so the guard
        # rejects them by name as defense-in-depth.
        denied_function = find_denied_function(statement)
        if denied_function is not None:
            raise SqlLabValidationError(
                f"Disallowed function: {denied_function}() is not permitted in a "
                "read-only query."
            )

        # 7. Reject any reference to a denylisted sensitive table.
        referenced = self._referenced_sensitive_tables(statement)
        if referenced:
            names = ", ".join(sorted(referenced))
            raise SqlLabValidationError(
                f"Sensitive-table reference: query references denylisted table(s): {names}."
            )

        # 8. A single read-only SELECT (including SELECT *) is allowed.
        return normalized

    @staticmethod
    def _operation_name(node: exp.Expression) -> str:
        """Return the human-readable operation name for a forbidden node."""
        for node_type, name in _FORBIDDEN_NODES:
            if isinstance(node, node_type):
                return name
        if isinstance(node, exp.Command):
            # sqlglot parses COPY/VACUUM/GRANT/etc. it does not model natively as
            # a Command whose ``this`` holds the leading keyword.
            keyword = node.this
            if isinstance(keyword, str) and keyword.strip():
                return keyword.strip().upper()
            return "administrative command"
        # Any other non-SELECT root (e.g. EXPLAIN/SHOW parsed to a distinct node).
        return type(node).__name__.upper()

    def _referenced_sensitive_tables(self, statement: exp.Expression) -> set[str]:
        """Collect denylisted table names referenced anywhere in the tree.

        Both real table references (``FROM``/``JOIN``/comma joins) and column
        qualifiers (e.g. ``users.id``) are considered, so a sensitive table can
        never be reached through any table position or qualifier.
        """
        if not self._sensitive_tables:
            return set()
        referenced: set[str] = set()
        for table in statement.find_all(exp.Table):
            name = table.name.lower()
            if name in self._sensitive_tables:
                referenced.add(name)
        for column in statement.find_all(exp.Column):
            qualifier = column.table.lower()
            if qualifier and qualifier in self._sensitive_tables:
                referenced.add(qualifier)
        return referenced
