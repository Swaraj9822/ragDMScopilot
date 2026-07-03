"""Unit tests for feedback review inbox action endpoints (R6.5–R6.11, task 11.4).

Covers:
- POST /feedback/{id}/classify — category validation, persistence, reviewed state
- POST /feedback/{id}/promote — BenchmarkCase creation, guards
- POST /feedback/{id}/resolve — resolved state, kept in inbox
- All three endpoints are operator-only (403 for non-operators)
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from rag_system import api as api_module
from rag_system.auth import require_operator
from rag_system.auth.models import UserPublic
from rag_system.models import (
    BenchmarkCase,
    FeedbackReviewRecord,
    QueryTraceRecord,
    ReviewStatus,
)

_OPERATOR = UserPublic(
    id="op-1",
    email="operator@example.com",
    is_active=True,
    created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    is_operator=True,
)

_NON_OPERATOR = UserPublic(
    id="user-1",
    email="user@example.com",
    is_active=True,
    created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    is_operator=False,
)


def _feedback_record(
    feedback_id: str = "fb-1",
    *,
    rating: int = 1,
    review_status: ReviewStatus = ReviewStatus.unreviewed,
    expected_answer: str | None = "the expected answer",
    promoted_case_id: str | None = None,
) -> FeedbackReviewRecord:
    return FeedbackReviewRecord(
        rating=rating,
        comment="bad answer",
        expected_answer=expected_answer,
        trace_id="trace-1",
        feedback_id=feedback_id,
        created_at="2024-06-15T10:00:00Z",
        review_status=review_status,
        promoted_case_id=promoted_case_id,
    )


# ---------------------------------------------------------------------------
# Fake service for testing
# ---------------------------------------------------------------------------


class _FakeService:
    """Minimal fake RagService exposing only the feedback action methods."""

    def __init__(self) -> None:
        self.records: dict[str, FeedbackReviewRecord] = {}
        self.traces: dict[str, QueryTraceRecord] = {}
        self.benchmark_cases: dict[str, BenchmarkCase] = {}

    def classify_feedback(
        self, feedback_id: str, category: str, reviewer: str
    ) -> FeedbackReviewRecord | None:
        record = self.records.get(feedback_id)
        if record is None:
            return None
        from rag_system.feedback import classify_feedback_record

        updated = classify_feedback_record(
            record,
            category=category,
            reviewer=reviewer,
            reviewed_at=datetime.now(timezone.utc).isoformat(),
        )
        self.records[feedback_id] = updated
        return updated

    def resolve_feedback(self, feedback_id: str) -> FeedbackReviewRecord | None:
        record = self.records.get(feedback_id)
        if record is None:
            return None
        from rag_system.feedback import resolve_feedback_record

        updated = resolve_feedback_record(record)
        self.records[feedback_id] = updated
        return updated

    def promote_feedback(self, feedback_id: str) -> BenchmarkCase | None:
        record = self.records.get(feedback_id)
        if record is None:
            return None
        from rag_system.feedback import promote_feedback_record

        trace = self.traces.get(record.trace_id)
        question = trace.question if trace else ""
        _, case = promote_feedback_record(record, question=question)
        # Simulate persisting the case and updating the record
        self.benchmark_cases[case.id] = case
        self.records[feedback_id] = record.model_copy(
            update={"promoted_case_id": case.id}
        )
        return case

    def list_feedback_reviews(self) -> list[FeedbackReviewRecord]:
        return list(self.records.values())

    def get_query_trace(self, trace_id: str) -> QueryTraceRecord | None:
        return self.traces.get(trace_id)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def operator_client(monkeypatch):
    """TestClient with operator gating satisfied and a controllable fake service."""
    api_module.app.dependency_overrides[require_operator] = lambda: _OPERATOR
    service = _FakeService()
    monkeypatch.setattr(api_module, "get_service", lambda: service)
    try:
        yield TestClient(api_module.app), service
    finally:
        api_module.app.dependency_overrides.pop(require_operator, None)


# ---------------------------------------------------------------------------
# POST /feedback/{id}/classify (R6.5, R6.10)
# ---------------------------------------------------------------------------


class TestClassifyEndpoint:
    def test_classifies_valid_category(self, operator_client):
        client, service = operator_client
        service.records["fb-1"] = _feedback_record("fb-1")

        resp = client.post(
            "/feedback/fb-1/classify",
            json={"category": "Missing knowledge"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["failure_category"] == "Missing knowledge"
        assert body["review_status"] == "reviewed"
        assert body["reviewed_by"] == "operator@example.com"
        assert body["reviewed_at"] is not None

    def test_all_six_categories_accepted(self, operator_client):
        client, service = operator_client
        categories = [
            "Missing knowledge",
            "Retrieval failure",
            "Wrong route",
            "Unsupported answer",
            "SQL problem",
            "Ambiguous question",
        ]
        for i, cat in enumerate(categories):
            fid = f"fb-{i}"
            service.records[fid] = _feedback_record(fid)
            resp = client.post(f"/feedback/{fid}/classify", json={"category": cat})
            assert resp.status_code == 200, f"Failed for category: {cat}"
            assert resp.json()["failure_category"] == cat

    def test_invalid_category_returns_400(self, operator_client):
        client, service = operator_client
        service.records["fb-1"] = _feedback_record("fb-1")

        resp = client.post(
            "/feedback/fb-1/classify",
            json={"category": "Not a valid category"},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "invalid_failure_category"

    def test_replaces_prior_classification(self, operator_client):
        client, service = operator_client
        service.records["fb-1"] = _feedback_record("fb-1")

        # First classification
        resp1 = client.post(
            "/feedback/fb-1/classify",
            json={"category": "Wrong route"},
        )
        assert resp1.status_code == 200
        assert resp1.json()["failure_category"] == "Wrong route"

        # Second classification replaces the first
        resp2 = client.post(
            "/feedback/fb-1/classify",
            json={"category": "SQL problem"},
        )
        assert resp2.status_code == 200
        assert resp2.json()["failure_category"] == "SQL problem"

    def test_nonexistent_feedback_returns_404(self, operator_client):
        client, _service = operator_client

        resp = client.post(
            "/feedback/no-such-id/classify",
            json={"category": "Wrong route"},
        )
        assert resp.status_code == 404

    def test_sets_review_status_to_reviewed(self, operator_client):
        client, service = operator_client
        service.records["fb-1"] = _feedback_record(
            "fb-1", review_status=ReviewStatus.unreviewed
        )

        resp = client.post(
            "/feedback/fb-1/classify",
            json={"category": "Retrieval failure"},
        )
        assert resp.status_code == 200
        assert resp.json()["review_status"] == "reviewed"


# ---------------------------------------------------------------------------
# POST /feedback/{id}/promote (R6.6, R6.7, R6.11)
# ---------------------------------------------------------------------------


class TestPromoteEndpoint:
    def test_promotes_reviewed_item_with_expected_answer(self, operator_client):
        client, service = operator_client
        service.records["fb-1"] = _feedback_record(
            "fb-1",
            expected_answer="42 is the answer",
            review_status=ReviewStatus.reviewed,
        )
        service.traces["trace-1"] = QueryTraceRecord(
            trace_id="trace-1",
            question="What is the answer?",
            route="documents",
            answer="42",
            evidence_status="supported",
            confidence="high",
            retrieved_hits=[],
        )

        resp = client.post("/feedback/fb-1/promote")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "feedback-fb-1"
        assert body["question"] == "What is the answer?"
        assert body["expected_answer"] == "42 is the answer"
        assert body["human_reviewed"] is True

    def test_missing_expected_answer_returns_400(self, operator_client):
        client, service = operator_client
        service.records["fb-1"] = _feedback_record(
            "fb-1",
            expected_answer=None,
            review_status=ReviewStatus.reviewed,
        )
        service.traces["trace-1"] = QueryTraceRecord(
            trace_id="trace-1",
            question="What?",
            route="documents",
            answer="42",
            evidence_status="supported",
            confidence="high",
            retrieved_hits=[],
        )

        resp = client.post("/feedback/fb-1/promote")
        assert resp.status_code == 400
        assert resp.json()["detail"] == "expected_answer_required"

    def test_already_promoted_returns_409(self, operator_client):
        client, service = operator_client
        service.records["fb-1"] = _feedback_record(
            "fb-1",
            expected_answer="the answer",
            review_status=ReviewStatus.reviewed,
            promoted_case_id="feedback-fb-1",
        )
        service.traces["trace-1"] = QueryTraceRecord(
            trace_id="trace-1",
            question="What?",
            route="documents",
            answer="42",
            evidence_status="supported",
            confidence="high",
            retrieved_hits=[],
        )

        resp = client.post("/feedback/fb-1/promote")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "already_in_evaluation_set"

    def test_nonexistent_feedback_returns_404(self, operator_client):
        client, _service = operator_client

        resp = client.post("/feedback/no-such-id/promote")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /feedback/{id}/resolve (R6.8)
# ---------------------------------------------------------------------------


class TestResolveEndpoint:
    def test_resolves_feedback_item(self, operator_client):
        client, service = operator_client
        service.records["fb-1"] = _feedback_record(
            "fb-1", review_status=ReviewStatus.reviewed
        )

        resp = client.post("/feedback/fb-1/resolve")
        assert resp.status_code == 200
        body = resp.json()
        assert body["review_status"] == "resolved"
        assert body["feedback_id"] == "fb-1"

    def test_resolved_item_stays_in_records(self, operator_client):
        client, service = operator_client
        service.records["fb-1"] = _feedback_record("fb-1")

        resp = client.post("/feedback/fb-1/resolve")
        assert resp.status_code == 200
        # The record is still present (not deleted)
        assert "fb-1" in service.records
        assert service.records["fb-1"].review_status == ReviewStatus.resolved

    def test_nonexistent_feedback_returns_404(self, operator_client):
        client, _service = operator_client

        resp = client.post("/feedback/no-such-id/resolve")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Operator gating (all three endpoints)
# ---------------------------------------------------------------------------


class TestOperatorGating:
    """All three feedback action endpoints require operator privileges."""

    def test_classify_rejects_non_operator(self, monkeypatch):
        """Non-operators get 403 from the require_operator dependency."""
        from rag_system.auth.dependencies import get_current_user

        api_module.app.dependency_overrides[get_current_user] = lambda: _NON_OPERATOR
        # Don't override require_operator — let the real dependency reject.
        try:
            client = TestClient(api_module.app)
            resp = client.post(
                "/feedback/fb-1/classify",
                json={"category": "Wrong route"},
            )
            # Should be rejected (403) because the user is not an operator
            assert resp.status_code == 403
        finally:
            api_module.app.dependency_overrides.pop(get_current_user, None)

    def test_promote_rejects_non_operator(self, monkeypatch):
        from rag_system.auth.dependencies import get_current_user

        api_module.app.dependency_overrides[get_current_user] = lambda: _NON_OPERATOR
        try:
            client = TestClient(api_module.app)
            resp = client.post("/feedback/fb-1/promote")
            assert resp.status_code == 403
        finally:
            api_module.app.dependency_overrides.pop(get_current_user, None)

    def test_resolve_rejects_non_operator(self, monkeypatch):
        from rag_system.auth.dependencies import get_current_user

        api_module.app.dependency_overrides[get_current_user] = lambda: _NON_OPERATOR
        try:
            client = TestClient(api_module.app)
            resp = client.post("/feedback/fb-1/resolve")
            assert resp.status_code == 403
        finally:
            api_module.app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# GET /feedback — inbox listing (R6.1-R6.4, task 11.1 wiring)
# ---------------------------------------------------------------------------


class TestListFeedbackEndpoint:
    def test_lists_only_negative_ratings_newest_first(self, operator_client):
        client, service = operator_client
        # Two negative (1,2) and one positive (5); positive must be excluded.
        service.records["fb-old"] = _feedback_record(
            "fb-old", rating=1
        ).model_copy(update={"created_at": "2024-06-15T09:00:00Z"})
        service.records["fb-new"] = _feedback_record(
            "fb-new", rating=2
        ).model_copy(update={"created_at": "2024-06-15T11:00:00Z"})
        service.records["fb-pos"] = _feedback_record("fb-pos", rating=5)

        resp = client.get("/feedback")
        assert resp.status_code == 200
        body = resp.json()
        ids = [item["feedback"]["feedback_id"] for item in body["items"]]
        # Positive excluded; negatives newest-first.
        assert ids == ["fb-new", "fb-old"]
        assert body["next_cursor"] is None

    def test_filters_by_review_status(self, operator_client):
        client, service = operator_client
        service.records["fb-unrev"] = _feedback_record(
            "fb-unrev", rating=1, review_status=ReviewStatus.unreviewed
        )
        service.records["fb-res"] = _feedback_record(
            "fb-res", rating=1, review_status=ReviewStatus.resolved
        )

        resp = client.get("/feedback", params={"review_status": "resolved"})
        assert resp.status_code == 200
        ids = [item["feedback"]["feedback_id"] for item in resp.json()["items"]]
        assert ids == ["fb-res"]

    def test_invalid_cursor_returns_400(self, operator_client):
        client, _service = operator_client
        resp = client.get("/feedback", params={"cursor": "not-a-valid-cursor"})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "invalid_cursor"

    def test_requires_operator(self, monkeypatch):
        from rag_system.auth.dependencies import get_current_user

        api_module.app.dependency_overrides[get_current_user] = lambda: _NON_OPERATOR
        service = _FakeService()
        monkeypatch.setattr(api_module, "get_service", lambda: service)
        try:
            client = TestClient(api_module.app)
            resp = client.get("/feedback")
            assert resp.status_code == 403
            assert resp.json()["detail"] == "operator_required"
        finally:
            api_module.app.dependency_overrides.pop(get_current_user, None)
