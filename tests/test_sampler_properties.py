"""Property tests for the trace sampling decision.

Feature: ai-observability-platform.

These tests exercise :class:`rag_system.observability_tracing.sampler.TraceSampler`
and its single decision method ``should_record(*, trace_id, has_trace_header)``.
The sampler makes its decision once per trace, honouring three concerns:

- the global enablement flag (R10.1/R10.8): when disabled the sampler records
  nothing and ignores the ``X-Trace-Id`` header;
- the header force-sample override (R10.7): when enabled and a trace header is
  present, the trace is always recorded regardless of the configured rate;
- the configured sampling rate (R10.4): otherwise the sampler records a
  proportion of traces equal to the configured rate within +/- 5 percent
  measured over 1000 traces.
"""

import random

from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.observability_tracing.sampler import TraceSampler

# ---------------------------------------------------------------------------
# Smart generators - constrained to the sampler's input domain.
# ---------------------------------------------------------------------------

# Any real, finite sampling rate within the inclusive unit interval (R10.4).
_rates = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)

# A trace_id is an opaque correlation string, or absent. The decision does not
# depend on its value, so a small alphabet keeps generation cheap.
_trace_ids = st.one_of(
    st.none(),
    st.text(alphabet="0123456789abcdef", min_size=1, max_size=32),
)


# ---------------------------------------------------------------------------
# Property 21 - sampling decision honours enablement, header override, and rate.
# ---------------------------------------------------------------------------


# Feature: ai-observability-platform, Property 21: Sampling decision honours enablement, header override, and rate
# Validates: Requirements 10.1, 10.7
@settings(max_examples=100)
@given(rate=_rates, trace_id=_trace_ids, has_header=st.booleans())
def test_disabled_never_records_ignoring_header(
    rate: float, trace_id: str | None, has_header: bool
) -> None:
    """When disabled, the sampler never records and ignores the header (R10.1).

    For any configured rate, any trace_id, and either header state, a disabled
    sampler must always return ``False`` - the ``X-Trace-Id`` header force-sample
    override does not apply while tracing is disabled.
    """
    sampler = TraceSampler(enabled=False, sample_rate=rate)
    assert sampler.should_record(trace_id=trace_id, has_trace_header=has_header) is False


# Feature: ai-observability-platform, Property 21: Sampling decision honours enablement, header override, and rate
# Validates: Requirements 10.7
@settings(max_examples=100)
@given(rate=_rates, trace_id=_trace_ids)
def test_enabled_with_header_always_records(rate: float, trace_id: str | None) -> None:
    """When enabled and a trace header is present, always record (R10.7).

    The header force-sample override applies regardless of the configured rate,
    so the decision is ``True`` for every rate in ``[0.0, 1.0]``.
    """
    sampler = TraceSampler(enabled=True, sample_rate=rate)
    assert sampler.should_record(trace_id=trace_id, has_trace_header=True) is True


# Feature: ai-observability-platform, Property 21: Sampling decision honours enablement, header override, and rate
# Validates: Requirements 10.1, 10.4, 10.7
@settings(max_examples=100)
@given(trace_id=_trace_ids)
def test_enabled_no_header_rate_extremes_are_deterministic(
    trace_id: str | None,
) -> None:
    """Enabled with no header: rate 1.0 always records, rate 0.0 never records.

    The boundary rates of the inclusive sampling interval (R10.4) are
    deterministic and do not depend on the random draw.
    """
    always = TraceSampler(enabled=True, sample_rate=1.0)
    never = TraceSampler(enabled=True, sample_rate=0.0)
    assert always.should_record(trace_id=trace_id, has_trace_header=False) is True
    assert never.should_record(trace_id=trace_id, has_trace_header=False) is False


# A fixed seed makes the statistical assertion deterministic and reproducible.
# With 1000 draws the largest deviation between the empirical proportion and the
# configured rate (the Kolmogorov-Smirnov statistic) stays well inside the
# +/- 5 percent tolerance for this seed, so the property holds for every rate
# rather than only for a lucky random sequence.
_SAMPLE_TRIALS = 1000
_SAMPLE_SEED = 20240517


# Feature: ai-observability-platform, Property 21: Sampling decision honours enablement, header override, and rate
# Validates: Requirements 10.4
@settings(max_examples=100, deadline=None)
@given(rate=_rates)
def test_enabled_no_header_rate_proportion_within_tolerance(rate: float) -> None:
    """Enabled with no header: observed sample proportion tracks the rate (R10.4).

    Over 1000 independent decisions the recorded proportion must lie within
    +/- 5 percent (absolute) of the configured rate. Randomness is seeded with a
    fixed value so the statistical assertion is stable and reproducible across
    runs, independent of any global random state.
    """
    sampler = TraceSampler(enabled=True, sample_rate=rate)
    random.seed(_SAMPLE_SEED)
    recorded = sum(
        1
        for _ in range(_SAMPLE_TRIALS)
        if sampler.should_record(trace_id=None, has_trace_header=False)
    )
    observed = recorded / _SAMPLE_TRIALS
    assert abs(observed - rate) <= 0.05
