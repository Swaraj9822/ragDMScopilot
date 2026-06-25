import sys
import contextlib
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
    ExampleQuery,
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
        "select max(secret_salary) from party",
    ],
)
def test_sql_guard_rejects_unsafe_sql(sql: str) -> None:
    guard = CopilotSqlGuard(_catalog(), max_rows=50)

    with pytest.raises(SqlValidationError):
        guard.validate(sql)


def test_prompts_include_business_context() -> None:
    sql_prompt = build_sql_prompt("What was total sales today?", _catalog().describe_for_prompt())
    system_prompt, user_prompt = build_database_answer_prompt(
        "What was total sales today?",
        "select sum(net_amount) as revenue from sales_invoice",
        [{"revenue": 100}],
    )
    # Combine for backward-compatible assertions
    answer_prompt = f"{system_prompt}\n{user_prompt}"

    assert "PostgreSQL SELECT" in sql_prompt
    assert "sales_invoice" in sql_prompt
    assert "Always aggregate in SQL" in sql_prompt
    assert "What was total sales today?" in answer_prompt
    assert '"revenue": 100' in answer_prompt
    assert '"summary"' in answer_prompt


def test_sql_prompt_includes_temporal_context() -> None:
    """The enhanced temporal context should appear in the SQL prompt."""
    from datetime import date

    sql_prompt = build_sql_prompt("Sales last year?", _catalog().describe_for_prompt())

    today = date.today()
    assert str(today.year) in sql_prompt
    assert "Temporal Reference" in sql_prompt
    assert "Last year" in sql_prompt
    assert "Financial year" in sql_prompt


def test_few_shot_examples_injected_when_present() -> None:
    """When example_queries exist on tables, they appear in the SQL prompt."""
    catalog = CopilotSchemaCatalog(
        tables=[
            CopilotTable(
                name="orders",
                description="Order data.",
                columns=[CopilotColumn(name="total", type="numeric")],
                example_queries=[
                    ExampleQuery(
                        question="Total order value?",
                        sql="SELECT SUM(total) FROM orders",
                    ),
                ],
            ),
        ],
    )
    sql_prompt = build_sql_prompt("Revenue?", catalog.describe_for_prompt(), tables=catalog.tables)
    assert "Few-shot examples" in sql_prompt
    assert "Total order value?" in sql_prompt
    assert "SELECT SUM(total) FROM orders" in sql_prompt


def test_business_glossary_in_schema_description() -> None:
    """Business glossary terms should appear in the schema description."""
    catalog = CopilotSchemaCatalog(
        tables=[
            CopilotTable(
                name="invoices",
                description="Invoice data.",
                columns=[CopilotColumn(name="grand_total", type="numeric")],
            ),
        ],
        business_glossary={"revenue": "Use grand_total for revenue (includes taxes)"},
    )
    desc = catalog.describe_for_prompt()
    assert "Business glossary" in desc
    assert "revenue" in desc
    assert "grand_total" in desc


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
    assert 'Results:\n[\n  {\n    "revenue": 100\n  }\n]' in answer
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

    class FakePool:
        def __init__(self, *args, **kwargs):
            pass

        @contextlib.contextmanager
        def connection(self):
            yield fake_connect()

    fake_pool_module = SimpleNamespace(ConnectionPool=FakePool)

    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.rows", fake_rows)
    monkeypatch.setitem(
        sys.modules,
        "psycopg.conninfo",
        SimpleNamespace(
            make_conninfo=lambda **kw: (
                "host='localhost' port=5432 dbname='app' user='app' password='secret' sslmode='require'"
            )
        ),
    )
    monkeypatch.setitem(sys.modules, "psycopg_pool", fake_pool_module)

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
    assert calls[5] == ("execute", "ROLLBACK", None)


class FakeGenerator:
    def check_intent(self, question):
        pass

    def select_tables(self, question, catalog):
        return catalog.tables

    def generate_sql(self, question, catalog, selected_tables=None, error_msg=None):
        assert "sales" in question.lower()
        return "select sum(net_amount) as revenue from sales_invoice"

    def answer(self, question, sql, rows):
        return f"Total sales were {rows[0]['revenue']}."


class FakeExecutor:
    def execute(self, sql, user_id=None):
        assert "sum(net_amount)" in sql
        return [{"revenue": 1250}]


def test_copilot_api_query_with_mocked_dependencies(monkeypatch) -> None:
    service = DatabaseCopilotService(
        SimpleNamespace(
            copilot_max_rows=25,
            copilot_schema_catalog_path="dummy.json",
            copilot_sql_max_attempts=3,
            copilot_schema_catalog_s3_uri=None,
        )
    )
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
