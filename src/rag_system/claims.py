"""Claim-level evidence mapping (R1).

This module derives, for a single claim, exactly one :class:`EvidenceStatus`
from that claim's associated evidence items. The classification is a pure,
deterministic function of the items' ``verification_result`` and ``coverage``
signals, so it is trivially unit- and property-testable without any model
round trip.

The four statuses (R1.4–R1.8) are:

* ``supported`` — some associated item has ``verification_result == entails``
  **and** ``coverage == full`` (an item entails the entire claim). (R1.4)
* ``partially_supported`` — one or more ``entails`` items cover only sub-parts
  (``coverage == partial``) and **no** single item is ``full``. (R1.5)
* ``unsupported`` — zero evidence items, or every associated item has a
  ``verification_result`` of ``does_not_entail``. (R1.6, R1.7)
* ``verification_unavailable`` — every associated item has a
  ``verification_result`` of ``undetermined``. (R1.8)
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from textwrap import dedent
from typing import Any

from .abstention import has_conflicting_evidence
from .config import Settings
from .llm import TextLLM, build_text_llm
from .models import (
    AnswerSpan,
    Claim,
    EvidenceCoverage,
    EvidenceItem,
    EvidenceStatus,
    RetrievalHit,
    VerificationResult,
)
from .observability import get_logger
from .observability_tracing.context import propagate_into_thread


def classify_evidence_status(
    evidence_items: Sequence[EvidenceItem],
) -> EvidenceStatus:
    """Derive exactly one :class:`EvidenceStatus` for a claim.

    Args:
        evidence_items: The evidence items associated with a single claim,
            each carrying its ``verification_result`` and ``coverage`` signal.

    Returns:
        The single :class:`EvidenceStatus` implied by the items, following the
        precedence defined by R1.4–R1.8.
    """
    # R1.7: a claim with zero associated evidence items is unsupported.
    if not evidence_items:
        return EvidenceStatus.unsupported

    has_full_entailment = False
    has_partial_entailment = False
    has_entails = False
    has_does_not_entail = False
    has_undetermined = False

    for item in evidence_items:
        if item.verification_result == VerificationResult.entails:
            has_entails = True
            if item.coverage == EvidenceCoverage.full:
                has_full_entailment = True
            elif item.coverage == EvidenceCoverage.partial:
                has_partial_entailment = True
        elif item.verification_result == VerificationResult.does_not_entail:
            has_does_not_entail = True
        else:  # VerificationResult.undetermined
            has_undetermined = True

    # R1.4: some item entails the entire claim.
    if has_full_entailment:
        return EvidenceStatus.supported

    # R1.5: entails items cover only sub-parts and none is full.
    if has_partial_entailment:
        return EvidenceStatus.partially_supported

    # R1.8: every associated item is undetermined.
    if has_undetermined and not has_entails and not has_does_not_entail:
        return EvidenceStatus.verification_unavailable

    # R1.6: at least one item and every item is does_not_entail.
    if has_does_not_entail and not has_entails and not has_undetermined:
        return EvidenceStatus.unsupported

    # Mixed remainders that reach here have no full/partial entailment support
    # and are not uniformly undetermined; the only supporting signal absent,
    # they are treated as unsupported (e.g. entails items all reporting
    # coverage == none, or a mix of does_not_entail and undetermined).
    return EvidenceStatus.unsupported


# ---------------------------------------------------------------------------
# ClaimMapper: decomposition + evidence association + verification (R1.1-R1.3)
# ---------------------------------------------------------------------------

logger = get_logger(__name__)

#: Upper bound on evidence items associated with a single claim (R1.2).
MAX_EVIDENCE_ITEMS = 100

#: Upper bound on the number of claims verified for one answer. Decomposition
#: rarely yields more, but this caps the worst-case verification fan-out so a
#: pathological answer cannot spawn an unbounded number of model calls.
MAX_CLAIMS = 100

#: Default ceiling on concurrent (claim, evidence) verification calls. The
#: per-pair entailment calls (R1.3) are otherwise a serial O(claims x hits)
#: chain on the answer path; running them through a bounded thread pool turns
#: that into roughly ``ceil(pairs / workers)`` round-trip latencies while
#: preserving the per-pair verdicts and the evidence-item count (R1.2).
DEFAULT_VERIFY_MAX_WORKERS = 8


@dataclass(frozen=True)
class ClaimMappingResult:
    """Outcome of mapping an answer to claims + evidence.

    Attributes:
        claims: The decomposed claims, each with its associated evidence items
            and a derived ``evidence_status``. Empty when decomposition failed.
        decomposition_failed: ``True`` when the answer could not be decomposed
            into claims (model error/timeout/unparseable output, R1.9). Surfaced
            via ``claim_decomposition_failed`` on the response by the caller
            (2.6). Never raised.
        conflicting_claim_ids: Claim ids flagged as carrying conflicting
            evidence during verification (≥1 ``entails`` AND ≥1
            ``does_not_entail`` from different ``document_id``s, R3.4). Consumed
            by the abstention gate.
    """

    claims: list[Claim] = field(default_factory=list)
    decomposition_failed: bool = False
    conflicting_claim_ids: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class _DecomposedClaim:
    """A single factual statement returned by the decomposition model."""

    text: str
    start: int
    end: int


@dataclass(frozen=True)
class _VerificationOutcome:
    """A (claim, evidence) verification result from the entailment model."""

    verification_result: VerificationResult
    coverage: EvidenceCoverage
    covered_subclaims: list[int] = field(default_factory=list)


def derive_claim_id(trace_id: str, claim_index: int) -> str:
    """Derive a stable ``claim_id`` from ``(trace_id, claim_index)`` (R1.1).

    The id is a deterministic function of the trace and the claim's position in
    the decomposition, so re-reading the same stored answer yields identical
    ids without persisting a counter.
    """
    digest = hashlib.sha256(f"{trace_id}:{claim_index}".encode("utf-8")).hexdigest()
    return f"claim-{digest[:16]}"


class ClaimMapper:
    """Decompose an answer into claims and associate verified evidence (R1).

    Decomposition (R1.1) and per-pair entailment verification (R1.3) are both
    LLM-based via the generation model (Gemini ``gemini-3.5-flash``). Both steps
    fail closed and never raise: a decomposition failure yields an empty claims
    list with ``decomposition_failed = True`` (R1.9); a verification failure for
    a pair yields ``undetermined`` with ``coverage == none`` (R1.8).
    """

    def __init__(self, settings: Settings, llm: TextLLM | None = None) -> None:
        self._llm = llm if llm is not None else build_text_llm(settings)
        self._model_id = self._llm.model_id
        self._verify_max_workers = max(
            1,
            int(
                getattr(
                    settings,
                    "claim_verification_max_workers",
                    DEFAULT_VERIFY_MAX_WORKERS,
                )
            ),
        )

    # -- Public API ---------------------------------------------------------

    def map_claims(
        self,
        answer_text: str,
        hits: list[RetrievalHit],
        trace_id: str,
    ) -> ClaimMappingResult:
        """Map ``answer_text`` to claims with associated, verified evidence.

        Args:
            answer_text: The generated answer prose to decompose.
            hits: The retrieved hits in scope, used as candidate evidence.
            trace_id: Trace id used to derive stable claim ids.

        Returns:
            A :class:`ClaimMappingResult`. Never raises on model error.
        """
        decomposed, failed = self._decompose(answer_text)
        if failed:
            return ClaimMappingResult(
                claims=[], decomposition_failed=True, conflicting_claim_ids=set()
            )

        # Cap the number of claims so a pathological decomposition cannot spawn
        # an unbounded verification fan-out (R1 bounds evidence per claim, not
        # the claim count).
        decomposed = decomposed[:MAX_CLAIMS]

        # Candidate evidence is drawn from the retrieved hits, capped at the
        # per-claim maximum (R1.2).
        candidate_hits = hits[:MAX_EVIDENCE_ITEMS]

        # Verify every (claim, evidence) pair concurrently. The per-pair
        # entailment call (R1.3) is a blocking model round trip; running the
        # pairs serially made this an O(claims x hits) chain on the answer path.
        # A bounded thread pool (with trace/span context propagated into each
        # worker) collapses that to roughly ceil(pairs / workers) latencies
        # while preserving the per-pair verdicts and the evidence-item count.
        verified: dict[tuple[int, int], EvidenceItem] = {}
        tasks = [
            (claim_index, hit_index, raw.text, hit)
            for claim_index, raw in enumerate(decomposed)
            for hit_index, hit in enumerate(candidate_hits)
        ]
        if tasks:
            max_workers = min(
                len(tasks),
                getattr(self, "_verify_max_workers", DEFAULT_VERIFY_MAX_WORKERS),
            )

            def _run(claim_text: str, hit: RetrievalHit) -> EvidenceItem:
                return self._verify_hit(claim_text, hit)

            with ThreadPoolExecutor(
                max_workers=max_workers, thread_name_prefix="claim-verify"
            ) as pool:
                future_to_key = {
                    pool.submit(
                        propagate_into_thread(_run), claim_text, hit
                    ): (claim_index, hit_index)
                    for claim_index, hit_index, claim_text, hit in tasks
                }
                for future in as_completed(future_to_key):
                    verified[future_to_key[future]] = future.result()

        claims: list[Claim] = []
        conflicting_claim_ids: set[str] = set()

        for claim_index, raw in enumerate(decomposed):
            claim_id = derive_claim_id(trace_id, claim_index)
            span = _clamp_span(raw.start, raw.end, len(answer_text))

            # Reassemble each claim's evidence in stable hit order.
            evidence_items: list[EvidenceItem] = [
                verified[(claim_index, hit_index)]
                for hit_index in range(len(candidate_hits))
            ]

            status = classify_evidence_status(evidence_items)
            claim = Claim(
                claim_id=claim_id,
                text=raw.text,
                answer_span=span,
                evidence_items=evidence_items,
                evidence_status=status,
            )
            claims.append(claim)

            # R3.4: flag claims with contradictory evidence for the abstention
            # gate (reuses the shared conflict predicate).
            if has_conflicting_evidence(claim):
                conflicting_claim_ids.add(claim_id)

        return ClaimMappingResult(
            claims=claims,
            decomposition_failed=False,
            conflicting_claim_ids=conflicting_claim_ids,
        )

    # -- Decomposition (R1.1, R1.9) ----------------------------------------

    def _decompose(self, answer_text: str) -> tuple[list[_DecomposedClaim], bool]:
        """Decompose the answer into factual statements with spans.

        Returns ``(claims, failed)``. ``failed`` is ``True`` only on model
        error/timeout/unparseable output (R1.9); a well-formed but empty list is
        not a failure (an answer may legitimately carry no factual statements).
        """
        prompt = build_decomposition_prompt(answer_text)
        try:
            raw_text, _usage = self._llm.generate(
                prompt, temperature=0.0, max_tokens=4096
            )
        except Exception:  # noqa: BLE001 - fail closed, never raise (R1.9)
            logger.warning(
                "Claim decomposition model call failed; returning empty claims",
                extra={"model_id": self._model_id},
            )
            return [], True

        parsed = _parse_decomposition(raw_text)
        if parsed is None:
            logger.warning(
                "Claim decomposition output was unparseable; returning empty claims",
                extra={"model_id": self._model_id},
            )
            return [], True
        return parsed, False

    # -- Verification (R1.3, R1.8) -----------------------------------------

    def _verify_hit(self, claim_text: str, hit: RetrievalHit) -> EvidenceItem:
        """Build a document ``EvidenceItem`` for ``hit`` carrying its verdict."""
        outcome = self._verify_pair(claim_text, hit)
        quote = hit.chunk.text
        return EvidenceItem(
            kind="document",
            verification_result=outcome.verification_result,
            coverage=outcome.coverage,
            covered_subclaims=outcome.covered_subclaims,
            quote=quote,
            source_start=0,
            source_end=len(quote),
            document_id=hit.chunk.document_id,
            document_version=hit.chunk.version,
        )

    def _verify_pair(self, claim_text: str, hit: RetrievalHit) -> _VerificationOutcome:
        """Run the entailment model for one (claim, evidence) pair (R1.3).

        Any model error, timeout, or unparseable/low-confidence response yields
        ``undetermined`` with ``coverage == none`` (R1.8), never a raise.
        """
        prompt = build_entailment_prompt(claim_text, hit.chunk.text)
        try:
            raw_text, _usage = self._llm.generate(
                prompt, temperature=0.0, max_tokens=1024
            )
        except Exception:  # noqa: BLE001 - fail closed to undetermined (R1.8)
            return _UNDETERMINED
        outcome = _parse_verification(raw_text)
        return outcome if outcome is not None else _UNDETERMINED


#: The fail-closed verification outcome (R1.8).
_UNDETERMINED = _VerificationOutcome(
    verification_result=VerificationResult.undetermined,
    coverage=EvidenceCoverage.none,
    covered_subclaims=[],
)


def _clamp_span(start: Any, end: Any, answer_len: int) -> AnswerSpan:
    """Clamp model-provided offsets to ``0 <= start <= end <= answer_len``."""
    try:
        start_i = int(start)
    except (TypeError, ValueError):
        start_i = 0
    try:
        end_i = int(end)
    except (TypeError, ValueError):
        end_i = answer_len
    start_i = max(0, min(start_i, answer_len))
    end_i = max(0, min(end_i, answer_len))
    if end_i < start_i:
        end_i = start_i
    return AnswerSpan(start=start_i, end=end_i)


def build_decomposition_prompt(answer_text: str) -> str:
    """Prompt the model to decompose an answer into factual statements (R1.1)."""
    return dedent(
        f"""
        Decompose the following answer into its distinct factual statements.
        Each statement must express exactly one factual claim, and carry the
        zero-based character offsets (start inclusive, end exclusive) of the
        span of the answer text it comes from.

        Return only JSON with this exact shape:
        {{
          "claims": [
            {{"text": "one factual statement", "start": 0, "end": 10}}
          ]
        }}

        If the answer contains no factual statements, return an empty claims
        list. Do not invent statements that are not in the answer.

        Answer:
        {answer_text}
        """
    ).strip()


def build_entailment_prompt(claim_text: str, evidence_text: str) -> str:
    """Prompt the model to judge whether evidence entails a claim (R1.3)."""
    return dedent(
        f"""
        Decide whether the evidence entails the claim. Respond with an
        entailment verdict and how much of the claim the evidence covers.

        verification_result must be one of: "entails", "does_not_entail",
        "undetermined".
        coverage must be one of: "full" (the evidence supports the entire
        claim), "partial" (it supports only part of the claim), or "none".

        Return only JSON with this exact shape:
        {{
          "verification_result": "entails|does_not_entail|undetermined",
          "coverage": "full|partial|none",
          "covered_subclaims": [0]
        }}

        Claim:
        {claim_text}

        Evidence:
        {evidence_text}
        """
    ).strip()


def _parse_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.IGNORECASE | re.DOTALL)
    if fenced:
        stripped = fenced.group(1).strip()
    try:
        payload = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _parse_decomposition(text: str) -> list[_DecomposedClaim] | None:
    """Parse the decomposition model output into claims, or ``None`` on error."""
    payload = _parse_json_object(text)
    if payload is None:
        return None
    raw_claims = payload.get("claims")
    if not isinstance(raw_claims, list):
        return None
    claims: list[_DecomposedClaim] = []
    for entry in raw_claims:
        if not isinstance(entry, dict):
            return None
        claim_text = entry.get("text")
        if not isinstance(claim_text, str) or not claim_text.strip():
            # Skip empty/malformed statements rather than failing the whole
            # decomposition.
            continue
        claims.append(
            _DecomposedClaim(
                text=claim_text.strip(),
                start=entry.get("start", 0),
                end=entry.get("end", 0),
            )
        )
    return claims


def _parse_verification(text: str) -> _VerificationOutcome | None:
    """Parse the entailment model output, or ``None`` for the fail-closed path."""
    payload = _parse_json_object(text)
    if payload is None:
        return None
    raw_result = payload.get("verification_result")
    raw_coverage = payload.get("coverage")
    try:
        result = VerificationResult(raw_result)
        coverage = EvidenceCoverage(raw_coverage)
    except ValueError:
        return None
    raw_subclaims = payload.get("covered_subclaims", [])
    covered: list[int] = []
    if isinstance(raw_subclaims, list):
        for value in raw_subclaims:
            if isinstance(value, int) and not isinstance(value, bool):
                covered.append(value)
    return _VerificationOutcome(
        verification_result=result,
        coverage=coverage,
        covered_subclaims=covered,
    )
