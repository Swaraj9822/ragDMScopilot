"""Declarative Chart_Spec schema, validator, and Gemini response schema (Slice 4).

The Chart_Spec is the schema-constrained JSON object the AI auto-dashboard
produces (R9.4). It carries **declarative aggregation instructions only** — the
language model names columns present in the Result_Set and picks an operation
from a bounded allowed set (``sum``, ``count``, ``avg``, ``min``, ``max``, plus
group-by via a chart's ``xColumn``); it emits **no precomputed numeric values**
anywhere (R10.2). The frontend ``AutoDashboard`` computes every displayed number
locally from the actual returned rows, so the model can never fabricate a number
it never emitted.

Because the schema is strictly declarative it also contains **no HTML,
JavaScript, or executable content** (R9.7):

* Every model is ``extra="forbid"`` — unknown/extra fields are rejected outright.
* The only enumerated fields (``op``, ``type``) are constrained to their bounded
  literal sets, so an executable payload smuggled into an enum position is
  rejected.
* The free-text fields (``label``, ``title``, ``column``, ``xColumn``,
  ``insight``) are scanned and rejected if they contain HTML tags or JavaScript
  execution markers.
* There are simply no numeric fields, so any number appearing where a string or
  enum is expected fails type validation.

:func:`validate_chart_spec` parses/validates raw model output (a JSON string or
an already-decoded ``dict``) and raises :class:`ChartSpecValidationError` with a
clear message on any failure (R9.5, R9.6). :data:`CHART_SPEC_RESPONSE_SCHEMA` is
the response-schema object handed to the ``google-genai`` structured-output call
(task 12.3) to constrain generation to this shape.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from rag_system.sql_lab.errors import SqlLabError

#: The bounded set of allowed aggregation operations (R10.2). The model may pick
#: only from these; anything else fails enum validation.
AllowedOp = Literal["sum", "count", "avg", "min", "max"]

#: The bounded set of allowed chart types.
ChartType = Literal["bar", "line", "pie"]

#: Maximum length of the optional insight line (R10.5).
MAX_INSIGHT_LENGTH = 200

#: Inclusive bounds on the number of charts a Chart_Spec may declare (R10.1).
MIN_CHARTS = 1
MAX_CHARTS = 3

#: Matches HTML tags (``<div``, ``</span``, ``<!--``) and common JavaScript /
#: executable-content markers. Deliberately requires a letter, ``/`` or ``!``
#: immediately after ``<`` so ordinary comparisons in a label (``a < 5``,
#: ``x > y``) are not flagged, while ``<script>`` / ``</b>`` style markup is.
_UNSAFE_TEXT_PATTERN = re.compile(
    r"</?[a-zA-Z!]|javascript:|data:text/html|on\w+\s*=|&(?:#\d+|lt|gt|#x[0-9a-fA-F]+);",
    re.IGNORECASE,
)


class ChartSpecValidationError(SqlLabError):
    """Raised when raw model output cannot be validated as a Chart_Spec.

    The message names the failure reason (invalid JSON, wrong top-level type,
    extra fields, a value outside the bounded op/type sets, a precomputed number
    where a string is expected, unsafe HTML/JS content, or a violated
    cardinality/length bound). Subclasses :class:`SqlLabError` so the route layer
    (task 12.6) can map it to an error response and leave the source Result_Set
    unchanged (R9.6).
    """


def _reject_unsafe_text(value: str) -> str:
    """Reject any free-text string carrying HTML tags or executable markers (R9.7)."""
    if _UNSAFE_TEXT_PATTERN.search(value):
        raise ValueError(
            "must not contain HTML, JavaScript, or other executable content"
        )
    return value


class KpiSpec(BaseModel):
    """A single KPI card: a label plus a declarative ``op`` over a named column.

    Carries no numeric value — the displayed number is computed locally from the
    Result_Set rows using ``op`` over ``column`` (R10.2, R10.3).
    """

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1)
    op: AllowedOp
    column: str = Field(min_length=1)

    @field_validator("label", "column")
    @classmethod
    def _no_unsafe_text(cls, value: str) -> str:
        return _reject_unsafe_text(value)


class SeriesSpec(BaseModel):
    """One chart series: a declarative ``op`` over a named column (no numbers)."""

    model_config = ConfigDict(extra="forbid")

    column: str = Field(min_length=1)
    op: AllowedOp

    @field_validator("column")
    @classmethod
    def _no_unsafe_text(cls, value: str) -> str:
        return _reject_unsafe_text(value)


class ChartDef(BaseModel):
    """A single chart: a type, a title, a group-by ``xColumn``, and 1+ series.

    Every value the chart renders is derived locally from the Result_Set rows by
    grouping on ``xColumn`` and applying each series' declared ``op`` to its
    referenced ``column``. The Chart_Spec itself holds no numbers.
    """

    model_config = ConfigDict(extra="forbid")

    type: ChartType
    title: str = Field(min_length=1)
    xColumn: str = Field(min_length=1)  # noqa: N815 - camelCase mirrors the JSON contract
    series: list[SeriesSpec] = Field(min_length=1)

    @field_validator("title", "xColumn")
    @classmethod
    def _no_unsafe_text(cls, value: str) -> str:
        return _reject_unsafe_text(value)


class ChartSpec(BaseModel):
    """The validated, strictly declarative auto-dashboard specification.

    * ``kpis`` — zero or more KPI card definitions.
    * ``charts`` — between :data:`MIN_CHARTS` and :data:`MAX_CHARTS` charts
      inclusive (R10.1).
    * ``insight`` — an optional single insight line of at most
      :data:`MAX_INSIGHT_LENGTH` characters (R10.5).

    ``extra="forbid"`` rejects any unknown field at the top level, so nothing
    outside this declarative contract (including HTML/JS/executable payloads
    smuggled as extra keys) can survive validation (R9.7).
    """

    model_config = ConfigDict(extra="forbid")

    kpis: list[KpiSpec] = Field(default_factory=list)
    charts: list[ChartDef] = Field(min_length=MIN_CHARTS, max_length=MAX_CHARTS)
    insight: str | None = Field(default=None, max_length=MAX_INSIGHT_LENGTH)

    @field_validator("insight")
    @classmethod
    def _no_unsafe_insight(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _reject_unsafe_text(value)


#: The response-schema object passed to the ``google-genai`` structured-output
#: call as ``response_schema`` (task 12.3). ``google-genai`` accepts a pydantic
#: model class directly for structured JSON output, so this is simply the
#: :class:`ChartSpec` model — keeping the generation-time schema and the
#: validation-time schema in perfect lockstep. The returned text is still
#: re-validated with :func:`validate_chart_spec` before it is trusted (R9.5).
CHART_SPEC_RESPONSE_SCHEMA = ChartSpec


def validate_chart_spec(raw: str | bytes | dict[str, Any]) -> ChartSpec:
    """Parse and strictly validate raw model output as a :class:`ChartSpec`.

    Accepts either a JSON string/bytes (as returned by ``response.text``) or an
    already-decoded ``dict``. Raises :class:`ChartSpecValidationError` with a
    clear reason on any failure — invalid JSON, a non-object top level, extra
    fields, a value outside the bounded ``op``/``type`` sets, a precomputed
    number where a string is expected, unsafe HTML/JS content, or a violated
    cardinality/length bound (R9.5, R9.6, R9.7, R10.2). Never returns partially
    validated content.
    """
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            data: Any = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ChartSpecValidationError(
                f"Chart_Spec is not valid JSON: {exc}"
            ) from exc
    elif isinstance(raw, dict):
        data = raw
    else:
        raise ChartSpecValidationError(
            "Chart_Spec must be a JSON string or object, "
            f"got {type(raw).__name__}."
        )

    if not isinstance(data, dict):
        raise ChartSpecValidationError(
            "Chart_Spec must be a JSON object at the top level, "
            f"got {type(data).__name__}."
        )

    try:
        return ChartSpec.model_validate(data)
    except ValidationError as exc:
        raise ChartSpecValidationError(
            f"Chart_Spec failed schema validation: {exc}"
        ) from exc


__all__ = [
    "AllowedOp",
    "ChartType",
    "MAX_INSIGHT_LENGTH",
    "MIN_CHARTS",
    "MAX_CHARTS",
    "KpiSpec",
    "SeriesSpec",
    "ChartDef",
    "ChartSpec",
    "CHART_SPEC_RESPONSE_SCHEMA",
    "ChartSpecValidationError",
    "validate_chart_spec",
]
