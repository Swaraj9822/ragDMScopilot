"""Centralised logging and retry helpers for the RAG service."""

from __future__ import annotations

import json as _json
import logging
import os
import sys
import time
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from enum import StrEnum as _StrEnum
from functools import wraps as _wraps
import threading
from threading import RLock
from typing import Any, Generator

from tenacity import (
    before_sleep_log,
    retry,
    stop_after_attempt,
    wait_exponential,
)


# ---------------------------------------------------------------------------
# Request-level timeout error
# ---------------------------------------------------------------------------


class RequestTimeoutError(Exception):
    """Raised when a request exceeds its configured wall-clock timeout."""

    def __init__(self, operation: str, elapsed_s: float, limit_s: int):
        self.operation = operation
        self.elapsed_s = elapsed_s
        self.limit_s = limit_s
        super().__init__(f"Request timed out: {operation} took {elapsed_s:.1f}s (limit {limit_s}s)")


# ---------------------------------------------------------------------------
# Structured JSON formatter
# ---------------------------------------------------------------------------

_EXTRA_FIELDS = (
    "answer_chars",
    "avg_score",
    "citation_count",
    "context_chars",
    "dense_dimension",
    "doc_filter_count",
    "document_id",
    "dominant_doc_ratio",
    "embedding_input_chars",
    "evidence_status",
    "version",
    "trace_id",
    "chunk_count",
    "duration_ms",
    "status_code",
    "method",
    "path",
    "model_id",
    "top_k",
    "top_score",
    "hit_count",
    "query_len",
    "min_score",
    "missing_sparse_count",
    "prompt_chars",
    "retrieval_mode",
    "vector_count",
    "s3_key",
    "sparse_count",
    "sparse_term_count",
    "top_match_ids",
    "unique_doc_count",
    "file_name",
)

_TRACE_ID: ContextVar[str | None] = ContextVar("rag_trace_id", default=None)


def get_trace_id() -> str | None:
    return _TRACE_ID.get()


def set_trace_id(trace_id: str):
    return _TRACE_ID.set(trace_id)


def reset_trace_id(token: Any) -> None:
    _TRACE_ID.reset(token)


class _TraceContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "trace_id"):
            record.trace_id = get_trace_id() or "-"
        return True


class _JSONFormatter(logging.Formatter):
    """One JSON object per line — ready for CloudWatch / Datadog / ELK."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            entry["exc"] = self.formatException(record.exc_info)
        for key in _EXTRA_FIELDS:
            val = getattr(record, key, None)
            if val is not None and not (key == "trace_id" and val == "-"):
                entry[key] = val
        return _json.dumps(entry, default=str)


class MetricsRegistry:
    """Small in-process metrics store with Prometheus text exposition."""

    def __init__(self, max_samples: int = 1000) -> None:
        self._lock = RLock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._samples: dict[tuple[str, tuple[tuple[str, str], ...]], deque[float]] = {}
        self._sample_sums: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._max_samples = max_samples

        self._cw_client = None
        self._cw_namespace = os.getenv("RAG_CW_NAMESPACE", "ProductionRAG")
        self._flush_interval = int(os.getenv("RAG_CW_FLUSH_INTERVAL", "60"))
        self._thread = None
        self._running = False
        self._cw_buffer: list[dict[str, Any]] = []

    def start_cloudwatch_flusher(self, session: Any = None) -> None:
        with self._lock:
            if self._running:
                return
            if os.getenv("RAG_CW_ENABLED", "").lower() not in ("true", "1", "yes"):
                return

            try:
                import boto3

                self._cw_client = (session or boto3.session.Session()).client("cloudwatch")
                self._running = True
                self._thread = threading.Thread(
                    target=self._flush_loop, daemon=True, name="MetricsFlusher"
                )
                self._thread.start()
                logging.getLogger(__name__).info("CloudWatch metrics flusher started")
            except Exception as e:
                logging.getLogger(__name__).error(
                    "Failed to start CloudWatch metrics flusher: %s", e
                )

    def stop_cloudwatch_flusher(self) -> None:
        with self._lock:
            self._running = False
        if self._thread:
            self._thread.join(timeout=5.0)

    def _flush_loop(self) -> None:
        while self._running:
            time.sleep(self._flush_interval)
            try:
                self.flush_to_cloudwatch()
            except Exception as e:
                logging.getLogger(__name__).warning("Failed to flush metrics to CloudWatch: %s", e)

    def flush_to_cloudwatch(self) -> None:
        with self._lock:
            buffer = self._cw_buffer
            self._cw_buffer = []

        if not buffer or not self._cw_client:
            return

        counter_deltas: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        sample_aggs: dict[tuple[str, tuple[tuple[str, str], ...]], list[float]] = {}

        for item in buffer:
            k = (item["name"], item["labels"])
            if item["type"] == "counter":
                counter_deltas[k] = counter_deltas.get(k, 0.0) + item["value"]
            else:
                if k not in sample_aggs:
                    sample_aggs[k] = []
                sample_aggs[k].append(item["value"])

        metric_data = []
        for (name, labels), value in counter_deltas.items():
            dimensions = [{"Name": k, "Value": v[:255]} for k, v in labels][:10]
            metric_data.append(
                {"MetricName": name, "Dimensions": dimensions, "Value": value, "Unit": "Count"}
            )

        for (name, labels), values in sample_aggs.items():
            dimensions = [{"Name": k, "Value": v[:255]} for k, v in labels][:10]
            metric_data.append(
                {
                    "MetricName": name,
                    "Dimensions": dimensions,
                    "StatisticValues": {
                        "SampleCount": len(values),
                        "Sum": sum(values),
                        "Minimum": min(values),
                        "Maximum": max(values),
                    },
                    "Unit": "None",
                }
            )

        for i in range(0, len(metric_data), 1000):
            batch = metric_data[i : i + 1000]
            if batch:
                self._cw_client.put_metric_data(Namespace=self._cw_namespace, MetricData=batch)

    def increment(
        self,
        name: str,
        labels: dict[str, Any] | None = None,
        amount: float = 1.0,
    ) -> None:
        key = (name, _normalise_labels(labels))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + amount
            if self._running:
                self._cw_buffer.append(
                    {"name": name, "labels": key[1], "value": amount, "type": "counter"}
                )

    def observe(self, name: str, value: float, labels: dict[str, Any] | None = None) -> None:
        key = (name, _normalise_labels(labels))
        with self._lock:
            if key not in self._samples:
                self._samples[key] = deque(maxlen=self._max_samples)
                self._sample_sums[key] = 0.0
            if len(self._samples[key]) == self._samples[key].maxlen:
                self._sample_sums[key] -= self._samples[key][0]
            self._samples[key].append(float(value))
            self._sample_sums[key] += float(value)
            if self._running:
                self._cw_buffer.append(
                    {"name": name, "labels": key[1], "value": float(value), "type": "observe"}
                )

    def render_prometheus(self) -> str:
        lines = [
            "# HELP rag_build_info Static application build metadata.",
            "# TYPE rag_build_info gauge",
            'rag_build_info{service="production-rag"} 1',
        ]
        with self._lock:
            rendered_counter_types: set[str] = set()
            for (name, labels), value in sorted(self._counters.items()):
                if name not in rendered_counter_types:
                    lines.append(f"# TYPE {name} counter")
                    rendered_counter_types.add(name)
                lines.append(f"{name}{_format_labels(labels)} {_format_number(value)}")

            rendered_sample_types: set[str] = set()
            for (name, labels), values in sorted(self._samples.items()):
                if not values:
                    continue
                sorted_values = sorted(values)
                count = len(sorted_values)
                sample_sum = self._sample_sums[(name, labels)]
                if name not in rendered_sample_types:
                    lines.append(f"# TYPE {name} summary")
                    rendered_sample_types.add(name)
                for quantile in (0.5, 0.95, 0.99):
                    labels_with_quantile = (*labels, ("quantile", str(quantile)))
                    lines.append(
                        f"{name}{_format_labels(labels_with_quantile)} "
                        f"{_format_number(_quantile(sorted_values, quantile))}"
                    )
                lines.append(f"{name}_count{_format_labels(labels)} {count}")
                lines.append(f"{name}_sum{_format_labels(labels)} {_format_number(sample_sum)}")
                lines.append(
                    f"{name}_min{_format_labels(labels)} {_format_number(sorted_values[0])}"
                )
                lines.append(
                    f"{name}_max{_format_labels(labels)} {_format_number(sorted_values[-1])}"
                )
        return "\n".join(lines) + "\n"


def _normalise_labels(labels: dict[str, Any] | None) -> tuple[tuple[str, str], ...]:
    if not labels:
        return ()
    return tuple(sorted((str(key), str(value)) for key, value in labels.items()))


def _format_labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    rendered = ",".join(f'{key}="{_escape_label(value)}"' for key, value in labels)
    return f"{{{rendered}}}"


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_number(value: float) -> str:
    return f"{value:.6g}"


def _quantile(sorted_values: list[float], quantile: float) -> float:
    if len(sorted_values) == 1:
        return sorted_values[0]
    index = round((len(sorted_values) - 1) * quantile)
    return sorted_values[index]


metrics = MetricsRegistry()


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def setup_logging(level: str | None = None) -> None:
    """Call once at application startup.

    Env vars
    --------
    LOG_LEVEL  – DEBUG / INFO / WARNING  (default INFO)
    LOG_FORMAT – ``text`` (default) or ``json``
    """
    log_level = getattr(
        logging,
        (level or os.getenv("LOG_LEVEL", "INFO")).upper(),
        logging.INFO,
    )
    use_json = os.getenv("LOG_FORMAT", "text").lower() == "json"

    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(log_level)

    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(_TraceContextFilter())
    if use_json:
        handler.setFormatter(_JSONFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s  %(levelname)-8s  [%(name)s]  [trace=%(trace_id)s]  %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
    root.addHandler(handler)

    # Reduce noise from chatty libraries
    for noisy in ("boto3", "botocore", "urllib3", "pinecone", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger."""
    return logging.getLogger(name)


# ---------------------------------------------------------------------------
# Timing context manager
# ---------------------------------------------------------------------------


@contextmanager
def timed(logger: logging.Logger, operation: str, **extra: Any) -> Generator[None, None, None]:
    """Log *operation* start, duration on success, or error with traceback."""
    logger.info("Starting %s", operation, extra=extra)
    t0 = time.perf_counter()
    try:
        yield
    except Exception:
        ms = (time.perf_counter() - t0) * 1000
        labels = {"operation": operation, "status": "error"}
        metrics.increment("rag_operation_total", labels)
        metrics.observe("rag_operation_duration_ms", ms, labels)
        logger.error(
            "%s failed after %.0fms",
            operation,
            ms,
            extra={**extra, "duration_ms": ms},
            exc_info=True,
        )
        raise
    else:
        ms = (time.perf_counter() - t0) * 1000
        labels = {"operation": operation, "status": "success"}
        metrics.increment("rag_operation_total", labels)
        metrics.observe("rag_operation_duration_ms", ms, labels)
        logger.info(
            "%s completed in %.0fms",
            operation,
            ms,
            extra={**extra, "duration_ms": ms},
        )


# ---------------------------------------------------------------------------
# Retry decorator for external API calls
# ---------------------------------------------------------------------------

_MAX_RETRIES = int(os.getenv("RAG_MAX_RETRIES", "3"))
_MIN_WAIT_S = float(os.getenv("RAG_RETRY_MIN_WAIT", "1"))
_MAX_WAIT_S = float(os.getenv("RAG_RETRY_MAX_WAIT", "30"))

_retry_logger = logging.getLogger("rag_system.retry")


def retry_on_transient(
    *,
    max_retries: int = _MAX_RETRIES,
    min_wait: float = _MIN_WAIT_S,
    max_wait: float = _MAX_WAIT_S,
    retryable_exceptions: tuple[type[Exception], ...] | None = None,
):
    """Tenacity retry with exponential back-off.  Logs each retry at WARNING."""
    from tenacity import retry_if_exception_type

    if retryable_exceptions is None:
        try:
            from botocore.exceptions import BotoCoreError
            from pinecone import PineconeException

            retryable_exceptions = (ConnectionError, TimeoutError, BotoCoreError, PineconeException)
        except ImportError:
            retryable_exceptions = (ConnectionError, TimeoutError)

    return retry(
        stop=stop_after_attempt(max_retries),
        wait=wait_exponential(multiplier=1, min=min_wait, max=max_wait),
        before_sleep=before_sleep_log(_retry_logger, logging.WARNING),
        retry=retry_if_exception_type(retryable_exceptions),
        reraise=True,
    )


# ---------------------------------------------------------------------------
# Circuit breaker for external services
# ---------------------------------------------------------------------------


class CircuitState(_StrEnum):
    closed = "closed"
    open = "open"
    half_open = "half_open"


class CircuitOpenError(Exception):
    """Raised when a call is rejected because the circuit breaker is open."""

    def __init__(self, name: str, opened_seconds_ago: float):
        self.name = name
        self.opened_seconds_ago = opened_seconds_ago
        super().__init__(
            f"Circuit breaker '{name}' is OPEN "
            f"(opened {opened_seconds_ago:.1f}s ago). Failing fast."
        )


class CircuitBreaker:
    """Thread-safe circuit breaker with CLOSED → OPEN → HALF_OPEN → CLOSED lifecycle."""

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout_s: float = 30.0,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout_s = recovery_timeout_s

        self._state = CircuitState.closed
        self._failure_count = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()
        self._logger = logging.getLogger(f"rag_system.circuit.{name}")

    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._effective_state()

    def _effective_state(self) -> CircuitState:
        """Return effective state, promoting OPEN → HALF_OPEN after cooldown."""
        if self._state == CircuitState.open and self._opened_at is not None:
            elapsed = time.perf_counter() - self._opened_at
            if elapsed >= self.recovery_timeout_s:
                return CircuitState.half_open
        return self._state

    def allow_request(self) -> bool:
        """Check if a request should be allowed through."""
        with self._lock:
            effective = self._effective_state()
            if effective == CircuitState.closed:
                return True
            if effective == CircuitState.half_open:
                # Allow one trial call
                self._state = CircuitState.half_open
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            if self._state in (CircuitState.half_open, CircuitState.open):
                self._logger.info("Circuit '%s' CLOSED after successful trial call", self.name)
                metrics.increment(
                    "rag_circuit_state_total", {"provider": self.name, "state": "closed"}
                )
            self._state = CircuitState.closed
            self._failure_count = 0
            self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            if self._state == CircuitState.half_open:
                # Trial call failed — reopen
                self._state = CircuitState.open
                self._opened_at = time.perf_counter()
                self._logger.warning(
                    "Circuit '%s' re-OPENED after failed trial call (failures=%d)",
                    self.name,
                    self._failure_count,
                )
                metrics.increment(
                    "rag_circuit_state_total", {"provider": self.name, "state": "open"}
                )
            elif self._failure_count >= self.failure_threshold:
                self._state = CircuitState.open
                self._opened_at = time.perf_counter()
                self._logger.warning(
                    "Circuit '%s' OPENED after %d consecutive failures",
                    self.name,
                    self._failure_count,
                )
                metrics.increment(
                    "rag_circuit_state_total", {"provider": self.name, "state": "open"}
                )


# Registry of named circuit breakers
_circuit_registry: dict[str, CircuitBreaker] = {}
_circuit_registry_lock = threading.Lock()


def get_circuit_breaker(
    name: str,
    failure_threshold: int = 5,
    recovery_timeout_s: float = 30.0,
) -> CircuitBreaker:
    """Get or create a named circuit breaker (singleton per name)."""
    with _circuit_registry_lock:
        if name not in _circuit_registry:
            _circuit_registry[name] = CircuitBreaker(name, failure_threshold, recovery_timeout_s)
        return _circuit_registry[name]


def circuit(
    name: str,
    failure_threshold: int = 5,
    recovery_timeout_s: float = 30.0,
):
    """Decorator that wraps a function with a named circuit breaker.

    Place *outside* @retry_on_transient so that an OPEN circuit fails fast
    without entering the retry/backoff loop.
    """

    def decorator(fn):
        @_wraps(fn)
        def wrapper(*args, **kwargs):
            cb = get_circuit_breaker(name, failure_threshold, recovery_timeout_s)
            if not cb.allow_request():
                opened_ago = time.perf_counter() - cb._opened_at if cb._opened_at else 0.0
                raise CircuitOpenError(name, opened_ago)
            try:
                result = fn(*args, **kwargs)
            except Exception:
                cb.record_failure()
                raise
            else:
                cb.record_success()
                return result

        return wrapper

    return decorator
