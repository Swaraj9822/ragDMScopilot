"""Trace sampling decision for the AI observability tracing platform.

The :class:`TraceSampler` decides, once per trace at creation time, whether a
trace is recorded. The decision honours the global enablement flag, the
``X-Trace-Id`` header force-sample override, and the configured sampling rate
(R10.1, R10.4, R10.5, R10.7, R10.8).

Validation of an invalid ``sample_rate`` (non-numeric or outside ``[0.0, 1.0]``)
is handled at startup by the pydantic settings validator in
:mod:`rag_system.config` (R10.6), not by this sampler.
"""

from __future__ import annotations

import random


class TraceSampler:
    """Decides whether a given trace is recorded, based on configuration.

    The decision is made once per trace at creation time. Deterministic hashing
    on the ``trace_id`` is unnecessary because no repeated decision is made for
    the same trace.
    """

    def __init__(self, enabled: bool, sample_rate: float) -> None:
        self._enabled = enabled
        self._sample_rate = sample_rate

    def should_record(self, *, trace_id: str | None, has_trace_header: bool) -> bool:
        """Return whether the trace identified by *trace_id* should be recorded.

        - R10.1/R10.8: when disabled, always return ``False`` and ignore the
          ``X-Trace-Id`` header.
        - R10.7: when enabled and a trace header is present, force-sample and
          always return ``True``.
        - R10.4/R10.5: otherwise record with probability ``sample_rate``
          (default 1.0).
        """
        if not self._enabled:
            return False
        if has_trace_header:
            return True
        return random.random() < self._sample_rate
