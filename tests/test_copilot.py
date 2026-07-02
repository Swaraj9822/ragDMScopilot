import sys
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from rag_system import api as api_module
from rag_system.copilot import (
    CopilotColumn,
    CopilotSchemaCatalog,
    CopilotSqlGuard,
    CopilotTable,
    DatabaseCopilotService,
    PostgresCopilotExecutor,
    SqlValidationError,
    _strip_sql_comments,
    build_database_answer_prompt,
    build_sql_prompt,
    format_database_answer,
)


def _catalog() -> CopilotSchemaCatalog:
    return CopilotSchemaCatalog(
        tables=[
            CopilotTable(
                name="sales_invoice",
                description="Sales invoices.",
                columns=[
                    CopilotColumn(name="invoice_date", type="date"),
                    CopilotColumn(name="net_amount", type="numeric"),
                    CopilotColumn(name="party_id", type="uuid"),
                ],
            ),
            CopilotTable(
                name="party",
                description="Party master.",
                columns=[CopilotColumn(name="id", type="uuid"), CopilotColumn(name="name")],
            ),
        ],
        business_rules=["Revenue means SUM(net_amount)."],
    )


def test_sql_guard_allows_approved_select_and_adds_limit() -> None:
    guard = CopilotSqlGuard(_catalog(), max_rows=50)

    sql = guard.validate(
        "select invoice_date, sum(net_amount) as revenue from sales_invoice group by invoice_date"
    )

    assert "LIMIT 50" in sql
    assert guard.data_sources(sql)[0].table == "sales_invoice"


@pytest.mark.parametrize(
    "sql",
    [
        "update sales_invoice set net_amount = 0",
        "select * from sales_invoice",
        "select invoice_date, net_amount from sales_invoice",
        "select id from users",
        "select id from sales_invoice; select id from party",
    ],
)
def test_sql_guard_rejects_unsafe_sql(sql: str) -> None:
    guard = CopilotSqlGuard(_catalog(), max_rows=50)

    with pytest.raises(SqlValidationError):
        guard.validate(sql)


def test_sql_guard_rejects_comma_joined_unapproved_table() -> None:
    # The P0 bypass: the old regex only validated identifiers after FROM/JOIN,
    # so a comma-joined unapproved table (here "users") slipped through and its
    # columns could be exfiltrated via an aggregate.
    guard = CopilotSqlGuard(_catalog(), max_rows=50)

    with pytest.raises(SqlValidationError):
        guard.validate(
            "select array_agg(u.secret) from sales_invoice s, users u"
        )


def test_sql_guard_rejects_unapproved_column_on_approved_table() -> None:
    # Columns were never validated before; an approved table must not expose
    # columns absent from the catalog.
    guard = CopilotSqlGuard(_catalog(), max_rows=50)

    with pytest.raises(SqlValidationError):
        guard.validate("select max(ssn) as x from sales_invoice")


def test_sql_guard_rejects_unknown_qualifier() -> None:
    guard = CopilotSqlGuard(_catalog(), max_rows=50)

    with pytest.raises(SqlValidationError):
        guard.validate("select sum(bogus.net_amount) as revenue from sales_invoice")


@pytest.mark.parametrize(
    "sql",
    [
        # Window functions do not collapse rows -> raw-detail leak.
        "select sum(net_amount) over (partition by party_id) as r from sales_invoice",
        # CTEs open an extra namespace the guard refuses to reason about.
        "with t as (select net_amount from sales_invoice) select sum(net_amount) as r from t",
        # Derived tables / subqueries likewise.
        "select sum(net_amount) as r from sales_invoice where party_id in (select id from party)",
        # Set operations.
        "select sum(net_amount) as r from sales_invoice union select sum(net_amount) as r from sales_invoice",
    ],
)
def test_sql_guard_rejects_unanalyzable_constructs(sql: str) -> None:
    guard = CopilotSqlGuard(_catalog(), max_rows=50)

    with pytest.raises(SqlValidationError):
        guard.validate(sql)


def test_sql_guard_allows_qualified_columns_across_join() -> None:
    guard = CopilotSqlGuard(_catalog(), max_rows=50)

    sql = guard.validate(
        "select p.name, sum(s.net_amount) as revenue "
        "from sales_invoice s join party p on s.party_id = p.id "
        "group by p.name"
    )

    assert "LIMIT 50" in sql


def test_data_sources_lists_comma_joined_tables() -> None:
    # The old regex missed comma-joined tables in data_sources too; the AST
    # extractor reports every approved table the query touches.
    guard = CopilotSqlGuard(_catalog(), max_rows=50)

    sql = guard.validate(
        "select sum(s.net_amount) as revenue "
        "from sales_invoice s, party p where s.party_id = p.id"
    )
    tables = {source.table for source in guard.data_sources(sql)}

    assert tables == {"sales_invoice", "party"}


def test_strip_sql_comments_handles_nested_block_comments() -> None:
    # Postgres block comments nest. The stripper must consume the whole nested
    # comment, leaving no dangling */ remnant.
    stripped = _strip_sql_comments("select /* a /* b */ c */ 1")
    assert "*/" not in stripped
    assert "select" in stripped
    assert "1" in stripped


def test_strip_sql_comments_preserves_markers_inside_string_literal() -> None:
    # Comment markers inside a string literal must be preserved verbatim.
    stripped = _strip_sql_comments("select '/* not a comment */' as x")
    assert "/* not a comment */" in stripped


def test_sql_guard_accepts_query_with_nested_comment() -> None:
    guard = CopilotSqlGuard(_catalog(), max_rows=50)

    sql = guard.validate(
        "select invoice_date, sum(net_amount) as revenue /* outer /* inner */ back */ "
        "from sales_invoice group by invoice_date"
    )

    assert "*/" not in sql
    assert "LIMIT 50" in sql


def test_sql_guard_rejects_write_hidden_in_nested_comment_via_failclose() -> None:
    # A write keyword smuggled after a nested comment must still be rejected
    # (the remnant is fully stripped, so the keyword is evaluated normally).
    guard = CopilotSqlGuard(_catalog(), max_rows=50)

    with pytest.raises(SqlValidationError):
        guard.validate("select /* /* */ */ 1; drop table sales_invoice")


def test_prompts_include_business_context() -> None:
    sql_prompt = build_sql_prompt("What was total sales today?", _catalog().describe_for_prompt())
    answer_prompt = build_database_answer_prompt(
        "What was total sales today?",
        "select sum(net_amount) as revenue from sales_invoice",
        [{"revenue": 100}],
    )

    assert "PostgreSQL SELECT" in sql_prompt
    assert "sales_invoice" in sql_prompt
    assert "Always aggregate in SQL" in sql_prompt
    assert "What was total sales today?" in answer_prompt
    assert '"revenue": 100' in answer_prompt
    assert '"summary"' in answer_prompt


def test_sql_guard_clamps_large_limits() -> None:
    guard = CopilotSqlGuard(_catalog(), max_rows=25)

    sql = guard.validate(
        "select party_id, sum(net_amount) as revenue from sales_invoice group by party_id limit 10000"
    )

    assert "LIMIT 25" in sql


def test_format_database_answer_uses_fixed_sections() -> None:
    answer = format_database_answer(
        '{"summary": ["Revenue was 100.", "One aggregate row was returned."], '
        '"conclusion": "Revenue is 100 for the requested period."}',
        [{"revenue": 100}],
    )

    assert answer.startswith("Summary:\nRevenue was 100.")
    assert "Results:\n[\n  {\n    \"revenue\": 100\n  }\n]" in answer
    assert "Conclusion:\nRevenue is 100 for the requested period." in answer


def test_postgres_executor_sets_timeout_with_set_config(monkeypatch) -> None:
    calls = []

    class FakeResult:
        def fetchmany(self, limit):
            calls.append(("fetchmany", limit))
            return [{"revenue": 1250}]

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql, params=None):
            calls.append(("execute", sql, params))
            if sql.startswith("select sum"):
                return FakeResult()
            return None

        def rollback(self):
            calls.append(("rollback",))

    def fake_connect(**kwargs):
        calls.append(("connect", kwargs))
        return FakeConnection()

    fake_psycopg = SimpleNamespace(connect=fake_connect)
    fake_rows = SimpleNamespace(dict_row=object())
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.rows", fake_rows)

    settings = SimpleNamespace(
        copilot_db_host="localhost",
        copilot_db_port=5432,
        copilot_db_name="app",
        copilot_db_user="app",
        copilot_db_password="secret",
        copilot_db_sslmode="require",
        copilot_statement_timeout_ms=10_000,
        copilot_max_rows=25,
    )

    rows = PostgresCopilotExecutor(settings).execute(
        "select sum(net_amount) as revenue from sales_invoice"
    )

    assert rows == [{"revenue": 1250}]
    assert calls[1] == ("execute", "BEGIN READ ONLY", None)
    assert calls[2] == (
        "execute",
        "SELECT set_config('statement_timeout', %s, true)",
        ("10000",),
    )
    assert calls[3] == (
        "execute",
        "select sum(net_amount) as revenue from sales_invoice",
        None,
    )
    assert calls[4] == ("fetchmany", 25)
    assert calls[5] == ("rollback",)


class FakeGenerator:
    def generate_sql(self, question, catalog):
        assert "sales" in question.lower()
        return "select sum(net_amount) as revenue from sales_invoice"

    def answer(self, question, sql, rows):
        return f"Total sales were {rows[0]['revenue']}."


class FakeExecutor:
    def execute(self, sql):
        assert "sum(net_amount)" in sql
        return [{"revenue": 1250}]


def test_copilot_api_query_with_mocked_dependencies(monkeypatch) -> None:
    service = DatabaseCopilotService(SimpleNamespace(copilot_max_rows=25))
    service._catalog = _catalog()
    service._guard = CopilotSqlGuard(service._catalog, max_rows=25)
    service._generator = FakeGenerator()
    service._executor = FakeExecutor()

    monkeypatch.setattr(api_module, "get_copilot_service", lambda: service)
    client = TestClient(api_module.app)

    response = client.post(
        "/copilot/query",
        json={"question": "What was total sales today?", "include_sql": True},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "Total sales were 1250."
    assert body["evidence_status"] == "grounded"
    assert body["sql"].startswith("select sum")
    assert body["rows"] == [{"revenue": 1250}]
