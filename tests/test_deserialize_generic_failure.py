"""Unit tests for the generic deserialization-failure path (Task 7.6).

These tests target :meth:`TraceSerializer.deserialize` and the generic branch of
:class:`TraceDeserializationError` described by R6.5:

    IF the Trace_Serializer cannot determine the affected trace_id or the
    specific malformation reason, THEN THE Trace_Serializer SHALL return a
    generic failure error response that indicates deserialization failed.

In the implementation this corresponds to the outer ``except Exception`` handler
in ``deserialize`` raising ``TraceDeserializationError(reason=None, trace_id=None)``
when an *unexpected* error surfaces before ``trace_id`` could be captured. To
reach that branch the input must:

  * pass the ``isinstance(stored, Mapping)`` guard (so it is not the specific
    "not a mapping" malformation, which carries a concrete reason), and
  * raise an unexpected (non-``TraceDeserializationError``) error when the
    serializer first reaches into it via ``.get("trace_id")``,

leaving ``trace_id`` undetermined (``None``) and the reason undetermined
(``None``). The error message then falls back to the generic
``"deserialization failed"`` text.

**Validates: Requirements 6.5**
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import pytest

from rag_system.observability_tracing.serializer import (
    TraceDeserializationError,
    TraceSerializer,
)


class _ExplodingMapping(Mapping):
    """A genuine ``Mapping`` whose item access raises an unexpected error.

    It satisfies ``isinstance(obj, Mapping)`` (so it clears the serializer's
    "not a mapping" guard), but any attempt to read a key — including the
    ``Mapping.get`` mixin used by ``deserialize`` for ``trace_id`` — raises a
    plain ``RuntimeError``. That error is neither a ``TraceDeserializationError``
    nor does it let the serializer determine a trace_id, so it drives the generic
    failure branch.
    """

    def __getitem__(self, key: Any) -> Any:  # pragma: no cover - always raises
        raise RuntimeError("backing store unavailable")

    def __iter__(self) -> Iterator[Any]:
        return iter(())

    def __len__(self) -> int:
        return 0


def test_generic_failure_raises_with_undetermined_reason_and_trace_id() -> None:
    """The generic branch raises with both reason and trace_id as ``None``.

    Because the input is a Mapping but blows up on the very first field access,
    neither the malformation reason nor the affected trace_id can be determined,
    so deserialization must surface the generic failure (R6.5).
    """
    serializer = TraceSerializer()

    with pytest.raises(TraceDeserializationError) as exc_info:
        serializer.deserialize(_ExplodingMapping())

    error = exc_info.value
    assert error.reason is None
    assert error.trace_id is None


def test_generic_failure_message_indicates_deserialization_failed() -> None:
    """The generic error message falls back to the generic 'deserialization failed' text.

    With reason and trace_id both undetermined, ``str(error)`` must still read
    sensibly and indicate a generic deserialization failure rather than a
    specific malformation (R6.5).
    """
    serializer = TraceSerializer()

    with pytest.raises(TraceDeserializationError) as exc_info:
        serializer.deserialize(_ExplodingMapping())

    message = str(exc_info.value)
    assert "deserialization failed" in message
    # trace_id is undetermined, so it renders as the repr of None.
    assert "None" in message
    assert message == "failed to deserialize trace None: deserialization failed"


def test_generic_failure_preserves_underlying_cause() -> None:
    """The unexpected underlying error is chained as the exception cause.

    The serializer raises the generic ``TraceDeserializationError`` ``from`` the
    original unexpected error, preserving it for diagnostics without leaking a
    misleading specific reason into the public error contract.
    """
    serializer = TraceSerializer()

    with pytest.raises(TraceDeserializationError) as exc_info:
        serializer.deserialize(_ExplodingMapping())

    cause = exc_info.value.__cause__
    assert isinstance(cause, RuntimeError)
    assert str(cause) == "backing store unavailable"


def test_non_mapping_is_specific_not_generic() -> None:
    """A plain non-mapping is a *specific* malformation, not the generic branch.

    This guards the boundary of R6.5: when the serializer *can* determine a
    reason (here, that the stored value is not a mapping), it must report that
    concrete reason rather than collapsing into the generic failure. ``trace_id``
    is still undetermined (``None``) because nothing could be read from the input.
    """
    serializer = TraceSerializer()

    with pytest.raises(TraceDeserializationError) as exc_info:
        serializer.deserialize("not-a-mapping")  # type: ignore[arg-type]

    error = exc_info.value
    assert error.trace_id is None
    assert error.reason == "stored trace representation is not a mapping"
    # Distinct from the generic message.
    assert "deserialization failed" not in str(error)
