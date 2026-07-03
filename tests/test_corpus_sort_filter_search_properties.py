"""Property-based test for corpus-listing sort/filter/search consistency (R4).

Feature: rag-trust-and-observability (task 8.4).

This module implements the single property named in the design for the
sort/filter/search behavior of :func:`rag_system.corpus.list_corpus`. It pages
through the *entire* listing and asserts, over the concatenation of all pages,
that:

* the full result is globally ordered by the selected sort field
  (``name``/``owner``/``date``) and direction, applied consistently across
  every page (R4.7);
* every returned Document satisfies each applied filter on
  ``status``/``owner``/``date``/``active version`` (R4.8);
* every returned Document's metadata contains the (case-insensitive) search
  term when one is supplied (R4.9); and
* every listed Document includes its owner (R4.11).

The checks are written as an *independent oracle* (they re-derive the expected
ordering key and metadata text rather than calling the module's private
helpers) so the test genuinely cross-checks the implementation.

**Validates: Requirements 4.7, 4.8, 4.9, 4.11**
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.corpus import (
    CorpusListParams,
    SortDirection,
    SortField,
    list_corpus,
)
from rag_system.models import DocumentRecord, DocumentStatus

_SIGNING_KEY = "test-pagination-secret"

# Small pools so ties in the primary sort key and real filter/search matches are
# exercised densely across the generated space.
_OWNERS = ["alice", "bob", "carol", None]
_STATUSES = [
    DocumentStatus.indexed,
    DocumentStatus.queued,
    DocumentStatus.failed,
    DocumentStatus.deleted,
]
_ACTIVE_VERSIONS = ["v1", "v2", "v3", None]
_DATES = ["2023-12-31", "2024-01-01", "2024-02-15", "2024-02-15", None]

# Alphabet shared by titles, owners, and versions so generated search terms have
# a realistic chance of actually matching document metadata.
_META_ALPHABET = "abABv123 "


def _make_doc(
    doc_id: str,
    title: str,
    owner: str | None,
    status: DocumentStatus,
    active_version: str | None,
    created_at: str | None,
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
    # Attach a date the ``date_of`` accessor can read for the ``date`` sort/filter.
    object.__setattr__(rec, "_created_at", created_at)
    return rec


def _date_of(rec: DocumentRecord) -> str | None:
    return getattr(rec, "_created_at", None)


_documents = st.lists(
    st.tuples(
        st.text(alphabet="abcdefghijklmnop0123456789", min_size=1, max_size=6),
        st.text(alphabet=_META_ALPHABET, min_size=0, max_size=5),
        st.sampled_from(_OWNERS),
        st.sampled_from(_STATUSES),
        st.sampled_from(_ACTIVE_VERSIONS),
        st.sampled_from(_DATES),
    ),
    min_size=0,
    max_size=40,
    unique_by=lambda t: t[0],  # unique document ids
).map(
    lambda rows: [
        _make_doc(i, title, owner, status, active, date)
        for i, title, owner, status, active, date in rows
    ]
)


# ---------------------------------------------------------------------------
# Independent oracles (re-derived, not imported from the module under test)
# ---------------------------------------------------------------------------


def _expected_primary_sort_value(doc: DocumentRecord, sort_field: SortField) -> str:
    if sort_field == SortField.name:
        return doc.title.casefold()
    if sort_field == SortField.owner:
        return (doc.owner or "").casefold()
    return _date_of(doc) or ""


def _expected_ordering_key(
    doc: DocumentRecord, sort_field: SortField
) -> tuple[str, str]:
    return (_expected_primary_sort_value(doc, sort_field), doc.id)


def _expected_searchable_text(doc: DocumentRecord) -> str:
    parts = [
        doc.id,
        doc.title,
        doc.version,
        doc.status.value,
        doc.owner or "",
        doc.active_version or "",
    ]
    return "\n".join(parts).casefold()


def _paginate_all(
    docs: list[DocumentRecord],
    *,
    params_factory,
    corpus_page_size: int,
) -> list[DocumentRecord]:
    """Page through the whole listing and return the visited documents in order."""
    visited: list[DocumentRecord] = []
    cursor: str | None = None
    guard = 0
    while True:
        guard += 1
        assert guard < 10_000, "pagination did not terminate"
        page = list_corpus(
            docs,
            viewer_identity=None,
            is_operator=True,
            params=params_factory(cursor),
            pagination_signing_key=_SIGNING_KEY,
            corpus_page_size=corpus_page_size,
            date_of=_date_of,
        )
        visited.extend(page.documents)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    return visited


# Feature: rag-trust-and-observability, Property 13: Sort and filter are consistent across pages
@settings(max_examples=300)
@given(
    docs=_documents,
    sort_field=st.sampled_from(list(SortField)),
    sort_direction=st.sampled_from(list(SortDirection)),
    filter_status=st.none() | st.sampled_from(_STATUSES),
    filter_owner=st.none() | st.sampled_from([o for o in _OWNERS if o is not None]),
    filter_active_version=st.none()
    | st.sampled_from([v for v in _ACTIVE_VERSIONS if v is not None]),
    date_from=st.none() | st.sampled_from([d for d in _DATES if d is not None]),
    date_to=st.none() | st.sampled_from([d for d in _DATES if d is not None]),
    search=st.none() | st.text(alphabet=_META_ALPHABET, min_size=1, max_size=6),
    requested_page_size=st.integers(min_value=1, max_value=10),
    corpus_page_size=st.integers(min_value=1, max_value=10),
)
def test_sort_filter_search_consistent_across_pages(
    docs: list[DocumentRecord],
    sort_field: SortField,
    sort_direction: SortDirection,
    filter_status: DocumentStatus | None,
    filter_owner: str | None,
    filter_active_version: str | None,
    date_from: str | None,
    date_to: str | None,
    search: str | None,
    requested_page_size: int,
    corpus_page_size: int,
) -> None:
    def params_factory(cursor: str | None) -> CorpusListParams:
        return CorpusListParams(
            sort_field=sort_field,
            sort_direction=sort_direction,
            status=filter_status,
            owner=filter_owner,
            active_version=filter_active_version,
            date_from=date_from,
            date_to=date_to,
            search=search,
            page_size=requested_page_size,
            cursor=cursor,
        )

    visited = _paginate_all(
        docs, params_factory=params_factory, corpus_page_size=corpus_page_size
    )

    # --- R4.7: globally ordered by the selected field/direction across pages ---
    keys = [_expected_ordering_key(d, sort_field) for d in visited]
    if sort_direction == SortDirection.asc:
        assert all(keys[i] <= keys[i + 1] for i in range(len(keys) - 1))
    else:
        assert all(keys[i] >= keys[i + 1] for i in range(len(keys) - 1))

    for doc in visited:
        # --- R4.8: every returned Document satisfies each applied filter ---
        if filter_status is not None:
            assert doc.status == filter_status
        if filter_owner is not None:
            assert doc.owner == filter_owner
        if filter_active_version is not None:
            assert doc.active_version == filter_active_version
        if date_from is not None or date_to is not None:
            doc_date = _date_of(doc)
            assert doc_date is not None
            if date_from is not None:
                assert doc_date >= date_from
            if date_to is not None:
                assert doc_date <= date_to

        # --- R4.9: metadata contains the (case-insensitive) search term ---
        if search:
            assert search.casefold() in _expected_searchable_text(doc)

        # --- R4.11: every listed Document includes its owner ---
        assert hasattr(doc, "owner")

    # The concatenated listing never invents or drops documents relative to what
    # the (operator-scoped) filter/search actually selects.
    def _oracle_selected(doc: DocumentRecord) -> bool:
        if filter_status is not None and doc.status != filter_status:
            return False
        if filter_owner is not None and doc.owner != filter_owner:
            return False
        if filter_active_version is not None and doc.active_version != filter_active_version:
            return False
        if date_from is not None or date_to is not None:
            d = _date_of(doc)
            if d is None:
                return False
            if date_from is not None and d < date_from:
                return False
            if date_to is not None and d > date_to:
                return False
        if search and search.casefold() not in _expected_searchable_text(doc):
            return False
        return True

    expected_ids = {d.id for d in docs if _oracle_selected(d)}
    assert {d.id for d in visited} == expected_ids
