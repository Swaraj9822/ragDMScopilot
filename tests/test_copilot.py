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
