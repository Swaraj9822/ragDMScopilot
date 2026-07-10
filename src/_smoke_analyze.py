from datetime import datetime, timezone
from fastapi.testclient import TestClient
from rag_system import api as api_module
from rag_system.auth import require_operator
from rag_system.auth.models import UserPublic
from rag_system.sql_lab.router import get_chart_spec_analyzer
from rag_system.sql_lab.chart_spec import ChartSpec, ChartSpecValidationError
from rag_system.sql_lab.errors import SqlLabAnalysisError

OP = UserPublic(id="op", email="op@x.com", is_active=True,
                created_at=datetime(2024, 1, 1, tzinfo=timezone.utc), is_operator=True)


class Stub:
    def __init__(self, spec=None, exc=None):
        self.spec = spec
        self.exc = exc
        self.mode = None

    def analyze(self, result, mode="default"):
        self.mode = mode
        if self.exc:
            raise self.exc
        return self.spec


spec = ChartSpec(kpis=[], charts=[{"type": "bar", "title": "t", "xColumn": "a",
                 "series": [{"column": "b", "op": "sum"}]}], insight="hi")

api_module.app.dependency_overrides[require_operator] = lambda: OP
client = TestClient(api_module.app)

body = {"columns": ["a", "b"], "rows": [{"a": 1, "b": 2}], "rowCount": 1,
        "durationMs": 5, "sql": "SELECT a,b", "truncated": False, "mode": "deep"}

s = Stub(spec=spec)
api_module.app.dependency_overrides[get_chart_spec_analyzer] = lambda: s
r = client.post("/sql/analyze", json=body)
print("success", r.status_code, r.json(), "mode=", s.mode)

s2 = Stub(exc=ChartSpecValidationError("bad spec"))
api_module.app.dependency_overrides[get_chart_spec_analyzer] = lambda: s2
r = client.post("/sql/analyze", json=body)
print("invalid", r.status_code, r.json())

s3 = Stub(exc=SqlLabAnalysisError("model down"))
api_module.app.dependency_overrides[get_chart_spec_analyzer] = lambda: s3
r = client.post("/sql/analyze", json=body)
print("unavailable", r.status_code, r.json())

s4 = Stub(spec=spec)
api_module.app.dependency_overrides[get_chart_spec_analyzer] = lambda: s4
client.post("/sql/analyze", json={"columns": ["a"], "rows": [], "rowCount": 0})
print("default mode=", s4.mode)
