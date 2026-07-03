"""Service-level tests for evaluation-run orchestration (R7.1, R7.4-R7.7).

Exercises ``RagService.run_evaluation`` end to end against a fake store and a
canned query runner: it loads the Evaluation_Set's Benchmark_Cases, scores the
deterministic checks, decides CI pass/fail, persists the run, and makes it
listable/retrievable.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from rag_system.evaluation import EvaluationSetValidationError
from rag_system.models import BenchmarkCase, Citation, QueryResponse
from rag_system.service import RagService
from rag_system.storage import PreconditionFailed, evaluation_set_case_key


class _Store:
    """Minimal JSON store with create-only writes and evaluation listers."""

    def __init__(self) -> None:
        self.objects: dict[str, object] = {}

    def get_json(self, key: str) -> object | None:
        return self.objects.get(key)

    def create_json(self, key: str, payload: object) -> str:
        if key in self.objects:
            raise PreconditionFailed(key)
        self.objects[key] = payload
        return f"s3://bucket/{key}"

    def list_evaluation_set_case_keys(self, set_id: str) -> list[str]:
        prefix = f"evaluation/sets/{set_id}/cases/"
        return [k for k in self.objects if k.startswith(prefix) and k.endswith(".json")]

    def list_evaluation_run_keys(self) -> list[str]:
        return [
            k
            for k in self.objects
            if k.startswith("evaluation/runs/") and k.endswith("/results.json")
        ]


_SET_ID = "default-set"


def _service(store: _Store, runner) -> RagService:
    service = object.__new__(RagService)
    service._settings = SimpleNamespace(
        default_evaluation_set_id=_SET_ID,
        retrieval_metric_depth_k=10,
    )
    service._store = store
    # Canned query + no trace lookup (cases carry no relevance labels).
    service.query = runner  # type: ignore[method-assign]
    service.get_query_trace = lambda _tid: None  # type: ignore[method-assign]
    return service


def _seed_case(store: _Store, case: BenchmarkCase) -> None:
    store.objects[evaluation_set_case_key(_SET_ID, case.id)] = case.model_dump()


def test_run_evaluation_persists_and_is_retrievable():
    store = _Store()
    _seed_case(
        store,
        BenchmarkCase(
            id="c1",
            question="What was revenue?",
            required_answer_terms=["10"],
            min_citations=1,
            human_reviewed=True,
        ),
    )

    def runner(_request):
        return QueryResponse(
            answer="Revenue was 10.",
            citations=[Citation(document_id="d", chunk_id="ch", page_start=1, page_end=1, title=None)],
            evidence_status="grounded",
            trace_id="t-1",
        )

    service = _service(store, runner)
    detail = service.run_evaluation()

    assert detail.ci_passed is True
    assert len(detail.results) == 1

    # Listable and retrievable.
    summaries = service.list_evaluation_runs()
    assert len(summaries) == 1
    assert summaries[0].run_id == detail.run_id
    assert summaries[0].result_count == 1

    fetched = service.get_evaluation_run(detail.run_id)
    assert fetched is not None
    assert fetched.run_id == detail.run_id


def test_run_evaluation_ci_fails_on_deterministic_failure():
    store = _Store()
    _seed_case(
        store,
        BenchmarkCase(
            id="c1",
            question="What was revenue?",
            required_answer_terms=["missing-term"],  # not in the answer -> fail
            human_reviewed=True,
        ),
    )

    def runner(_request):
        return QueryResponse(
            answer="Revenue was 10.",
            citations=[],
            evidence_status="grounded",
            trace_id="t-1",
        )

    detail = _service(store, runner).run_evaluation()
    assert detail.ci_passed is False


def test_run_evaluation_requires_human_reviewed_case():
    store = _Store()
    _seed_case(
        store,
        BenchmarkCase(id="c1", question="Q", human_reviewed=False),
    )
    service = _service(store, lambda _r: QueryResponse(
        answer="a", citations=[], evidence_status="grounded", trace_id="t"
    ))
    with pytest.raises(EvaluationSetValidationError):
        service.run_evaluation()
