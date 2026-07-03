"""Span recording — the core in-process capture API for the tracing platform.

:class:`SpanRecorder` wraps the existing ``timed()`` semantics from
:mod:`rag_system.observability` and adds span lifecycle management. It creates a
:class:`Trace` with a Root_Span for each sampled request (or ingestion job) and
times each instrumented :class:`Span` as a child of the currently active span,
following the natural call nesting via the trace-context propagator.

This module implements task 4.1 (trace and span lifecycle) and task 4.2 (the
scalar-coercing ``set_attributes`` helper and the stage-specific attribute
helpers). ``record_span`` routes supplied attributes through ``set_attributes``
so every recorded value is a scalar (R3.7).

Requirements covered:

* R1.1 / R1.2 — Root_Span adopts the active trace_id when present, otherwise a
  newly generated 32-char lowercase hex trace_id unique among active traces.
* R1.3 — child Span has a unique span_id, a recorded start timestamp, and the
  currently active span as its parent.
* R1.4 / R1.5 / R4.3 — span duration recorded as a non-negative integer number
  of milliseconds on both the success and the error path.
* R1.6 — closing a span restores its parent as the active span.
* R1.7 — operation labels mirror those used by the ``timed`` helper, and the
  same operation metrics are emitted (R11.5 / R11.6).
* R1.8 — span-creation failures are logged and the stage proceeds with a no-op
  span sentinel, without propagating.
* R4.1 / R4.2 / R4.4 — on a pipeline exception the span status is ``error``, the
  exception type and a truncated (<= 4096 char) message are recorded, and the
  original exception is re-raised unchanged.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import Lock
from time import perf_counter
from typing import Any, Iterator

from ..observability import metrics as _default_metrics
from ..observability import reset_trace_id, set_trace_id
from . import context as _default_propagator
from .models import Span, Trace

__all__ = [
    "MAX_EXCEPTION_MESSAGE_LENGTH",
    "MAX_QUESTION_LENGTH",
    "NO_SCORE",
    "QUERY_SUMMARY_OPERATION",
    "ROOT_SPAN_OPERATION",
    "UNAVAILABLE",
    "SpanRecorder",
]

#: Maximum length of a recorded exception message attribute (R4.2).
MAX_EXCEPTION_MESSAGE_LENGTH = 4096

#: Operation label used for the Root_Span of every trace.
ROOT_SPAN_OPERATION = "Root_Span"

#: Operation label used for the per-request query summary span.
QUERY_SUMMARY_OPERATION = "query summary"

#: Maximum stored length of a recorded question attribute.
MAX_QUESTION_LENGTH = 500

#: Explicit sentinel recorded when an expected attribute value is missing — e.g.
#: a model id or token count the LLM provider did not return (R3.2), or a
#: document id/version that is unavailable at ingestion-stage completion (R12.5).
UNAVAILABLE = "unavailable"

#: Explicit sentinel recorded for the top-retrieval-score attribute when a
#: retrieval span completes with a hit count of 0, i.e. no score exists (R3.4).
NO_SCORE = "no score"

#: Scalar attribute types permitted on a span; anything else is stringified (R3.7).
_SCALAR_TYPES = (str, bool, int, float)

#: Metric names mirroring the ``timed`` helper (R1.7, R11.5, R11.6).
_OPERATION_TOTAL_METRIC = "rag_operation_total"
_OPERATION_DURATION_METRIC = "rag_operation_duration_ms"


def _new_span_id() -> str:
    """Return a fresh span_id (32-char lowercase hex)."""
    return uuid.uuid4().hex


def _utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


def _make_noop_span() -> Span:
    """Build a sentinel Span used when span creation is skipped or fails (R1.8, R10.1).

    The sentinel is a fully formed :class:`Span` so that user code inside the
    ``with`` block can safely read or annotate it, but it is never enqueued for
    persistence.
    """
    return Span(
        span_id="",
        parent_span_id=None,
        operation="",
        start_ts=_utcnow(),
        duration_ms=0,
        status="success",
        attributes={},
    )


class SpanRecorder:
    """Creates, times, annotates, and closes spans for the tracing platform.

    The recorder is the only component on the latency-sensitive request path
    that builds spans. It never touches the database: completed spans are handed
    to a bounded in-memory buffer for off-path persistence.
    """

    def __init__(
        self,
        sampler: Any,
        propagator: Any = _default_propagator,
        span_buffer: Any = None,
        metrics: Any = _default_metrics,
        logger: logging.Logger | None = None,
    ) -> None:
        self._sampler = sampler
        self._propagator = propagator
        self._span_buffer = span_buffer
        self._metrics = metrics
        self._logger = logger or logging.getLogger(__name__)
        # Track currently active trace_ids so generated ids are unique (R1.2).
        self._active_lock = Lock()
        self._active_trace_ids: set[str] = set()

    # ------------------------------------------------------------------
    # Trace lifecycle
    # ------------------------------------------------------------------

    @contextmanager
    def start_trace(
        self,
        *,
        trace_id: str | None,
        route: str,
        is_root_http: bool = True,
    ) -> Iterator[Span]:
        """Open a Trace with its Root_Span for the duration of the ``with`` block.

        Adopts the active trace_id when one is present (an explicit *trace_id*
        argument, e.g. from the ``X-Trace-Id`` header, or the trace_id already on
        the context), otherwise generates a unique 32-char lowercase hex trace_id
        (R1.1, R1.2). When the sampler declines to record the trace, a no-op span
        sentinel is yielded and nothing is persisted (R10.1).
        """
        has_trace_header = trace_id is not None
        effective_trace_id = trace_id or self._propagator.get_active_trace_id()

        if not self._sampler.should_record(
            trace_id=effective_trace_id,
            has_trace_header=has_trace_header,
        ):
            # Mark the context as not recording so any record_span calls nested
            # inside this block are skipped entirely (R10.1, R10.8).
            recording_token = self._propagator.set_recording(False)
            try:
                yield _make_noop_span()
            finally:
                self._propagator.reset_recording(recording_token)
            return

        # Resolve (or generate) the trace_id used to identify this Trace.
        try:
            resolved_trace_id = self._resolve_trace_id(effective_trace_id)
            span_id = _new_span_id()
            start_ts = _utcnow()
            root_span = Span(
                span_id=span_id,
                parent_span_id=None,
                operation=ROOT_SPAN_OPERATION,
                start_ts=start_ts,
                duration_ms=0,
                status="success",
                attributes={},
                trace_id=resolved_trace_id,
                route=route,
            )
            Trace(
                trace_id=resolved_trace_id,
                route=route,
                start_ts=start_ts,
                duration_ms=0,
                root_status="success",
                spans=[root_span],
            )
            trace_token = set_trace_id(resolved_trace_id)
            span_token = self._propagator.bind_span(span_id)
        except Exception:  # pragma: no cover - defensive (R1.8)
            self._logger.warning(
                "Failed to create Root_Span for route %s; continuing without a span",
                route,
                exc_info=True,
            )
            yield _make_noop_span()
            return

        # The trace is being recorded: mark the context so nested record_span
        # calls create and enqueue real spans (R10.1).
        recording_token = self._propagator.set_recording(True)
        t0 = perf_counter()
        try:
            yield root_span
        except Exception as exc:
            self._close_span(
                root_span,
                t0,
                status="error",
                operation=ROOT_SPAN_OPERATION,
                exc=exc,
                span_token=span_token,
                trace_token=trace_token,
                trace_id=resolved_trace_id,
                recording_token=recording_token,
            )
            raise
        else:
            self._close_span(
                root_span,
                t0,
                status="success",
                operation=ROOT_SPAN_OPERATION,
                exc=None,
                span_token=span_token,
                trace_token=trace_token,
                trace_id=resolved_trace_id,
                recording_token=recording_token,
            )

    # ------------------------------------------------------------------
    # Span lifecycle
    # ------------------------------------------------------------------

    @contextmanager
    def record_span(self, operation: str, **attributes: Any) -> Iterator[Span]:
        """Open a child Span of the active span for the duration of the block.

        The span is timed, its status is set to ``success`` on normal completion
        or ``error`` on exception, the operation metrics are emitted with the
        same labels as the ``timed`` helper, and any exception is re-raised
        unchanged after the span is recorded (R1.3-R1.7, R4.1-R4.4).

        If span creation itself fails, the failure is logged and a no-op span
        sentinel is yielded so the stage continues without propagating (R1.8).
        """
        # When the enclosing trace is not being recorded (tracing disabled or
        # the trace was not sampled), skip span creation entirely: yield a no-op
        # sentinel and enqueue nothing, mirroring the disabled-trace behaviour
        # (R10.1, R10.8).
        if not self._propagator.is_recording():
            yield _make_noop_span()
            return

        try:
            span_id = _new_span_id()
            parent_span_id = self._propagator.get_active_span_id()
            span = Span(
                span_id=span_id,
                parent_span_id=parent_span_id,
                operation=operation,
                start_ts=_utcnow(),
                duration_ms=0,
                status="success",
                attributes={},
                # Stamp the owning trace so the off-path flush worker can group
                # this span with its trace without any external context (R9.1).
                trace_id=self._propagator.get_active_trace_id(),
            )
            # Route the supplied attributes through the scalar-coercion layer so
            # non-scalar values are stringified before being attached (R3.7).
            self.set_attributes(span, **attributes)
            span_token = self._propagator.bind_span(span_id)
        except Exception:
            self._logger.warning(
                "Failed to create span for operation %s; continuing without a span",
                operation,
                exc_info=True,
            )
            yield _make_noop_span()
            return

        t0 = perf_counter()
        try:
            yield span
        except Exception as exc:
            self._close_span(
                span,
                t0,
                status="error",
                operation=operation,
                exc=exc,
                span_token=span_token,
            )
            raise
        else:
            self._close_span(
                span,
                t0,
                status="success",
                operation=operation,
                exc=None,
                span_token=span_token,
            )

    # ------------------------------------------------------------------
    # Attribute recording
    # ------------------------------------------------------------------

    def set_attributes(self, span: Span, **attributes: Any) -> None:
        """Attach *attributes* to *span*, coercing non-scalar values (R3.7).

        Every value whose type is not natively ``str``, ``int``, ``float`` or
        ``bool`` is replaced with its ``str()`` representation before being
        stored; scalar values pass through with their native type intact. (Note
        that ``bool`` is a subclass of ``int`` but is preserved as a boolean,
        since both are permitted scalar types.)
        """
        for key, value in attributes.items():
            span.attributes[key] = value if isinstance(value, _SCALAR_TYPES) else str(value)

    # ------------------------------------------------------------------
    # Stage-specific attribute helpers (R3, R12)
    #
    # Each is a thin wrapper over ``set_attributes`` using the agreed keys.
    # Absent values (passed as ``None``) are recorded with an explicit sentinel
    # rather than omitted, so a missing datum is always distinguishable from one
    # that was never expected.
    # ------------------------------------------------------------------

    def set_generation_attributes(
        self,
        span: Span,
        *,
        model_id: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
    ) -> None:
        """Record generation/routing span attributes (R3.1, R3.2).

        Records the model identifier (str) and the prompt/completion/total token
        counts (int >= 0). When the LLM provider does not return the model id or
        one or more token counts, each absent value is recorded with the
        :data:`UNAVAILABLE` sentinel; all available values are still recorded and
        no error is raised.
        """
        self.set_attributes(
            span,
            model_id=model_id if model_id is not None else UNAVAILABLE,
            prompt_tokens=prompt_tokens if prompt_tokens is not None else UNAVAILABLE,
            completion_tokens=(
                completion_tokens if completion_tokens is not None else UNAVAILABLE
            ),
            total_tokens=total_tokens if total_tokens is not None else UNAVAILABLE,
        )

    #: Routing spans carry the same attribute shape as generation spans (R3.1).
    set_routing_attributes = set_generation_attributes

    def set_retrieval_attributes(
        self,
        span: Span,
        *,
        retrieval_mode: str,
        hit_count: int,
        top_score: float | int | None = None,
    ) -> None:
        """Record retrieval span attributes (R3.3, R3.4).

        Records the retrieval mode (str), the hit count (int >= 0), and the top
        retrieval score (number). When ``hit_count == 0`` (or no score is
        available), the top-score attribute is recorded with the
        :data:`NO_SCORE` sentinel instead of a number.
        """
        if hit_count == 0 or top_score is None:
            top = NO_SCORE
        else:
            top = top_score
        self.set_attributes(
            span,
            retrieval_mode=retrieval_mode,
            hit_count=hit_count,
            top_score=top,
        )

    def set_answer_generation_attributes(
        self,
        span: Span,
        *,
        evidence_status: str,
        citation_count: int,
    ) -> None:
        """Record answer-generation span attributes (R3.5).

        Records the evidence status (str) and the citation count (int >= 0).
        """
        self.set_attributes(
            span,
            evidence_status=evidence_status,
            citation_count=citation_count,
        )

    def set_query_summary_attributes(
        self,
        span: Span,
        *,
        question: str,
        confidence_score: float | None = None,
        total_tokens: int | None = None,
    ) -> None:
        """Record a per-request query summary on *span*.

        Captures the user-facing question, the numeric confidence score, and the
        total LLM tokens consumed across the whole request. Absent numeric values
        are recorded with the :data:`UNAVAILABLE` sentinel. The question is
        truncated to keep span payloads bounded. The ``summary_kind`` marker lets
        consumers (e.g. the Individual Query view) find this span unambiguously.
        """
        trimmed = question.strip()
        if len(trimmed) > MAX_QUESTION_LENGTH:
            trimmed = trimmed[: MAX_QUESTION_LENGTH - 1].rstrip() + "\u2026"
        self.set_attributes(
            span,
            summary_kind="query",
            question=trimmed,
            confidence_score=(
                confidence_score if confidence_score is not None else UNAVAILABLE
            ),
            total_tokens=total_tokens if total_tokens is not None else UNAVAILABLE,
        )

    def set_document_id(self, span: Span, document_id: Any) -> None:
        """Record the document identifier on *span* as a string attribute (R3.6).

        Recorded regardless of whether the stage succeeds or the document is
        valid. The value is coerced to a scalar by :meth:`set_attributes`.
        """
        self.set_attributes(span, document_id=document_id)

    def set_trace_config(
        self,
        span: Span,
        *,
        ai_configuration_version_id: str | None,
        resolved_settings: dict[str, Any] | None = None,
    ) -> None:
        """Record the producing AI configuration version on the trace (R9.1, R9.2, R9.11).

        Stamps the ``ai_configuration_version_id`` and the redacted resolved
        settings onto the span so the flush worker can propagate them to the
        assembled :class:`Trace`. The *resolved_settings* must already be
        redacted (the caller is responsible for calling
        :func:`~.config_redaction.build_trace_config_payload` which performs
        the deep-copy and redaction).

        When the version cannot be resolved the caller passes the
        ``UNRESOLVED_VERSION_ID`` sentinel and an empty settings dict, ensuring
        the trace still retains all other data (R9.2).
        """
        self.set_attributes(
            span,
            ai_configuration_version_id=ai_configuration_version_id or "unresolved",
        )
        # Store as a span-level attribute for the flush worker to extract.
        span.attributes["_resolved_settings"] = str(resolved_settings or {})
        # Also attach directly to the Trace via the span's trace_id reference,
        # so the flush worker's group_spans_by_trace can propagate it.
        span._trace_config_version_id = ai_configuration_version_id  # type: ignore[attr-defined]
        span._trace_resolved_settings = resolved_settings or {}  # type: ignore[attr-defined]

    def set_ingestion_attributes(
        self,
        span: Span,
        *,
        document_id: str | None = None,
        document_version: Any = None,
        source_filename: str | None = None,
    ) -> None:
        """Record ingestion-stage span attributes (R12.4, R12.5).

        Records the document identifier, document version, and (when known) the
        original source filename so the Individual Query view can label an
        upload. When a value is unavailable (passed as ``None``), the
        corresponding attribute is recorded with the :data:`UNAVAILABLE` sentinel.
        """
        self.set_attributes(
            span,
            document_id=document_id if document_id is not None else UNAVAILABLE,
            document_version=(
                document_version if document_version is not None else UNAVAILABLE
            ),
            source_filename=(
                source_filename if source_filename is not None else UNAVAILABLE
            ),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_trace_id(self, candidate: str | None) -> str:
        """Adopt *candidate* when present, else generate a unique trace_id (R1.2)."""
        with self._active_lock:
            if candidate is None:
                candidate = _new_span_id()
                while candidate in self._active_trace_ids:
                    candidate = _new_span_id()
            self._active_trace_ids.add(candidate)
        return candidate

    def _release_trace_id(self, trace_id: str) -> None:
        """Drop *trace_id* from the active set once its trace closes."""
        with self._active_lock:
            self._active_trace_ids.discard(trace_id)

    def _close_span(
        self,
        span: Span,
        t0: float,
        *,
        status: str,
        operation: str,
        exc: BaseException | None,
        span_token: Any,
        trace_token: Any = None,
        trace_id: str | None = None,
        recording_token: Any = None,
    ) -> None:
        """Finalise a span: duration, status, exception attrs, metrics, cleanup.

        Restores the parent span (and, for a root span, the trace context and
        active-id registry) and enqueues the completed span for persistence.
        Cleanup never propagates an error to the caller.
        """
        elapsed_ms = (perf_counter() - t0) * 1000.0
        span.duration_ms = max(0, round(elapsed_ms))
        span.status = "error" if status == "error" else "success"

        if exc is not None:
            self._record_exception(span, exc)

        self._emit_operation_metrics(operation, span.status, elapsed_ms)

        # Restore the active span to this span's parent (R1.6).
        try:
            self._propagator.restore_span(span_token)
        except Exception:  # pragma: no cover - defensive
            self._logger.warning(
                "Failed to restore parent span after %s", operation, exc_info=True
            )

        if trace_token is not None:
            try:
                reset_trace_id(trace_token)
            except Exception:  # pragma: no cover - defensive
                self._logger.warning("Failed to reset trace context", exc_info=True)
        if recording_token is not None:
            try:
                self._propagator.reset_recording(recording_token)
            except Exception:  # pragma: no cover - defensive
                self._logger.warning(
                    "Failed to reset recording state", exc_info=True
                )
        if trace_id is not None:
            self._release_trace_id(trace_id)

        self._enqueue(span)

    def _record_exception(self, span: Span, exc: BaseException) -> None:
        """Record the exception type and a truncated message on *span* (R4.2)."""
        span.attributes["exception.type"] = type(exc).__name__
        span.attributes["exception.message"] = str(exc)[:MAX_EXCEPTION_MESSAGE_LENGTH]

    def _emit_operation_metrics(
        self, operation: str, status: str, elapsed_ms: float
    ) -> None:
        """Emit the same operation metrics as the ``timed`` helper (R11.5, R11.6)."""
        labels = {"operation": operation, "status": status}
        try:
            self._metrics.increment(_OPERATION_TOTAL_METRIC, labels)
            self._metrics.observe(_OPERATION_DURATION_METRIC, elapsed_ms, labels)
        except Exception:  # pragma: no cover - defensive
            self._logger.warning(
                "Failed to emit operation metrics for %s", operation, exc_info=True
            )

    def _enqueue(self, span: Span) -> None:
        """Enqueue a completed span to the span buffer, non-blocking (R1.8 spirit)."""
        if self._span_buffer is None:
            return
        try:
            self._span_buffer.add(span)
        except Exception:  # pragma: no cover - defensive
            self._logger.warning(
                "Failed to enqueue span %s for persistence", span.span_id, exc_info=True
            )
