import json
import re
import uuid
from pathlib import Path
from textwrap import dedent
from typing import Any, Iterator

from pydantic import BaseModel, Field

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
    _write_keywords = re.compile(
        r"\b(insert|update|delete|drop|alter|create|truncate|merge|grant|revoke|copy|call)\b",
        re.IGNORECASE,
    )
    _table_pattern = re.compile(r"\b(?:from|join)\s+([a-zA-Z_][\w]*(?:\.[a-zA-Z_][\w]*)?)", re.IGNORECASE)
    _aggregate_pattern = re.compile(
        r"\b(count|sum|avg|min|max|string_agg|array_agg|json_agg|bool_and|bool_or)\s*\(",
        re.IGNORECASE,
    )
    _limit_pattern = re.compile(r"\blimit\s+(\d+)\b", re.IGNORECASE)

    def __init__(self, catalog: CopilotSchemaCatalog, max_rows: int):
        self._catalog = catalog
        self._max_rows = max_rows

    def validate(self, sql: str) -> str:
        sql = sql.strip().rstrip(";")
        if not sql:
            raise SqlValidationError("SQL is empty.")
        if ";" in sql:
            raise SqlValidationError("Only one SQL statement is allowed.")
        if not re.match(r"^\s*select\b", sql, re.IGNORECASE):
            raise SqlValidationError("Only SELECT queries are allowed.")
        if self._write_keywords.search(sql):
            raise SqlValidationError("Write or administrative SQL is not allowed.")
        if re.search(r"\bselect\s+\*", sql, re.IGNORECASE):
            raise SqlValidationError("SELECT * is not allowed; select explicit columns.")
        if not self._aggregate_pattern.search(sql):
            raise SqlValidationError("SQL must aggregate results to protect the database.")

        table_names = self._extract_table_names(sql)
        if not table_names:
            raise SqlValidationError("Query must read from at least one approved table.")
        unknown_tables = sorted(name for name in table_names if name.lower() not in self._catalog.table_names)
        if unknown_tables:
            raise SqlValidationError(f"Unapproved table(s): {', '.join(unknown_tables)}")

        limit = self._limit_pattern.search(sql)
        if limit and int(limit.group(1)) > self._max_rows:
            sql = self._limit_pattern.sub(f"LIMIT {self._max_rows}", sql, count=1)
        elif not limit and re.search(r"\bgroup\s+by\b", sql, re.IGNORECASE):
            sql = f"{sql}\nLIMIT {self._max_rows}"
        return sql

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
        names = set()
        for match in self._table_pattern.finditer(sql):
            raw_name = match.group(1)
            names.add(raw_name.split(".")[-1])
        return names


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
            conn.execute("BEGIN READ ONLY")
            conn.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (str(self._settings.copilot_statement_timeout_ms),),
            )
            rows = conn.execute(sql).fetchmany(self._settings.copilot_max_rows)
            conn.rollback()
            return [dict(row) for row in rows]


class BedrockDatabaseCopilot:
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
        self._generator: BedrockDatabaseCopilot | None = None

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
    def generator(self) -> BedrockDatabaseCopilot:
        if self._generator is None:
            self._generator = BedrockDatabaseCopilot(self._settings)
        return self._generator

    def query(self, request: CopilotQueryRequest) -> CopilotQueryResponse:
        trace_id = get_trace_id() or str(uuid.uuid4())
        log_extra = {"trace_id": trace_id, "query_len": len(request.question), "mode": "database"}
        logger.info("Processing copilot query", extra=log_extra)
        metrics.increment("rag_copilot_queries_total", {"mode": "database"})

        with timed(logger, "copilot SQL generation", **log_extra):
            sql = self.generator.generate_sql(request.question, self.catalog)
        with timed(logger, "copilot SQL validation", **log_extra):
            sql = self.guard.validate(sql)
        with timed(logger, "copilot SQL execution", **log_extra):
            rows = self.executor.execute(sql)
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
            sql=sql if request.include_sql else None,
            rows=rows if request.include_sql else [],
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
        with timed(logger, "copilot SQL validation", **log_extra):
            sql = self.guard.validate(sql)
        yield {"type": "status", "stage": "running_sql"}
        with timed(logger, "copilot SQL execution", **log_extra):
            rows = self.executor.execute(sql)

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
                sql=sql if request.include_sql else None,
                rows=rows if request.include_sql else [],
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
