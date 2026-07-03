"""Unit test for scope-ambiguity clarification wording (R2.9).

When the classifier flags ``scope_ambiguous``, the resulting ClarificationPrompt
must ask whether to search the selected Documents or the entire Corpus.

Task 4.7 — validates Requirement 2.9.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from rag_system.clarification import (
    ClarificationStore,
    SCOPE_CLARIFICATION_QUESTION,
    resolve_clarification_question,
)
from rag_system.models import ClarificationPrompt
from rag_system.router import _parse_routing_response


class _FakeStore:
    """Minimal in-memory store satisfying the JsonStore protocol."""

    def __init__(self) -> None:
        self.writes: dict[str, object] = {}

    def create_json(self, key: str, payload: object) -> str:
        self.writes[key] = payload
        return key

    def get_json(self, key: str) -> object | None:
        return self.writes.get(key)


def _settings(expiry_minutes: int = 30) -> SimpleNamespace:
    return SimpleNamespace(clarification_expiry_minutes=expiry_minutes)


class TestScopeAmbiguityClarificationWording:
    """Assert clarification asks whether to search selected Documents or the entire Corpus."""

    def test_scope_ambiguous_classification_triggers_document_vs_corpus_question(
        self,
    ) -> None:
        """End-to-end: scope_ambiguous classification → ClarificationPrompt wording.

        1. Parse a routing response that sets scope_ambiguous=true.
        2. Resolve the clarification question via the standard helper.
        3. Issue the prompt via ClarificationStore.
        4. Assert the final ClarificationPrompt question references both
           'documents' (selected scope) and 'corpus' (entire scope).
        """
        # Step 1: Trigger scope_ambiguous via classifier output parsing.
        raw = (
            '{"route": "rag", "reasoning": "unclear which docs to search", '
            '"confidence": 0.4, "ambiguous": false, "scope_ambiguous": true, '
            '"clarification_question": null}'
        )
        decision = _parse_routing_response(raw)
        assert decision.scope_ambiguous is True
        assert decision.ambiguous is True  # scope_ambiguous implies ambiguous

        # Step 2: Resolve the question text using the standard logic.
        question = resolve_clarification_question(
            scope_ambiguous=decision.scope_ambiguous,
            clarification_question=decision.clarification_question,
        )

        # Step 3: Issue through the store to produce the full ClarificationPrompt.
        store = ClarificationStore(_FakeStore(), _settings())
        prompt = store.issue(
            original_question="Show me the latest metrics",
            conversation_turn_id="conv-scope:0",
            clarification_question=question,
            document_scope=["doc-1", "doc-2"],
        )

        # Step 4: Assert the ClarificationPrompt asks about documents vs. corpus.
        assert isinstance(prompt, ClarificationPrompt)
        q = prompt.clarification_question.lower()
        assert "documents" in q or "document" in q, (
            f"Expected wording to reference 'documents' (selected scope), got: {prompt.clarification_question}"
        )
        assert "corpus" in q, (
            f"Expected wording to reference 'corpus' (entire scope), got: {prompt.clarification_question}"
        )

    def test_scope_ambiguous_wording_mentions_selection_vs_entirety(self) -> None:
        """The wording must convey the choice between a subset and the full corpus."""
        question = resolve_clarification_question(
            scope_ambiguous=True,
            clarification_question="This should be ignored for scope ambiguity",
        )

        # Must reference selected/specific documents
        assert "selected" in question.lower() or "your" in question.lower(), (
            f"Expected reference to user's selected documents, got: {question}"
        )
        # Must reference the entire corpus
        assert "entire" in question.lower() or "all" in question.lower(), (
            f"Expected reference to full/entire corpus, got: {question}"
        )

    def test_scope_ambiguous_prompt_is_the_fixed_constant(self) -> None:
        """The scope clarification question uses the module-level constant."""
        question = resolve_clarification_question(
            scope_ambiguous=True,
            clarification_question=None,
        )
        assert question == SCOPE_CLARIFICATION_QUESTION

    def test_non_scope_ambiguity_does_not_use_scope_wording(self) -> None:
        """When ambiguity is NOT scope-related, the wording must NOT be the scope question."""
        raw = (
            '{"route": "rag", "reasoning": "under-specified entity", '
            '"confidence": 0.3, "ambiguous": true, "scope_ambiguous": false, '
            '"clarification_question": "Which project do you mean?"}'
        )
        decision = _parse_routing_response(raw)
        assert decision.ambiguous is True
        assert decision.scope_ambiguous is False

        question = resolve_clarification_question(
            scope_ambiguous=decision.scope_ambiguous,
            clarification_question=decision.clarification_question,
        )

        # Non-scope ambiguity should use the classifier's own question.
        assert question == "Which project do you mean?"
        # And it should NOT be the scope constant.
        assert question != SCOPE_CLARIFICATION_QUESTION


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
