"""Property test for Chart_Spec schema validation (task 12.2).

# Feature: sql-lab, Property 14: Chart_Spec schema validation rejects invalid or non-declarative content

Property statement:

    *For any* strictly declarative Chart_Spec — 1-3 charts, KPIs and series that
    reference column names with an operation drawn only from the bounded allowed
    set (``sum``, ``count``, ``avg``, ``min``, ``max``), chart types drawn only
    from ``bar``/``line``/``pie``, safe free text, and an optional insight of at
    most :data:`MAX_INSIGHT_LENGTH` characters — :func:`validate_chart_spec`
    accepts it and returns a :class:`ChartSpec`. *For any* spec that violates the
    declarative contract in one of the rejection categories — an extra/unknown
    field, an ``op``/``type`` outside its bounded set, a precomputed numeric
    value where a string is expected, HTML/JavaScript/executable content in a
    text field, an out-of-range chart count (0 or 4+), or an over-long insight —
    :func:`validate_chart_spec` raises :class:`ChartSpecValidationError`.

The valid generator builds only declarative specs, so acceptance is exercised
across the whole allowed input space. The invalid generator starts from a valid
spec and mutates exactly one rejection category, so each category is exercised
in isolation and the *only* reason for rejection is the injected violation.

**Validates: Requirements 9.5, 9.6, 9.7, 10.2**
"""

from __future__ import annotations

import copy
import string

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from rag_system.sql_lab.chart_spec import (
    MAX_CHARTS,
    MAX_INSIGHT_LENGTH,
    MIN_CHARTS,
    ChartSpec,
    ChartSpecValidationError,
    validate_chart_spec,
)

# ---------------------------------------------------------------------------
# Bounded declarative vocabulary (mirrors chart_spec.py)
# ---------------------------------------------------------------------------

_ALLOWED_OPS = ["sum", "count", "avg", "min", "max"]
_CHART_TYPES = ["bar", "line", "pie"]

# Safe free text: letters, digits, spaces and a few punctuation marks that can
# never form an HTML tag (``<x``), an entity (``&...;``), an event handler
# (``on...=``), or a ``javascript:`` / ``data:text/html`` marker. This keeps
# every generated valid spec strictly declarative.
_SAFE_ALPHABET = string.ascii_letters + string.digits + " _-.,()#/"
_safe_text = st.text(alphabet=_SAFE_ALPHABET, min_size=1, max_size=20)

# Strings that carry HTML tags or executable markers the validator must reject.
_UNSAFE_TEXTS = [
    "<script>alert(1)</script>",
    "<div>hi</div>",
    "<b>bold</b>",
    "javascript:alert(1)",
    "data:text/html,<b>x</b>",
    "click <a href=x>here</a>",
    "img onerror=alert(1)",
    "onclick=steal()",
    "&lt;script&gt;",
    "<!-- comment -->",
]

# Values outside the bounded op / type literal sets.
_BAD_OPS = ["median", "stddev", "total", "SUM", "", "product"]
_BAD_TYPES = ["scatter", "area", "donut", "BAR", "", "table"]


# ---------------------------------------------------------------------------
# Strategies for a valid declarative Chart_Spec (as a raw dict)
# ---------------------------------------------------------------------------

_kpi = st.fixed_dictionaries(
    {
        "label": _safe_text,
        "op": st.sampled_from(_ALLOWED_OPS),
        "column": _safe_text,
    }
)

_series = st.fixed_dictionaries(
    {
        "column": _safe_text,
        "op": st.sampled_from(_ALLOWED_OPS),
    }
)

_chart = st.fixed_dictionaries(
    {
        "type": st.sampled_from(_CHART_TYPES),
        "title": _safe_text,
        "xColumn": _safe_text,
        "series": st.lists(_series, min_size=1, max_size=3),
    }
)


@st.composite
def _valid_spec(draw: st.DrawFn) -> dict[str, object]:
    """Build a strictly declarative, schema-valid Chart_Spec dict."""
    kpis = draw(st.lists(_kpi, min_size=0, max_size=3))
    charts = draw(st.lists(_chart, min_size=MIN_CHARTS, max_size=MAX_CHARTS))
    spec: dict[str, object] = {"kpis": kpis, "charts": charts}
    insight = draw(
        st.one_of(
            st.none(),
            st.text(alphabet=_SAFE_ALPHABET, min_size=0, max_size=MAX_INSIGHT_LENGTH),
        )
    )
    if insight is not None:
        spec["insight"] = insight
    return spec


# ---------------------------------------------------------------------------
# Strategy for an invalid spec: mutate exactly one rejection category
# ---------------------------------------------------------------------------

_REJECTION_CATEGORIES = [
    "extra_field",
    "bad_op",
    "bad_type",
    "numeric_where_string",
    "unsafe_text",
    "chart_count",
    "long_insight",
]


@st.composite
def _invalid_spec(draw: st.DrawFn) -> tuple[str, dict[str, object]]:
    """Return (category, spec) where ``spec`` violates exactly one rule."""
    spec = draw(_valid_spec())
    category = draw(st.sampled_from(_REJECTION_CATEGORIES))

    if category == "extra_field":
        # An unknown field anywhere the model forbids extras (top level or a
        # nested declarative object) must be rejected (R9.7).
        target = draw(st.sampled_from(["top", "chart", "series"]))
        if target == "top":
            spec["unexpected"] = draw(_safe_text)
        elif target == "chart":
            spec["charts"][0]["surprise"] = draw(_safe_text)  # type: ignore[index]
        else:
            spec["charts"][0]["series"][0]["surprise"] = draw(_safe_text)  # type: ignore[index]

    elif category == "bad_op":
        # An operation outside the bounded allowed set (R10.2). Corrupt either a
        # series op (always present) or a KPI op when one exists.
        bad_op = draw(st.sampled_from(_BAD_OPS))
        if spec["kpis"] and draw(st.booleans()):  # type: ignore[truthy-iterable]
            spec["kpis"][0]["op"] = bad_op  # type: ignore[index]
        else:
            spec["charts"][0]["series"][0]["op"] = bad_op  # type: ignore[index]

    elif category == "bad_type":
        # A chart type outside bar/line/pie.
        spec["charts"][0]["type"] = draw(st.sampled_from(_BAD_TYPES))  # type: ignore[index]

    elif category == "numeric_where_string":
        # A precomputed numeric value where a string is expected — the schema has
        # no numeric fields, so this must fail type validation (R10.2).
        number = draw(
            st.one_of(st.integers(), st.floats(allow_nan=False, allow_infinity=False))
        )
        target = draw(st.sampled_from(["title", "xColumn", "column"]))
        if target == "column":
            spec["charts"][0]["series"][0]["column"] = number  # type: ignore[index]
        else:
            spec["charts"][0][target] = number  # type: ignore[index]

    elif category == "unsafe_text":
        # HTML / JavaScript / executable content in a free-text field (R9.7).
        unsafe = draw(st.sampled_from(_UNSAFE_TEXTS))
        target = draw(st.sampled_from(["title", "xColumn", "series_column", "insight"]))
        if target == "title":
            spec["charts"][0]["title"] = unsafe  # type: ignore[index]
        elif target == "xColumn":
            spec["charts"][0]["xColumn"] = unsafe  # type: ignore[index]
        elif target == "series_column":
            spec["charts"][0]["series"][0]["column"] = unsafe  # type: ignore[index]
        else:
            spec["insight"] = unsafe

    elif category == "chart_count":
        # Out-of-range chart cardinality: 0 charts or MAX_CHARTS + 1 or more.
        if draw(st.booleans()):
            spec["charts"] = []
        else:
            extra = draw(st.integers(min_value=1, max_value=3))
            spec["charts"] = draw(
                st.lists(_chart, min_size=MAX_CHARTS + extra, max_size=MAX_CHARTS + extra)
            )

    else:  # long_insight
        # An insight longer than MAX_INSIGHT_LENGTH characters (R10.5 bound).
        overflow = draw(st.integers(min_value=1, max_value=300))
        spec["insight"] = "a" * (MAX_INSIGHT_LENGTH + overflow)

    return category, spec


# ---------------------------------------------------------------------------
# Property test (a): valid declarative specs are accepted
# ---------------------------------------------------------------------------


# Feature: sql-lab, Property 14: Chart_Spec schema validation rejects invalid or non-declarative content
# Validates: Requirements 9.5, 9.6, 9.7, 10.2
@settings(max_examples=300)
@given(spec=_valid_spec())
@example(spec={"kpis": [], "charts": [{"type": "bar", "title": "t", "xColumn": "x", "series": [{"column": "c", "op": "sum"}]}]})
@example(
    spec={
        "kpis": [{"label": "Total", "op": "count", "column": "id"}],
        "charts": [
            {"type": "line", "title": "Trend", "xColumn": "day", "series": [{"column": "amt", "op": "avg"}]},
            {"type": "pie", "title": "Split", "xColumn": "kind", "series": [{"column": "n", "op": "max"}]},
        ],
        "insight": "a" * MAX_INSIGHT_LENGTH,
    }
)
def test_valid_declarative_chart_spec_is_accepted(spec: dict[str, object]) -> None:
    """A strictly declarative Chart_Spec passes validation and round-trips."""
    result = validate_chart_spec(spec)

    assert isinstance(result, ChartSpec)
    assert MIN_CHARTS <= len(result.charts) <= MAX_CHARTS
    for kpi in result.kpis:
        assert kpi.op in _ALLOWED_OPS
    for chart in result.charts:
        assert chart.type in _CHART_TYPES
        assert len(chart.series) >= 1
        for series in chart.series:
            assert series.op in _ALLOWED_OPS
    if result.insight is not None:
        assert len(result.insight) <= MAX_INSIGHT_LENGTH


# ---------------------------------------------------------------------------
# Property test (b): non-declarative / invalid specs are rejected
# ---------------------------------------------------------------------------


# Feature: sql-lab, Property 14: Chart_Spec schema validation rejects invalid or non-declarative content
# Validates: Requirements 9.5, 9.6, 9.7, 10.2
@settings(max_examples=400)
@given(case=_invalid_spec())
@example(case=("extra_field", {"kpis": [], "charts": [{"type": "bar", "title": "t", "xColumn": "x", "series": [{"column": "c", "op": "sum"}]}], "evil": "x"}))
@example(case=("bad_op", {"kpis": [], "charts": [{"type": "bar", "title": "t", "xColumn": "x", "series": [{"column": "c", "op": "median"}]}]}))
@example(case=("bad_type", {"kpis": [], "charts": [{"type": "scatter", "title": "t", "xColumn": "x", "series": [{"column": "c", "op": "sum"}]}]}))
@example(case=("numeric_where_string", {"kpis": [], "charts": [{"type": "bar", "title": 42, "xColumn": "x", "series": [{"column": "c", "op": "sum"}]}]}))
@example(case=("unsafe_text", {"kpis": [], "charts": [{"type": "bar", "title": "<script>alert(1)</script>", "xColumn": "x", "series": [{"column": "c", "op": "sum"}]}]}))
@example(case=("chart_count", {"kpis": [], "charts": []}))
@example(case=("long_insight", {"kpis": [], "charts": [{"type": "bar", "title": "t", "xColumn": "x", "series": [{"column": "c", "op": "sum"}]}], "insight": "a" * (MAX_INSIGHT_LENGTH + 1)}))
def test_invalid_or_non_declarative_chart_spec_is_rejected(
    case: tuple[str, dict[str, object]],
) -> None:
    """A spec violating one rejection category raises ChartSpecValidationError."""
    _category, spec = case

    # Guard the invariant of this test: the base spec (before mutation) is
    # validated implicitly by construction; here we assert the mutated spec is
    # rejected. Deep-copy so the assertion cannot be confused by aliasing.
    with pytest.raises(ChartSpecValidationError):
        validate_chart_spec(copy.deepcopy(spec))
