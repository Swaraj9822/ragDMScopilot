"""Chart_Spec analyzer for the SQL Lab AI auto-dashboard (Slice 4, task 12.4).

:class:`ChartSpecAnalyzer` turns a Result_Set into a validated, strictly
declarative :class:`~rag_system.sql_lab.chart_spec.ChartSpec` by asking Gemini
for schema-constrained structured output. It is deliberately thin and enforces
the anti-hallucination / data-minimization requirements of R9:

* **Bounded sample only (R9.2, R9.3).** The payload handed to the model contains
  *only* the column names, an inferred type per column, the total row count, and
  a sample of **at most 20 rows** (all rows when the Result_Set has fewer than
  20). The full Result_Set beyond that 20-row sample is never sent.
* **Structured output + validation (R9.4, R9.5).** Generation is constrained to
  the declarative Chart_Spec schema by the dedicated
  :class:`~rag_system.sql_lab.gemini_client.SqlLabGeminiClient`; the returned
  text is re-validated with :func:`~rag_system.sql_lab.chart_spec.validate_chart_spec`
  before it is returned. A validation failure surfaces as
  :class:`~rag_system.sql_lab.chart_spec.ChartSpecValidationError` (a
  :class:`SqlLabError`), which the route maps to R9.6.
* **Model selection (R9.8, R9.9).** ``mode="default"`` uses the Gemini Flash
  model id and ``mode="deep"`` uses the Gemini Pro model id; the mapping lives in
  the injected client, which reads it from ``Settings``.
* **60s budget (R9.10).** The 60-second budget is enforced at the client's
  ``HttpOptions`` level. Any transport/timeout/unavailability failure from the
  client is mapped to :class:`~rag_system.sql_lab.errors.SqlLabAnalysisError`, so
  the route can report that analysis could not be completed while leaving the
  source Result_Set unchanged.

The Gemini client is injected (defaulting to one built from ``Settings``) so the
analyzer is unit-testable without the ``google-genai`` dependency or a live
model.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from rag_system.config import Settings
from rag_system.sql_lab.chart_spec import ChartSpec, validate_chart_spec
from rag_system.sql_lab.errors import SqlLabAnalysisError
from rag_system.sql_lab.gemini_client import AnalysisMode, SqlLabGeminiClient

#: The maximum number of sample rows sent to the language model (R9.2/R9.3).
#: All rows are sent when the Result_Set contains fewer than this many.
MAX_SAMPLE_ROWS = 20


def _extract_result_set(
    result: Any,
) -> tuple[list[str], list[dict[str, Any]], int]:
    """Coerce a Result_Set into ``(columns, rows, row_count)``.

    Accepts either the backend :class:`~rag_system.sql_lab.service.SqlRunResult`
    dataclass (attributes ``columns``/``rows``/``row_count``) or a plain mapping
    using the camelCase JSON contract the frontend posts (``columns``, ``rows``,
    ``rowCount``). ``row_count`` falls back to ``len(rows)`` when absent so the
    reported total is always well defined.
    """

    def _get(name: str, *aliases: str) -> Any:
        if isinstance(result, Mapping):
            for key in (name, *aliases):
                if key in result:
                    return result[key]
            return None
        for key in (name, *aliases):
            if hasattr(result, key):
                return getattr(result, key)
        return None

    raw_columns = _get("columns")
    raw_rows = _get("rows")
    raw_row_count = _get("row_count", "rowCount")

    columns: list[str] = [str(c) for c in raw_columns] if raw_columns else []
    rows: list[dict[str, Any]] = list(raw_rows) if raw_rows else []
    row_count = int(raw_row_count) if isinstance(raw_row_count, int) else len(rows)
    return columns, rows, row_count


def _infer_column_type(values: Sequence[Any]) -> str:
    """Infer a coarse type label for a column from its sampled values.

    Scans the non-null sampled values and returns one of ``"integer"``,
    ``"number"``, ``"boolean"``, ``"string"``, or ``"unknown"`` (when every
    sampled value is null/absent). ``bool`` is checked before ``int`` because
    ``bool`` is a subclass of ``int`` in Python. Mixed numeric/other values fall
    back to ``"string"`` so the label never overstates the column's shape.
    """
    seen: set[str] = set()
    for value in values:
        if value is None:
            continue
        if isinstance(value, bool):
            seen.add("boolean")
        elif isinstance(value, int):
            seen.add("integer")
        elif isinstance(value, float):
            seen.add("number")
        else:
            seen.add("string")

    if not seen:
        return "unknown"
    if seen == {"integer"}:
        return "integer"
    if seen == {"number"} or seen == {"integer", "number"}:
        return "number"
    if seen == {"boolean"}:
        return "boolean"
    return "string"


class ChartSpecAnalyzer:
    """Build a compact analysis payload and return a validated ``ChartSpec``.

    ``client`` may be injected (primarily for testing); by default a
    :class:`~rag_system.sql_lab.gemini_client.SqlLabGeminiClient` is built from
    ``settings``.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        client: SqlLabGeminiClient | None = None,
    ) -> None:
        self._settings = settings
        self._client = client or SqlLabGeminiClient(settings)

    def analyze(self, result: Any, mode: AnalysisMode = "default") -> ChartSpec:
        """Analyze ``result`` and return a validated declarative Chart_Spec.

        Builds the bounded payload (column names, inferred types, total row
        count, ≤ 20-row sample — all rows when fewer than 20; R9.2/R9.3),
        requests schema-constrained structured output from the injected client
        selecting Flash for ``"default"`` and Pro for ``"deep"`` (R9.8/R9.9),
        then validates the returned text against the strict Chart_Spec schema
        before returning it (R9.5).

        Raises :class:`~rag_system.sql_lab.errors.SqlLabAnalysisError` if the
        model is unavailable or exceeds the client's 60s budget (R9.10), and
        :class:`~rag_system.sql_lab.chart_spec.ChartSpecValidationError` if the
        model output fails schema validation (R9.6). In either failure the
        source Result_Set is left unchanged.
        """
        payload = self._build_payload(result)

        try:
            raw = self._client.generate_chart_spec_json(payload, mode)
        except Exception as exc:  # noqa: BLE001 - map any client failure to R9.10
            raise SqlLabAnalysisError(
                "Analysis could not be completed: the analysis model is "
                "unavailable or did not respond within the allotted time."
            ) from exc

        # Schema validation (R9.5). ChartSpecValidationError propagates for the
        # route to map to R9.6, leaving the source Result_Set unchanged.
        return validate_chart_spec(raw)

    def _build_payload(self, result: Any) -> str:
        """Assemble the compact, bounded prompt payload sent to the model.

        The payload embeds only the column names, an inferred type per column,
        the total row count, and a sample of at most :data:`MAX_SAMPLE_ROWS`
        rows (all rows when fewer). The full Result_Set beyond that sample is
        never included (R9.2/R9.3).
        """
        columns, rows, row_count = _extract_result_set(result)

        # Bounded sample: all rows when fewer than the cap, else the first cap.
        sample = rows[:MAX_SAMPLE_ROWS]

        # Infer a coarse type per column from the sampled values only.
        column_types = {
            column: _infer_column_type([row.get(column) for row in sample])
            for column in columns
        }

        # Project each sample row down to the known columns so no extra fields
        # leak into the payload.
        sample_rows = [
            {column: row.get(column) for column in columns} for row in sample
        ]

        payload = {
            "columns": columns,
            "columnTypes": column_types,
            "rowCount": row_count,
            "sampleRowCount": len(sample_rows),
            "sampleRows": sample_rows,
        }

        instructions = (
            "You are given a compact summary of a read-only SQL query result "
            "set: the column names, an inferred type per column, the total row "
            "count, and a small sample of rows (at most 20). Produce a "
            "declarative auto-dashboard specification that names columns and "
            "chooses aggregation operations from the allowed set "
            "(sum, count, avg, min, max, plus group-by via a chart's xColumn). "
            "Reference only column names that appear in `columns`. Do NOT emit "
            "any precomputed numeric values, HTML, JavaScript, or executable "
            "content — only column names and operations. The dashboard computes "
            "every displayed number locally from the actual rows."
        )

        return (
            f"{instructions}\n\nRESULT_SET_SUMMARY:\n"
            f"{json.dumps(payload, default=str, ensure_ascii=False)}"
        )


__all__ = [
    "MAX_SAMPLE_ROWS",
    "ChartSpecAnalyzer",
]
