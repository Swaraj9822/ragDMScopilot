"""Unit tests for the corpus listing service (R4).

Feature: rag-trust-and-observability (task 8.1).

Covers owner-based role scoping (R4.2, R4.3, R4.11), signed cursor pagination
(R4.4, R4.5, R4.6), sort/filter/search (R4.7, R4.8, R4.9), owner inclusion
(R4.11), and the over-long search-term rejection (R4.14).
"""

from __future__ import annotations

import pytest

from rag_system.corpus import (
    MAX_SEARCH_TERM_LENGTH,
    CorpusListParams,
    InvalidCursorError,
    SearchTermTooLongError,
    SortDirection,
    SortField,
    decode_cursor,
    encode_cursor,
    list_corpus,
)
from rag_system.models import DocumentRecord, DocumentStatus

_SIGNING_KEY = "test-pagination-secret"


def _doc(
    doc_id: str,
    title: str,
    owner: str | None = None,
    status: DocumentStatus = DocumentStatus.indexed,
    active_version: str | None = "v1",
    created_at: str | None = None,
) -> DocumentRecord:
    rec = DocumentRecord(
        id=doc_id,
        title=title,
        version="v1",
        s3_uri=f"s3://bucket/{doc_id}",
        status=status,
        owner=owner,
        active_version=active_version,
    )
    # Attach a date the tests can sort/filter on via the ``date_of`` accessor.
    object.__setattr__(rec, "_created_at", created_at)
    return rec


def _date_of(rec: DocumentRecord) -> str | None:
    return getattr(rec, "_created_at", None)


def _list(docs, *, viewer_identity, is_operator, params, date_of=_date_of):
    return list_corpus(
        docs,
        viewer_identity=viewer_identity,
        is_operator=is_operator,
        params=params,
        pagination_signing_key=_SIGNING_KEY,
        corpus_page_size=50,
        date_of=date_of,
    )


# ---------------------------------------------------------------------------
# Role scoping (R4.2, R4.3, R4.11)
# ---------------------------------------------------------------------------


def test_operator_sees_entire_corpus():
    docs = [_doc("a", "Alpha", owner="alice"), _doc("b", "Beta", owner="bob")]
    page = _list(docs, viewer_identity="alice", is_operator=True, params=CorpusListParams())
    assert {d.id for d in page.documents} == {"a", "b"}


def test_non_operator_sees_only_owned_documents():
    docs = [
        _doc("a", "Alpha", owner="alice"),
        _doc("b", "Beta", owner="bob"),
        _doc("c", "Gamma", owner=None),  # legacy record, unowned
    ]
    page = _list(docs, viewer_identity="alice", is_operator=False, params=CorpusListParams())
    assert {d.id for d in page.documents} == {"a"}


def test_non_operator_never_sees_unowned_legacy_documents():
    docs = [_doc("c", "Gamma", owner=None)]
    page = _list(docs, viewer_identity="alice", is_operator=False, params=CorpusListParams())
    assert page.documents == []


def test_owner_included_per_document():
    # R4.11
    docs = [_doc("a", "Alpha", owner="alice")]
    page = _list(docs, viewer_identity="alice", is_operator=True, params=CorpusListParams())
    assert page.documents[0].owner == "alice"


# ---------------------------------------------------------------------------
# Sorting (R4.7)
# ---------------------------------------------------------------------------


def test_sort_by_name_ascending_case_insensitive():
    docs = [_doc("1", "banana"), _doc("2", "Apple"), _doc("3", "cherry")]
    page = _list(docs, viewer_identity=None, is_operator=True, params=CorpusListParams())
    assert [d.title for d in page.documents] == ["Apple", "banana", "cherry"]


def test_sort_by_name_descending():
    docs = [_doc("1", "banana"), _doc("2", "Apple"), _doc("3", "cherry")]
    params = CorpusListParams(sort_direction=SortDirection.desc)
    page = _list(docs, viewer_identity=None, is_operator=True, params=params)
    assert [d.title for d in page.documents] == ["cherry", "banana", "Apple"]


def test_sort_by_owner():
    docs = [_doc("1", "X", owner="carol"), _doc("2", "Y", owner="alice"), _doc("3", "Z", owner="bob")]
    params = CorpusListParams(sort_field=SortField.owner)
    page = _list(docs, viewer_identity=None, is_operator=True, params=params)
    assert [d.owner for d in page.documents] == ["alice", "bob", "carol"]


def test_sort_by_date():
    docs = [
        _doc("1", "X", created_at="2024-03-01"),
        _doc("2", "Y", created_at="2024-01-01"),
        _doc("3", "Z", created_at="2024-02-01"),
    ]
    params = CorpusListParams(sort_field=SortField.date)
    page = _list(docs, viewer_identity=None, is_operator=True, params=params)
    assert [d.id for d in page.documents] == ["2", "3", "1"]


# ---------------------------------------------------------------------------
# Filters (R4.8)
# ---------------------------------------------------------------------------


def test_filter_by_status():
    docs = [
        _doc("1", "X", status=DocumentStatus.indexed),
        _doc("2", "Y", status=DocumentStatus.failed),
    ]
    params = CorpusListParams(status=DocumentStatus.failed)
    page = _list(docs, viewer_identity=None, is_operator=True, params=params)
    assert [d.id for d in page.documents] == ["2"]


def test_filter_by_owner():
    docs = [_doc("1", "X", owner="alice"), _doc("2", "Y", owner="bob")]
    params = CorpusListParams(owner="bob")
    page = _list(docs, viewer_identity=None, is_operator=True, params=params)
    assert [d.id for d in page.documents] == ["2"]


def test_filter_by_active_version():
    docs = [_doc("1", "X", active_version="v1"), _doc("2", "Y", active_version="v2")]
    params = CorpusListParams(active_version="v2")
    page = _list(docs, viewer_identity=None, is_operator=True, params=params)
    assert [d.id for d in page.documents] == ["2"]


def test_filter_by_date_range_excludes_undated():
    docs = [
        _doc("1", "X", created_at="2024-01-15"),
        _doc("2", "Y", created_at="2024-03-15"),
        _doc("3", "Z", created_at=None),
    ]
    params = CorpusListParams(date_from="2024-01-01", date_to="2024-02-01")
    page = _list(docs, viewer_identity=None, is_operator=True, params=params)
    assert [d.id for d in page.documents] == ["1"]


# ---------------------------------------------------------------------------
# Search (R4.9, R4.14)
# ---------------------------------------------------------------------------


def test_search_matches_metadata_case_insensitively():
    docs = [_doc("1", "Quarterly Report"), _doc("2", "Random Notes")]
    params = CorpusListParams(search="quarterly")
    page = _list(docs, viewer_identity=None, is_operator=True, params=params)
    assert [d.id for d in page.documents] == ["1"]


def test_search_matches_owner_field():
    docs = [_doc("1", "X", owner="alice@example.com"), _doc("2", "Y", owner="bob@example.com")]
    params = CorpusListParams(search="ALICE")
    page = _list(docs, viewer_identity=None, is_operator=True, params=params)
    assert [d.id for d in page.documents] == ["1"]


def test_blank_search_is_noop():
    docs = [_doc("1", "X"), _doc("2", "Y")]
    params = CorpusListParams(search="")
    page = _list(docs, viewer_identity=None, is_operator=True, params=params)
    assert len(page.documents) == 2


def test_search_term_at_limit_is_accepted():
    docs = [_doc("1", "X")]
    params = CorpusListParams(search="a" * MAX_SEARCH_TERM_LENGTH)
    page = _list(docs, viewer_identity=None, is_operator=True, params=params)
    assert page.documents == []  # no match, but not rejected


def test_search_term_over_limit_is_rejected():
    docs = [_doc("1", "X")]
    params = CorpusListParams(search="a" * (MAX_SEARCH_TERM_LENGTH + 1))
    with pytest.raises(SearchTermTooLongError) as exc:
        _list(docs, viewer_identity=None, is_operator=True, params=params)
    assert exc.value.code == "search_term_too_long"


# ---------------------------------------------------------------------------
# Pagination (R4.4, R4.5)
# ---------------------------------------------------------------------------


def _paginate_all(docs, *, page_size, is_operator=True, viewer_identity=None, base_params=None):
    """Page through the whole listing, returning the concatenated ids."""
    seen: list[str] = []
    cursor = None
    guard = 0
    while True:
        guard += 1
        assert guard < 1000, "pagination did not terminate"
        params = CorpusListParams(
            sort_field=(base_params or CorpusListParams()).sort_field,
            sort_direction=(base_params or CorpusListParams()).sort_direction,
            page_size=page_size,
            cursor=cursor,
        )
        page = list_corpus(
            docs,
            viewer_identity=viewer_identity,
            is_operator=is_operator,
            params=params,
            pagination_signing_key=_SIGNING_KEY,
            corpus_page_size=page_size,
            date_of=_date_of,
        )
        seen.extend(d.id for d in page.documents)
        assert len(page.documents) <= page_size
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    return seen


def test_pagination_visits_each_document_exactly_once():
    docs = [_doc(f"{i:02d}", f"Doc {i:02d}") for i in range(23)]
    seen = _paginate_all(docs, page_size=5)
    assert seen == [f"{i:02d}" for i in range(23)]
    assert len(seen) == len(set(seen))


def test_final_page_has_null_cursor():
    docs = [_doc("1", "A"), _doc("2", "B")]
    params = CorpusListParams(page_size=5)
    page = list_corpus(
        docs,
        viewer_identity=None,
        is_operator=True,
        params=params,
        pagination_signing_key=_SIGNING_KEY,
        corpus_page_size=5,
        date_of=_date_of,
    )
    assert page.next_cursor is None


def test_page_size_clamped_to_max():
    docs = [_doc(f"{i:02d}", f"Doc {i:02d}") for i in range(10)]
    params = CorpusListParams(page_size=1000)
    page = list_corpus(
        docs,
        viewer_identity=None,
        is_operator=True,
        params=params,
        pagination_signing_key=_SIGNING_KEY,
        corpus_page_size=4,
        date_of=_date_of,
    )
    assert len(page.documents) == 4
    assert page.next_cursor is not None


def test_pagination_stable_when_sorted_by_owner():
    docs = [
        _doc("d", "D", owner="z"),
        _doc("a", "A", owner="a"),
        _doc("c", "C", owner="a"),
        _doc("b", "B", owner="m"),
    ]
    base = CorpusListParams(sort_field=SortField.owner)
    seen = _paginate_all(docs, page_size=2, base_params=base)
    # owners: a (a), a (c), m (b), z (d) -> ids a, c, b, d
    assert seen == ["a", "c", "b", "d"]


# ---------------------------------------------------------------------------
# Cursor integrity (R4.6)
# ---------------------------------------------------------------------------


def _first_page_cursor(docs):
    params = CorpusListParams(page_size=1)
    page = list_corpus(
        docs,
        viewer_identity=None,
        is_operator=True,
        params=params,
        pagination_signing_key=_SIGNING_KEY,
        corpus_page_size=1,
        date_of=_date_of,
    )
    return page.next_cursor


def test_malformed_cursor_rejected():
    docs = [_doc("1", "A"), _doc("2", "B")]
    params = CorpusListParams(cursor="not-a-valid-cursor")
    with pytest.raises(InvalidCursorError) as exc:
        _list(docs, viewer_identity=None, is_operator=True, params=params)
    assert exc.value.code == "invalid_cursor"


def test_tampered_cursor_signature_rejected():
    docs = [_doc("1", "A"), _doc("2", "B")]
    cursor = _first_page_cursor(docs)
    assert cursor is not None
    payload_b64, _sig = cursor.split(".")
    # Re-sign the same payload with a different key: signature won't verify.
    forged = encode_cursor(
        sort_field=SortField.name,
        sort_direction=SortDirection.asc,
        sort_value="a",
        last_id="1",
        signing_key="attacker-key",
    )
    params = CorpusListParams(cursor=forged)
    with pytest.raises(InvalidCursorError):
        _list(docs, viewer_identity=None, is_operator=True, params=params)


def test_truncated_cursor_rejected():
    docs = [_doc("1", "A"), _doc("2", "B")]
    cursor = _first_page_cursor(docs)
    assert cursor is not None
    truncated = cursor[: len(cursor) // 2]
    params = CorpusListParams(cursor=truncated)
    with pytest.raises(InvalidCursorError):
        _list(docs, viewer_identity=None, is_operator=True, params=params)


def test_cursor_from_different_ordering_rejected():
    docs = [_doc(f"{i}", f"Doc {i}") for i in range(5)]
    # Mint a cursor under name/asc then replay it against owner/asc.
    cursor = _first_page_cursor(docs)
    assert cursor is not None
    params = CorpusListParams(sort_field=SortField.owner, cursor=cursor)
    with pytest.raises(InvalidCursorError):
        _list(docs, viewer_identity=None, is_operator=True, params=params)


def test_valid_cursor_round_trips():
    token = encode_cursor(
        sort_field=SortField.date,
        sort_direction=SortDirection.desc,
        sort_value="2024-01-01",
        last_id="doc-9",
        signing_key=_SIGNING_KEY,
    )
    boundary = decode_cursor(token, _SIGNING_KEY)
    assert boundary.sort_field == "date"
    assert boundary.sort_direction == "desc"
    assert boundary.sort_value == "2024-01-01"
    assert boundary.last_id == "doc-9"
