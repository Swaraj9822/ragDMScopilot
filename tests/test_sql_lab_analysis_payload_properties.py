"""Property test for the SQL Lab analysis payload (task 12.5).

Feature: sql-lab (Slice 4 — AI auto-dashboard analysis).

# Feature: sql-lab, Property 13: The analysis payload sends only a bounded sample

Property statement:

    *For any* Result_Set, the payload the analyzer sends to the model includes
    only column names, inferred types, the row count, and a sample of at most 20
    rows (all rows when fewer than 20), and never the full result set beyond the
    20-row sample.

:class:`~rag_system.sql_lab.analyzer.ChartSpecAnalyzer` builds the payload via
``_build_payload`` and hands it to
``client.generate_chart_spec_json(payload, mode)``. This test injects a fake
``SqlLabGeminiClient``-like client that

* **captures** the exact payload passed to ``generate_chart_spec_json`` (so the
  data-minimization contract can be inspected without a live model), and
* returns a canned, schema-**valid** Chart_Spec JSON string, so
  :func:`~rag_system.sql_lab.chart_spec.validate_chart_spec` succeeds and
  :meth:`ChartSpecAnalyzer.analyze` completes end-to-end.

The generator produces Result_Sets (as the :class:`SqlRunResult` dataclass and
as the frontend camelCase dict) with varying row counts — including fewer than
20, exactly 20, and more than 20 — so the ≤ 20-row bound is exercised across the
boundary. The captured payload is parsed back from its JSON summary and asserted
to expose only the allowed keys, echo the column names / inferred types / row
count, and carry a bounded sample equal to ``rows[:20]`` — never the full set
beyond that sample.

**Validates: Requirements 9.2, 9.3**
"""

from __future__ import annotations

import json
from typing import Any

from hypothesis import example, given, settings
from hypothesis import strategies as st

from rag_system.config import Settings
from rag_system.sql_lab.analyzer import MAX_SAMPLE_ROWS, ChartSpecAnalyzer
from rag_system.sql_lab.chart_spec import ChartSpec
from rag_system.sql_lab.service import SqlRunResult

# Required Settings supplied by alias so the model builds in isolation
# (mirrors the convention in the sibling SQL Lab property tests).
_REQUIRED_BY_ALIAS = {
    "RAG_GCS_BUCKET": "test-bucket",
    "LLAMA_CLOUD_API_KEY": "test-llama-key",
    "PINECONE_API_KEY": "test-pinecone-key",
    "PINECONE_INDEX_NAME": "test-index",
}

# The keys the compact payload is permitted to carry. Anything else would leak
# more than "column names, inferred types, the row count, and a sample".
_ALLOWED_PAYLOAD_KEYS = {
    "columns",
    "columnTypes",
    "rowCount",
    "sampleRowCount",
    "sampleRows",
}

# The coarse type labels ``_infer_column_type`` may emit.
_TYPE_LABELS = {"integer", "number", "boolean", "string", "unknown"}

# A separator marking the start of the JSON summary inside the prompt payload.
_SUMMARY_MARKER = "RESULT_SET_SUMMARY:\n"

# A canned, schema-valid Chart_Spec so ``validate_chart_spec`` succeeds and
# ``analyze`` returns without touching a real model.
_CANNED_CHART_SPEC_JSON = json.dumps(
    {
        "kpis": [],
        "charts": [
            {
                "type": "bar",
                "title": "Rows",
                "xColumn": "x",
                "series": [{"column": "c", "op": "count"}],
            }
        ],
    }
)


class _CapturingClient:
    """Fake ``SqlLabGeminiClient`` that captures the payload and returns a spec.

    Records every ``contents`` payload (and ``mode``) handed to
    ``generate_chart_spec_json`` so the test can inspect exactly what the
    analyzer would send to the model, then returns a canned schema-valid
    Chart_Spec JSON string so the analyzer's validation step succeeds.
    """

    def __init__(self, canned_json: str) -> None:
        self._canned_json = canned_json
        self.payloads: list[Any] = []
        self.modes: list[str] = []

    def generate_chart_spec_json(self, contents: Any, mode: str = "default") -> str:
        self.payloads.append(contents)
        self.modes.append(mode)
        return self._canned_json


def _build_settings() -> Settings:
    """Construct ``Settings`` in isolation (the analyzer only stores it)."""
    return Settings(**_REQUIRED_BY_ALIAS)  # type: ignore[arg-type]


def _parse_payload_summary(payload: Any) -> dict[str, Any]:
    """Extract and JSON-decode the RESULT_SET_SUMMARY object from the payload."""
    assert isinstance(payload, str), "payload sent to the model must be a string"
    assert _SUMMARY_MARKER in payload, "payload must embed the result-set summary"
    _prefix, _, summary = payload.partition(_SUMMARY_MARKER)
    return json.loads(summary)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Distinct, select-order column names (may be empty for a value-less projection).
_columns = st.lists(
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=8),
    min_size=0,
    max_size=5,
    unique=True,
)

# Cell values covering the coarse type space the analyzer infers. NaN/inf are
# excluded so the JSON summary round-trips to equal values.
_cell = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-1_000_000, max_value=1_000_000),
    st.floats(allow_nan=False, allow_infinity=False, width=32),
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz 0123456789_-", min_size=0, max_size=10),
)


@st.composite
def _result_case(draw: st.DrawFn) -> dict[str, Any]:
    """Generate a Result_Set (dataclass or dict) with a varied row count."""
    columns = draw(_columns)
    # Row counts spanning below, at, and above the 20-row sample bound.
    n_rows = draw(st.integers(min_value=0, max_value=40))
    rows = [
        {column: draw(_cell) for column in columns} for _ in range(n_rows)
    ]
    row_count = len(rows)

    as_dataclass = draw(st.booleans())
    if as_dataclass:
        result: Any = SqlRunResult(
            columns=list(columns),
            rows=rows,
            row_count=row_count,
            duration_ms=draw(st.integers(min_value=0, max_value=10_000)),
            sql="SELECT * FROM t",
            truncated=False,
        )
    else:
        result = {
            "columns": list(columns),
            "rows": rows,
            "rowCount": row_count,
            "durationMs": draw(st.integers(min_value=0, max_value=10_000)),
            "sql": "SELECT * FROM t",
            "truncated": False,
        }

    return {"result": result, "columns": columns, "rows": rows, "row_count": row_count}


# ---------------------------------------------------------------------------
# Property test
# ---------------------------------------------------------------------------


# Feature: sql-lab, Property 13: The analysis payload sends only a bounded sample
# Validates: Requirements 9.2, 9.3
@settings(max_examples=300)
@given(case=_result_case(), mode=st.sampled_from(["default", "deep"]))
# Fewer than 20 rows: the whole set is sampled.
@example(
    case={
        "result": {"columns": ["a"], "rows": [{"a": i} for i in range(5)], "rowCount": 5},
        "columns": ["a"],
        "rows": [{"a": i} for i in range(5)],
        "row_count": 5,
    },
    mode="default",
)
# Exactly 20 rows: the boundary — all 20 are sampled.
@example(
    case={
        "result": {"columns": ["a"], "rows": [{"a": i} for i in range(20)], "rowCount": 20},
        "columns": ["a"],
        "rows": [{"a": i} for i in range(20)],
        "row_count": 20,
    },
    mode="deep",
)
# More than 20 rows: the sample is capped at 20 and never the full set.
@example(
    case={
        "result": {"columns": ["a"], "rows": [{"a": i} for i in range(40)], "rowCount": 40},
        "columns": ["a"],
        "rows": [{"a": i} for i in range(40)],
        "row_count": 40,
    },
    mode="default",
)
def test_analysis_payload_sends_only_a_bounded_sample(
    case: dict[str, Any], mode: str
) -> None:
    """The captured payload carries only a bounded, minimal Result_Set summary."""
    columns: list[str] = case["columns"]
    rows: list[dict[str, Any]] = case["rows"]
    row_count: int = case["row_count"]

    client = _CapturingClient(_CANNED_CHART_SPEC_JSON)
    analyzer = ChartSpecAnalyzer(_build_settings(), client=client)

    spec = analyzer.analyze(case["result"], mode)  # type: ignore[arg-type]

    # The flow completes: the canned spec validates and one call was made with
    # the requested mode.
    assert isinstance(spec, ChartSpec)
    assert len(client.payloads) == 1
    assert client.modes == [mode]

    summary = _parse_payload_summary(client.payloads[0])

    # (a) The payload exposes ONLY the allowed keys — nothing beyond column
    #     names, inferred types, the row count, and the sample (R9.2, R9.3).
    assert set(summary.keys()) == _ALLOWED_PAYLOAD_KEYS

    # (b) Column names are echoed verbatim in select order.
    assert summary["columns"] == columns

    # (c) An inferred type label is present for every column and nothing else.
    assert set(summary["columnTypes"].keys()) == set(columns)
    assert all(label in _TYPE_LABELS for label in summary["columnTypes"].values())

    # (d) The row count reported is the Result_Set's total row count.
    assert summary["rowCount"] == row_count

    # (e) The sample holds at most 20 rows — all rows when fewer than 20.
    expected_sample_size = min(len(rows), MAX_SAMPLE_ROWS)
    assert summary["sampleRowCount"] == expected_sample_size
    assert len(summary["sampleRows"]) == expected_sample_size
    assert len(summary["sampleRows"]) <= MAX_SAMPLE_ROWS

    # (f) The sample is exactly the first ≤20 rows projected onto the known
    #     columns — never the full result set beyond the 20-row sample (R9.3).
    expected_sample = [
        {column: row.get(column) for column in columns}
        for row in rows[:MAX_SAMPLE_ROWS]
    ]
    assert summary["sampleRows"] == expected_sample

    # (g) When the Result_Set has more than 20 rows, the sample is strictly
    #     smaller than the full set: the excess rows are never sent.
    if len(rows) > MAX_SAMPLE_ROWS:
        assert len(summary["sampleRows"]) == MAX_SAMPLE_ROWS
        assert len(summary["sampleRows"]) < len(rows)
