import json
import re
import uuid
from pathlib import Path
from textwrap import dedent
from typing import Any, Iterator

import sqlglot
from pydantic import BaseModel, Field
from sqlglot import exp
from sqlglot.errors import SqlglotError

from rag_system.config import Settings
from rag_system.confidence import database_confidence_score
from rag_system.llm import build_text_llm
from rag_system.models import CopilotDataSource, CopilotQueryRequest, CopilotQueryResponse
from rag_system.observability import (
    get_logger,
    get_trace_id,
    is_unified_active,
    metrics,
    retry_on_transient,
    timed,
)
from rag_system.observability_tracing import record_query_summary

logger = get_logger(__name__)


class CopilotColumn(BaseModel):
    name: str
    type: str | None = None
    description: str | None = None
    synonyms: list[str] = Field(default_factory=list)


class CopilotTable(BaseModel):
    name: str
    description: str | None = None
    columns: list[CopilotColumn]
    joins: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)


class CopilotSchemaCatalog(BaseModel):
    tables: list[CopilotTable]
    business_rules: list[str] = Field(default_factory=list)

    @property
    def table_names(self) -> set[str]:
        return {table.name.lower() for table in self.tables}

    def column_names_for(self, table_name: str) -> set[str]:
        for table in self.tables:
            if table.name.lower() == table_name.lower():
                return {column.name.lower() for column in table.columns}
        return set()

    def describe_for_prompt(self) -> str:
        table_blocks = []
        for table in self.tables:
            columns = "\n".join(
                f"- {column.name}"
                f"{f' ({column.type})' if column.type else ''}"
                f"{f': {column.description}' if column.description else ''}"
                for column in table.columns
            )
            joins = "\n".join(f"- {join}" for join in table.joins) or "- None specified"
            examples = "\n".join(f"- {example}" for example in table.examples) or "- None specified"
            table_blocks.append(
                dedent(
                    f"""
                    Table: {table.name}
                    Purpose: {table.description or "Not specified"}
                    Columns:
                    {columns}
                    Joins:
                    {joins}
                    Example questions:
                    {examples}
                    """
                ).strip()
            )
        rules = "\n".join(f"- {rule}" for rule in self.business_rules) or "- None specified"
        return "\n\n".join(table_blocks) + f"\n\nBusiness rules:\n{rules}"


class SqlValidationError(ValueError):
    pass


def _strip_sql_comments(sql: str) -> str:
    """Remove SQL line (``--``) and block (``/* */``) comments.

    Comments are stripped so they cannot be used to smuggle statement
    terminators or write keywords past the guard, and so keywords that appear
    only inside a comment do not trigger false rejections. String literals
    (``'...'``) and quoted identifiers (``"..."``) — including their doubled-quote
    escapes — are preserved verbatim, so a value like ``'a--b'`` or ``'/*x*/'``
    is never mangled. This is a targeted scanner, not a full SQL parser.
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch in ("'", '"'):
            # Copy a quoted string/identifier verbatim, honoring '' / "" escapes.
            quote = ch
            out.append(ch)
            i += 1
            while i < n:
                out.append(sql[i])
                if sql[i] == quote:
                    if i + 1 < n and sql[i + 1] == quote:
                        out.append(sql[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            i += 2
            while i < n and sql[i] != "\n":
                i += 1
            out.append(" ")
            continue
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            # Postgres block comments nest: /* outer /* inner */ still outer */.
            # Track depth so we consume the whole nested comment instead of
            # stopping at the first */ and leaving a dangling */ remnant in the
            # SQL (which would turn a legitimate nested-comment query into a
            # confusing rejection). Note: dollar-quoted strings ($$...$$ /
            # $tag$...$tag$) are NOT handled here; any comment markers inside one
            # are treated as real comments. That is acceptable for this guard
            # because only aggregate SELECTs pass validation, but worth knowing.
            depth = 1
            i += 2
            while i < n and depth > 0:
                if sql[i] == "/" and i + 1 < n and sql[i + 1] == "*":
                    depth += 1
                    i += 2
                elif sql[i] == "*" and i + 1 < n and sql[i + 1] == "/":
                    depth -= 1
                    i += 2
                else:
                    i += 1
            out.append(" ")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def load_schema_catalog(settings: Settings) -> CopilotSchemaCatalog:
    path = Path(settings.copilot_schema_catalog_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent.parent / path
    if not path.exists():
        raise FileNotFoundError(
            f"Copilot schema catalog not found at {path}. "
            "Create it from config/copilot_schema_catalog.example.json."
        )
    return CopilotSchemaCatalog.model_validate(json.loads(path.read_text(encoding="utf-8")))


class CopilotSqlGuard:
    """AST-based allowlist for copilot-generated SQL.

    The guard parses the SQL into a syntax tree (via ``sqlglot``) and validates
    it structurally rather than with surface regexes. Regex table extraction
    only saw identifiers after ``FROM``/``JOIN`` and never looked at columns, so
    a comma join to an unlisted table plus a hand-picked column
    (``SELECT array_agg(u.password_hash) FROM sales_order s, users u``) sailed
    straight through. The AST guard instead enforces, over the *whole* tree:

    * exactly one statement, and its root is a ``SELECT``;
    * no write/DDL/administrative nodes, no CTEs, subqueries, set operations,
      or window functions (which would break the row-collapsing guarantee or
      open extra, hard-to-analyse namespaces);
    * no ``SELECT *`` / ``table.*`` projection;
    * at least one aggregate function (so raw detail rows are never returned);
    * every referenced table — including comma-joined ones — is on the catalog
      allowlist;
    * every referenced column resolves to an approved column on an approved
      table (qualified columns must match the aliased table; unqualified ones
      must exist on some referenced table or be a projection alias).

    Row limits are clamped/appended using the parsed AST (never a regex over the
    raw text), so a ``LIMIT`` inside a string literal is never mistaken for a
    real clause. The model's text is preserved verbatim except when an oversized
    ``LIMIT`` must be clamped, where the statement is re-serialised as PostgreSQL.
    """

    # Nodes that must never appear anywhere in the tree. Data-modifying and
    # administrative statements can only reach a SELECT root by hiding inside a
    # CTE/subquery, so rejecting those namespaces also blocks nested DML.
    _FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
        exp.Insert,
        exp.Update,
        exp.Delete,
        exp.Drop,
        exp.Create,
        exp.Command,  # sqlglot parses COPY/CALL/GRANT/VACUUM/etc. as Command
        exp.Merge,
        exp.Window,  # window aggregates do not collapse rows -> raw detail leak
        exp.With,  # CTEs (incl. data-modifying WITH ... AS (DELETE ...))
        exp.Subquery,  # derived tables / scalar subqueries -> extra namespaces
        exp.Union,  # covers UNION / EXCEPT / INTERSECT (subclasses of Union)
    )

    def __init__(self, catalog: CopilotSchemaCatalog, max_rows: int):
        self._catalog = catalog
        self._max_rows = max_rows

    def _parse(self, sql: str) -> exp.Select:
        try:
            statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
        except SqlglotError as exc:
            raise SqlValidationError(f"Could not parse SQL: {exc}") from exc
        if len(statements) != 1:
            raise SqlValidationError("Only one SQL statement is allowed.")
        statement = statements[0]
        if not isinstance(statement, exp.Select):
            raise SqlValidationError("Only SELECT queries are allowed.")
        return statement

    def validate(self, sql: str) -> str:
        # Strip comments first (string-literal aware) so a hidden ";" or write
        # keyword inside a comment cannot slip past the checks below, then parse.
        sql = _strip_sql_comments(sql).strip().rstrip(";").strip()
        if not sql:
            raise SqlValidationError("SQL is empty.")

        statement = self._parse(sql)

        if next(statement.find_all(*self._FORBIDDEN_NODES), None) is not None:
            raise SqlValidationError(
                "Write, administrative, subquery, CTE, set-operation, and window "
                "SQL is not allowed."
            )

        self._reject_star(statement)

        if next(statement.find_all(exp.AggFunc), None) is None:
            raise SqlValidationError("SQL must aggregate results to protect the database.")

        alias_to_table, referenced_tables = self._resolve_tables(statement)
        if not referenced_tables:
            raise SqlValidationError("Query must read from at least one approved table.")
        unknown_tables = sorted(
            name for name in referenced_tables if name not in self._catalog.table_names
        )
        if unknown_tables:
            raise SqlValidationError(f"Unapproved table(s): {', '.join(unknown_tables)}")

        self._validate_columns(statement, alias_to_table, referenced_tables)

        return self._apply_limit(sql, statement)

    @staticmethod
    def _reject_star(statement: exp.Select) -> None:
        """Reject ``SELECT *`` / ``t.*`` projections (but allow ``count(*)``)."""
        for select in statement.find_all(exp.Select):
            for projection in select.expressions:
                target = projection.this if isinstance(projection, exp.Alias) else projection
                if isinstance(target, exp.Star) or (
                    isinstance(target, exp.Column) and isinstance(target.this, exp.Star)
                ):
                    raise SqlValidationError(
                        "SELECT * is not allowed; select explicit columns."
                    )

    def _resolve_tables(self, statement: exp.Expression) -> tuple[dict[str, str], set[str]]:
        """Map every table alias/name to its real table and collect table names.

        Both are lowercased. Comma-joined tables are ordinary ``Table`` nodes,
        so they are captured here just like ``JOIN`` targets — closing the hole
        where a comma join smuggled an unlisted table past the old regex.
        """
        alias_to_table: dict[str, str] = {}
        referenced: set[str] = set()
        for table in statement.find_all(exp.Table):
            real = table.name.lower()
            referenced.add(real)
            alias_to_table[(table.alias or table.name).lower()] = real
        return alias_to_table, referenced

    def _validate_columns(
        self,
        statement: exp.Expression,
        alias_to_table: dict[str, str],
        referenced_tables: set[str],
    ) -> None:
        output_aliases = {
            alias.alias.lower() for alias in statement.find_all(exp.Alias) if alias.alias
        }
        for column in statement.find_all(exp.Column):
            name = column.name.lower()
            qualifier = column.table.lower()
            if qualifier:
                real = alias_to_table.get(qualifier)
                if real is None:
                    raise SqlValidationError(f"Unknown table qualifier: {column.table}")
                if name not in self._catalog.column_names_for(real):
                    raise SqlValidationError(
                        f"Unapproved column: {column.table}.{column.name}"
                    )
            elif name in output_aliases:
                # A reference to a projection alias (e.g. GROUP BY/ORDER BY on
                # "AS revenue"); not a base-table column.
                continue
            elif not any(
                name in self._catalog.column_names_for(table) for table in referenced_tables
            ):
                raise SqlValidationError(f"Unapproved column: {column.name}")

    def _apply_limit(self, sql: str, statement: exp.Select) -> str:
        """Clamp or append the row limit, driven by the parsed AST.

        Using the AST to *decide* (rather than a regex over the raw text) means a
        ``LIMIT`` appearing inside a string literal — e.g.
        ``WHERE label = 'limit 999999'`` — is never mistaken for a real clause.
        The model's text is returned verbatim in the common cases:

        * a query whose ``LIMIT`` is already within ``max_rows`` is untouched;
        * a grouped query with no ``LIMIT`` gets one appended at the end (an
          append can never corrupt an interior literal);
        * a single-row aggregate (no GROUP BY, no LIMIT) is left uncapped.

        Only when an existing ``LIMIT`` exceeds the cap (or is non-numeric) is the
        statement re-serialised, so the clamp is always correct regardless of any
        string literals in the query. ``LIMIT ALL`` never reaches here — sqlglot
        parses ``ALL`` as an identifier, so the column allowlist rejects it
        upstream (fail-closed).
        """
        limit_node = statement.args.get("limit")
        if limit_node is not None:
            value = self._limit_value(limit_node)
            if value is not None and value <= self._max_rows:
                return sql  # within cap; keep the model's text verbatim
            return statement.limit(self._max_rows).sql(dialect="postgres")  # clamp
        if statement.args.get("group") is not None:
            return f"{sql}\nLIMIT {self._max_rows}"  # append-only; text preserved
        return sql  # single-row aggregate; no limit needed

    @staticmethod
    def _limit_value(limit_node: exp.Expression) -> int | None:
        """Return the integer count of a ``LIMIT`` node, or ``None`` if it is not
        a plain non-negative integer literal (e.g. a parameterised limit)."""
        expression = limit_node.args.get("expression")
        if isinstance(expression, exp.Literal) and expression.is_number:
            try:
                return int(expression.name)
            except ValueError:
                return None
        return None

    def data_sources(self, sql: str) -> list[CopilotDataSource]:
        sources = []
        for table_name in sorted(self._extract_table_names(sql)):
            sources.append(
                CopilotDataSource(
                    table=table_name,
                    columns=sorted(self._catalog.column_names_for(table_name)),
                )
            )
        return sources

    def _extract_table_names(self, sql: str) -> set[str]:
        try:
            statement = self._parse(sql)
        except SqlValidationError:
            return set()
        _alias_to_table, referenced = self._resolve_tables(statement)
        return referenced


class PostgresCopilotExecutor:
    def __init__(self, settings: Settings):
        self._settings = settings

    def execute(self, sql: str) -> list[dict[str, Any]]:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("Install psycopg[binary] to use the database copilot.") from exc

        required = {
            "COPILOT_DB_HOST": self._settings.copilot_db_host,
            "COPILOT_DB_NAME": self._settings.copilot_db_name,
            "COPILOT_DB_USER": self._settings.copilot_db_user,
            "COPILOT_DB_PASSWORD": self._settings.copilot_db_password,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError(f"Missing copilot database setting(s): {', '.join(missing)}")

        with psycopg.connect(
            host=self._settings.copilot_db_host,
            port=self._settings.copilot_db_port,
            dbname=self._settings.copilot_db_name,
            user=self._settings.copilot_db_user,
            password=self._settings.copilot_db_password,
            sslmode=self._settings.copilot_db_sslmode,
            row_factory=dict_row,
        ) as conn:
            # psycopg connects with autocommit=False, so the first execute()
            # implicitly opens a transaction *before* the statement runs. A bare
            # "BEGIN READ ONLY" would therefore execute inside that already-open
            # transaction — Postgres warns "there is already a transaction in
            # progress" and the READ ONLY attribute is never applied. Use
            # "SET TRANSACTION READ ONLY", which is valid inside the open
            # transaction and applies to it, as the guard against writes.
            #
            # NOTE: we deliberately do NOT set default_transaction_read_only via
            # the connection ``options`` startup parameter — pooled providers
            # (e.g. Neon's PgBouncer pooler) reject unknown startup parameters
            # ("unsupported startup parameter in options"). SET TRANSACTION is a
            # plain SQL command and works on both pooled and direct connections.
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (str(self._settings.copilot_statement_timeout_ms),),
            )
            rows = conn.execute(sql).fetchmany(self._settings.copilot_max_rows)
            conn.rollback()
            return [dict(row) for row in rows]


class LlmDatabaseCopilot:
    """Database copilot SQL/answer generation backed by the configured LLM provider."""

    def __init__(self, settings: Settings):
        self._llm = build_text_llm(settings)
        self._model_id = self._llm.model_id

    @retry_on_transient()
    def _call_llm(self, prompt: str, max_tokens: int = 2048) -> str:
        text, _usage = self._llm.generate(prompt, temperature=0.0, max_tokens=max_tokens)
        return text

    def generate_sql(self, question: str, catalog: CopilotSchemaCatalog) -> str:
        prompt = build_sql_prompt(question, catalog.describe_for_prompt())
        return _extract_sql(self._call_llm(prompt))

    def answer(self, question: str, sql: str, rows: list[dict[str, Any]]) -> str:
        prompt = build_database_answer_prompt(question, sql, rows)
        return format_database_answer(self._call_llm(prompt), rows)

    def answer_stream(
        self, question: str, sql: str, rows: list[dict[str, Any]]
    ) -> Iterator[str]:
        """Stream a plain-prose business answer for the SQL result.

        Unlike :meth:`answer` (which assembles a structured summary/results/
        conclusion block), the streaming variant emits a concise prose answer
        token by token. The SQL and result rows travel in the final event so
        the client renders them as a table.
        """
        prompt = build_database_stream_prompt(question, sql, rows)
        yield from self._llm.generate_stream(prompt, temperature=0.0, max_tokens=2048)


class DatabaseCopilotService:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._catalog: CopilotSchemaCatalog | None = None
        self._guard: CopilotSqlGuard | None = None
        self._executor: PostgresCopilotExecutor | None = None
        self._generator: LlmDatabaseCopilot | None = None

    @property
    def catalog(self) -> CopilotSchemaCatalog:
        if self._catalog is None:
            self._catalog = load_schema_catalog(self._settings)
        return self._catalog

    @property
    def guard(self) -> CopilotSqlGuard:
        if self._guard is None:
            self._guard = CopilotSqlGuard(self.catalog, self._settings.copilot_max_rows)
        return self._guard

    @property
    def executor(self) -> PostgresCopilotExecutor:
        if self._executor is None:
            self._executor = PostgresCopilotExecutor(self._settings)
        return self._executor

    @property
    def generator(self) -> LlmDatabaseCopilot:
        if self._generator is None:
            self._generator = LlmDatabaseCopilot(self._settings)
        return self._generator

    def validate_ready(self) -> None:
        """Eagerly check boot-time prerequisites so misconfiguration degrades
        gracefully instead of failing the first query with a 500.

        Everything on this service is otherwise lazy, so a missing schema
        catalog or absent database configuration would only surface when the
        first copilot query runs. Callers that want a startup-time signal (e.g.
        the router factory, which falls back to RAG-only when the copilot is
        unavailable) call this to force those checks up front. Raises
        ``FileNotFoundError``/``ValueError`` if the catalog is missing or
        malformed, or ``RuntimeError`` if required database settings are unset.

        No network connection is attempted — only local configuration and the
        catalog file are validated, so this stays fast and does not flap on a
        transient database outage.
        """
        # Force the schema catalog to load (raises if missing/malformed).
        _ = self.catalog
        # The executor needs these to run any query, so without them the copilot
        # can never produce an answer — treat that as "unavailable" at boot.
        required = {
            "COPILOT_DB_HOST": self._settings.copilot_db_host,
            "COPILOT_DB_NAME": self._settings.copilot_db_name,
            "COPILOT_DB_USER": self._settings.copilot_db_user,
            "COPILOT_DB_PASSWORD": self._settings.copilot_db_password,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError(
                f"Missing copilot database setting(s): {', '.join(missing)}"
            )

    def query(self, request: CopilotQueryRequest) -> CopilotQueryResponse:
        trace_id = get_trace_id() or str(uuid.uuid4())
        log_extra = {"trace_id": trace_id, "query_len": len(request.question), "mode": "database"}
        logger.info("Processing copilot query", extra=log_extra)
        metrics.increment("rag_copilot_queries_total", {"mode": "database"})

        with timed(logger, "copilot SQL generation", **log_extra):
            sql = self.generator.generate_sql(request.question, self.catalog)
        logger.info("Generated SQL: %s", sql, extra=log_extra)
        with timed(logger, "copilot SQL validation", **log_extra):
            sql = self.guard.validate(sql)
        with timed(logger, "copilot SQL execution", **log_extra):
            rows = self.executor.execute(sql)
        logger.info("SQL returned %d row(s)", len(rows), extra=log_extra)
        with timed(logger, "copilot answer generation", **log_extra):
            answer = self.generator.answer(request.question, sql, rows)

        evidence_status = "grounded" if rows else "no_rows"
        # The guard already validated the SQL (it raises otherwise), so a
        # query that reaches this point passed validation.
        confidence_score = database_confidence_score(
            evidence_status=evidence_status,
            row_count=len(rows),
            sql_validated=True,
        )
        metrics.observe(
            "rag_copilot_confidence_score",
            confidence_score,
            {"evidence_status": evidence_status},
        )
        if not is_unified_active():
            record_query_summary(request.question, confidence_score)
        return CopilotQueryResponse(
            answer=answer,
            mode="database",
            evidence_status=evidence_status,
            trace_id=trace_id,
            confidence_score=confidence_score,
            sql=sql,
            rows=rows,
            data_sources=self.guard.data_sources(sql),
        )

    def query_stream(self, request: CopilotQueryRequest) -> Iterator[dict[str, Any]]:
        """Stream a database-copilot answer.

        Emits status events while SQL is generated, validated, and executed
        (which cannot be streamed), then streams the prose answer, then a final
        event with the structured payload (sql, rows, confidence).
        """
        trace_id = get_trace_id() or str(uuid.uuid4())
        log_extra = {"trace_id": trace_id, "query_len": len(request.question), "mode": "database"}
        logger.info("Processing copilot query (streaming)", extra=log_extra)
        metrics.increment("rag_copilot_queries_total", {"mode": "database"})

        yield {"type": "status", "stage": "generating_sql"}
        with timed(logger, "copilot SQL generation", **log_extra):
            sql = self.generator.generate_sql(request.question, self.catalog)
        logger.info("Generated SQL: %s", sql, extra=log_extra)
        with timed(logger, "copilot SQL validation", **log_extra):
            sql = self.guard.validate(sql)
        yield {"type": "status", "stage": "running_sql"}
        with timed(logger, "copilot SQL execution", **log_extra):
            rows = self.executor.execute(sql)
        logger.info("SQL returned %d row(s)", len(rows), extra=log_extra)

        yield {"type": "status", "stage": "generating"}
        answer_parts: list[str] = []
        with timed(logger, "copilot answer generation (streaming)", **log_extra):
            for piece in self.generator.answer_stream(request.question, sql, rows):
                answer_parts.append(piece)
                yield {"type": "delta", "text": piece}
        answer = "".join(answer_parts).strip()

        evidence_status = "grounded" if rows else "no_rows"
        confidence_score = database_confidence_score(
            evidence_status=evidence_status,
            row_count=len(rows),
            sql_validated=True,
        )
        metrics.observe(
            "rag_copilot_confidence_score",
            confidence_score,
            {"evidence_status": evidence_status},
        )
        if not is_unified_active():
            record_query_summary(request.question, confidence_score)
        yield {
            "type": "final",
            "response": CopilotQueryResponse(
                answer=answer,
                mode="database",
                evidence_status=evidence_status,
                trace_id=trace_id,
                confidence_score=confidence_score,
                sql=sql,
                rows=rows,
                data_sources=self.guard.data_sources(sql),
            ),
        }


def build_sql_prompt(question: str, catalog_description: str) -> str:
    return dedent(
        f"""
        You generate PostgreSQL SELECT queries for an enterprise data copilot.
        Use only the approved schema below. Generate exactly one read-only SELECT query.
        Do not use SELECT *. Select explicit columns and use clear aliases.
        Always aggregate in SQL using COUNT, SUM, AVG, MIN, MAX, or another approved aggregate.
        Do not return transaction-level, customer-level, invoice-level, or other raw detail rows.
        If the user asks for a large/detail report, summarize it with grouped aggregate metrics.
        When filtering by a product, customer, party, or other name, match case-insensitively
        with partial matching using ILIKE '%term%' (e.g. product_name ILIKE '%life cheese%')
        rather than exact equality (=) or LOWER(col) = '...'. Stored names include size or
        variant suffixes and specific capitalization (e.g. 'Life Cheese 1L'), so exact matches
        miss real rows. Use only the meaningful keywords from the user's phrasing in the pattern.
        Prefer business date columns from the schema for words like today, yesterday, month, or year.
        Include a LIMIT on grouped aggregate queries. Single-row aggregate queries do not need a LIMIT.
        Return only SQL, with no markdown.

        Approved schema:
        {catalog_description}

        User question:
        {question}
        """
    ).strip()


def build_database_stream_prompt(question: str, sql: str, rows: list[dict[str, Any]]) -> str:
    """Prompt for a streamed plain-prose answer over the SQL result."""
    result_json = json.dumps(rows, default=str, ensure_ascii=False)
    return dedent(
        f"""
        You answer stakeholder questions using only the SQL result provided.
        Write a concise, business-friendly answer in plain prose (2-4 sentences).
        State the key figures from the result. If the result is empty, say no
        matching rows were found. Do not invent values not supported by the
        result, and do not output JSON or markdown tables.

        Question:
        {question}

        SQL:
        {sql}

        SQL result JSON:
        {result_json}
        """
    ).strip()


def build_database_answer_prompt(question: str, sql: str, rows: list[dict[str, Any]]) -> str:
    result_json = json.dumps(rows, default=str, ensure_ascii=False)
    return dedent(
        f"""
        You answer stakeholder questions using only the SQL result provided.
        Return only JSON with this exact shape:
        {{"summary": ["line 1", "line 2"], "conclusion": "one short conclusion"}}
        The summary must be 2-3 short business-friendly lines.
        If the result is empty, say no matching rows were found.
        Do not invent values or explanations not supported by the result.

        Question:
        {question}

        SQL:
        {sql}

        SQL result JSON:
        {result_json}
        """
    ).strip()


def format_database_answer(llm_text: str, rows: list[dict[str, Any]]) -> str:
    payload = _parse_answer_payload(llm_text)
    summary_lines = payload.get("summary") or ["No matching rows were found."]
    conclusion = payload.get("conclusion") or "Conclusion is limited to the SQL result shown above."
    result_json = json.dumps(rows, default=str, ensure_ascii=False, indent=2)
    if not rows:
        result_json = "No matching rows found."

    summary = "\n".join(str(line).strip() for line in summary_lines[:3] if str(line).strip())
    return "\n\n".join(
        [
            f"Summary:\n{summary}",
            f"Results:\n{result_json}",
            f"Conclusion:\n{str(conclusion).strip()}",
        ]
    ).strip()


def _parse_answer_payload(text: str) -> dict[str, Any]:
    stripped = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.IGNORECASE | re.DOTALL)
    if fenced:
        stripped = fenced.group(1).strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return {"summary": lines[:3], "conclusion": lines[-1] if lines else ""}
    return payload if isinstance(payload, dict) else {}


def _extract_sql(text: str) -> str:
    stripped = text.strip()
    fenced = re.search(r"```(?:sql)?\s*(.*?)```", stripped, re.IGNORECASE | re.DOTALL)
    if fenced:
        return fenced.group(1).strip()
    return stripped
