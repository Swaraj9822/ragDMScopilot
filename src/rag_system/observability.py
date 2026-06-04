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
from threading import RLock
from typing import Any, Generator

from tenacity import (
    before_sleep_log,
    retry,
    stop_after_attempt,
    wait_exponential,
)

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

    def increment(
        self,
        name: str,
        labels: dict[str, Any] | None = None,
        amount: float = 1.0,
    ) -> None:
        key = (name, _normalise_labels(labels))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + amount

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
def timed(
    logger: logging.Logger, operation: str, **extra: Any
) -> Generator[None, None, None]:
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
):
    """Tenacity retry with exponential back-off.  Logs each retry at WARNING."""
    return retry(
        stop=stop_after_attempt(max_retries),
        wait=wait_exponential(multiplier=1, min=min_wait, max=max_wait),
        before_sleep=before_sleep_log(_retry_logger, logging.WARNING),
        reraise=True,
    )
