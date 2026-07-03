"""Property-based test for corpus-listing invalid-cursor rejection (R4.6).

Feature: rag-trust-and-observability (task 8.5).

This module implements the single property named in the design for cursor
validation of :func:`rag_system.corpus.decode_cursor` / ``list_corpus``. A
pagination cursor is an opaque, HMAC-signed token; any deviation from a
genuine, correctly-signed token minted for the *current* ordering must be
rejected with :class:`~rag_system.corpus.InvalidCursorError` (stable error
``code == "invalid_cursor"``) — never trusted or silently reset (R4.6).

The property exercises four families of bad cursors over generated inputs:

* **Random / arbitrary strings** — anything not produced by ``encode_cursor``.
* **Structurally malformed tokens** — wrong number of ``.``-separated parts.
* **Truncated cursors** — a genuine cursor cut short (bytes/signature no longer
  line up).
* **Tampered-signature cursors** — a genuine payload whose signature was made
  under a different key (a forged/tampered signature).
* **Wrong-ordering cursors** — a correctly-signed cursor minted for one
  ordering, replayed against a listing requested with a different sort field or
  direction (does not identify a valid position, R4.6).

**Validates: Requirements 4.6**
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.corpus import (
    CorpusListParams,
    InvalidCursorError,
    SortDirection,
    SortField,
    decode_cursor,
    encode_cursor,
    list_corpus,
)
from rag_system.models import DocumentRecord, DocumentStatus

_SIGNING_KEY = "test-pagination-secret"
_WRONG_KEY = "a-different-signing-secret"


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

# Text that could plausibly look like a cursor (base64url-ish alphabet plus the
# ``.`` separator), so the malformed/decoder paths are exercised, not just the
# empty-string short-circuit.
_cursorish_text = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.=",
    min_size=0,
    max_size=60,
)

# Arbitrary unicode strings (including the empty string) — nothing here was
# produced by ``encode_cursor`` so nothing should ever verify.
_arbitrary_text = st.text(min_size=0, max_size=40)

# Structurally malformed tokens: a variable number of ``.``-separated chunks
# that is never exactly two, so the "must have <payload>.<signature>" shape
# check rejects them.
_malformed_shape = st.lists(
    st.text(alphabet="ABCabc012-_", min_size=0, max_size=8),
    min_size=0,
    max_size=5,
).filter(lambda parts: len(parts) != 2).map(lambda parts: ".".join(parts))


def _valid_cursor(
    sort_field: SortField,
    sort_direction: SortDirection,
    sort_value: str,
    last_id: str,
    *,
    signing_key: str = _SIGNING_KEY,
) -> str:
    return encode_cursor(
        sort_field=sort_field,
        sort_direction=sort_direction,
        sort_value=sort_value,
        last_id=last_id,
        signing_key=signing_key,
    )


_sort_fields = st.sampled_from(list(SortField))
_sort_directions = st.sampled_from(list(SortDirection))
_sort_values = st.text(alphabet="abc ABC012 ", min_size=0, max_size=10)
_ids = st.text(alphabet="abcdefghijklmnop0123456789", min_size=1, max_size=8)


def _assert_invalid_cursor(callable_, *args, **kwargs) -> None:
    """Assert the call raises ``InvalidCursorError`` with the stable code."""
    with pytest.raises(InvalidCursorError) as exc_info:
        callable_(*args, **kwargs)
    assert exc_info.value.code == "invalid_cursor"


# ---------------------------------------------------------------------------
# Property 14 — invalid cursors are rejected.
# ---------------------------------------------------------------------------


# Feature: rag-trust-and-observability, Property 14: Invalid cursor is rejected
@settings(max_examples=200)
@given(token=st.one_of(_arbitrary_text, _cursorish_text, _malformed_shape))
def test_random_and_malformed_cursors_are_rejected(token: str) -> None:
    # Nothing not minted by ``encode_cursor`` may ever be trusted: decoding must
    # raise ``invalid_cursor`` for arbitrary, cursor-shaped, and mis-shaped text.
    _assert_invalid_cursor(decode_cursor, token, _SIGNING_KEY)


# Feature: rag-trust-and-observability, Property 14: Invalid cursor is rejected
@settings(max_examples=200)
@given(
    sort_field=_sort_fields,
    sort_direction=_sort_directions,
    sort_value=_sort_values,
    last_id=_ids,
    truncate_to=st.integers(min_value=0, max_value=1000),
)
def test_truncated_cursors_are_rejected(
    sort_field: SortField,
    sort_direction: SortDirection,
    sort_value: str,
    last_id: str,
    truncate_to: int,
) -> None:
    token = _valid_cursor(sort_field, sort_direction, sort_value, last_id)
    # Cut the genuine cursor strictly short: the bytes/signature no longer align
    # so verification must fail (a full-length slice would be the valid cursor).
    cut = truncate_to % len(token)
    truncated = token[:cut]
    _assert_invalid_cursor(decode_cursor, truncated, _SIGNING_KEY)


# Feature: rag-trust-and-observability, Property 14: Invalid cursor is rejected
@settings(max_examples=200)
@given(
    sort_field=_sort_fields,
    sort_direction=_sort_directions,
    sort_value=_sort_values,
    last_id=_ids,
)
def test_tampered_signature_cursors_are_rejected(
    sort_field: SortField,
    sort_direction: SortDirection,
    sort_value: str,
    last_id: str,
) -> None:
    # A genuine payload carrying a signature forged under a different key: the
    # payload verbatim, but its signature will not verify against the real key.
    payload_part = _valid_cursor(
        sort_field, sort_direction, sort_value, last_id
    ).split(".", 1)[0]
    forged_sig_part = _valid_cursor(
        sort_field, sort_direction, sort_value, last_id, signing_key=_WRONG_KEY
    ).split(".", 1)[1]
    forged = f"{payload_part}.{forged_sig_part}"
    _assert_invalid_cursor(decode_cursor, forged, _SIGNING_KEY)


# Feature: rag-trust-and-observability, Property 14: Invalid cursor is rejected
@settings(max_examples=200)
@given(
    sort_value=_sort_values,
    last_id=_ids,
    minted_field=_sort_fields,
    minted_direction=_sort_directions,
    requested_field=_sort_fields,
    requested_direction=_sort_directions,
)
def test_wrong_ordering_cursor_is_rejected(
    sort_value: str,
    last_id: str,
    minted_field: SortField,
    minted_direction: SortDirection,
    requested_field: SortField,
    requested_direction: SortDirection,
) -> None:
    # A correctly-signed cursor minted for one ordering, replayed against a
    # listing requested with a *different* ordering, does not identify a valid
    # position and must be rejected (R4.6).
    if (minted_field, minted_direction) == (requested_field, requested_direction):
        return  # same ordering is a valid replay, not a wrong-ordering case
    token = _valid_cursor(minted_field, minted_direction, sort_value, last_id)
    docs = [
        DocumentRecord(
            id="doc-1",
            title="alpha",
            version="v1",
            s3_uri="s3://bucket/doc-1",
            status=DocumentStatus.indexed,
            owner="alice",
            active_version="v1",
        )
    ]
    params = CorpusListParams(
        sort_field=requested_field,
        sort_direction=requested_direction,
        cursor=token,
    )
    _assert_invalid_cursor(
        list_corpus,
        docs,
        viewer_identity=None,
        is_operator=True,
        params=params,
        pagination_signing_key=_SIGNING_KEY,
        corpus_page_size=10,
    )
