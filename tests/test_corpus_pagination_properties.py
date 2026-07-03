"""Property-based test for corpus-listing cursor pagination (R4.4, R4.5).

Feature: rag-trust-and-observability (task 8.3).

This module implements the single property named in the design for cursor
pagination of :func:`rag_system.corpus.list_corpus`. Paging through the whole
listing with the opaque, signed next-cursor must **partition the scoped corpus
exactly once**: every Document is visited, no Document is visited twice, and no
Document is skipped — across generated corpora, sort fields/directions, and
page sizes. In addition, every page is bounded by the effective (clamped) page
size and the final page carries a ``None`` next cursor.

**Validates: Requirements 4.4, 4.5**
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

_OWNERS = ["alice", "bob", "carol", None]
_DATES = ["2024-01-01", "2024-02-15", "2024-02-15", "2023-12-31", None]


def _make_doc(
    doc_id: str,
    title: str,
    owner: str | None,
    created_at: str | None,
) -> DocumentRecord:
    rec = DocumentRecord(
        id=doc_id,
        title=title,
        version="v1",
        s3_uri=f"s3://bucket/{doc_id}",
        status=DocumentStatus.indexed,
        owner=owner,
        active_version="v1",
    )
    # Attach a date the ``date_of`` accessor can read for the ``date`` sort.
    object.__setattr__(rec, "_created_at", created_at)
    return rec


def _date_of(rec: DocumentRecord) -> str | None:
    return getattr(rec, "_created_at", None)


# A corpus of documents with unique ids, arbitrary (possibly colliding) titles,
# owners, and dates so ties in the primary sort key are exercised and the
# tie-break on the stable id matters for a clean partition.
_documents = st.lists(
    st.tuples(
        st.text(alphabet="abcdefghijklmnop0123456789", min_size=1, max_size=6),
        st.text(alphabet="ABab ", min_size=0, max_size=4),
        st.sampled_from(_OWNERS),
        st.sampled_from(_DATES),
    ),
    min_size=0,
    max_size=40,
    unique_by=lambda t: t[0],  # unique document ids
).map(
    lambda rows: [_make_doc(i, title, owner, date) for i, title, owner, date in rows]
)


def _paginate_all(
    docs: list[DocumentRecord],
    *,
    sort_field: SortField,
    sort_direction: SortDirection,
    requested_page_size: int,
    corpus_page_size: int,
    is_operator: bool = True,
    viewer_identity: str | None = None,
) -> tuple[list[str], list[int], bool]:
    """Page through the entire listing.

    Returns the ids seen (in visitation order), the size of each page, and
    whether the terminal page ended with a ``None`` next cursor.
    """
    seen: list[str] = []
    page_sizes: list[int] = []
    cursor: str | None = None
    terminated_with_null = False
    guard = 0
    while True:
        guard += 1
        assert guard < 10_000, "pagination did not terminate"
        params = CorpusListParams(
            sort_field=sort_field,
            sort_direction=sort_direction,
            page_size=requested_page_size,
            cursor=cursor,
        )
        page = list_corpus(
            docs,
            viewer_identity=viewer_identity,
            is_operator=is_operator,
            params=params,
            pagination_signing_key=_SIGNING_KEY,
            corpus_page_size=corpus_page_size,
            date_of=_date_of,
        )
        seen.extend(d.id for d in page.documents)
        page_sizes.append(len(page.documents))
        if page.next_cursor is None:
            terminated_with_null = True
            break
        cursor = page.next_cursor
    return seen, page_sizes, terminated_with_null


# Feature: rag-trust-and-observability, Property 12: Cursor pagination partitions the corpus exactly once
@settings(max_examples=250)
@given(
    docs=_documents,
    sort_field=st.sampled_from(list(SortField)),
    sort_direction=st.sampled_from(list(SortDirection)),
    requested_page_size=st.integers(min_value=1, max_value=12),
    corpus_page_size=st.integers(min_value=1, max_value=12),
)
def test_cursor_pagination_partitions_the_corpus_exactly_once(
    docs: list[DocumentRecord],
    sort_field: SortField,
    sort_direction: SortDirection,
    requested_page_size: int,
    corpus_page_size: int,
) -> None:
    all_ids = {d.id for d in docs}

    seen, page_sizes, terminated_with_null = _paginate_all(
        docs,
        sort_field=sort_field,
        sort_direction=sort_direction,
        requested_page_size=requested_page_size,
        corpus_page_size=corpus_page_size,
    )

    # Partition exactly once: every Document visited, none skipped (no gaps)...
    assert set(seen) == all_ids
    # ...and none visited twice (no duplicates).
    assert len(seen) == len(set(seen))

    # Page sizes are bounded by the effective (clamped) page size (R4.4).
    effective_page_size = max(1, min(requested_page_size, max(1, corpus_page_size)))
    assert all(size <= effective_page_size for size in page_sizes)

    # The final page always carries a null next cursor (R4.4).
    assert terminated_with_null is True


# Feature: rag-trust-and-observability, Property 12: Cursor pagination partitions the corpus exactly once
@settings(max_examples=100)
@given(
    docs=_documents,
    viewer_identity=st.sampled_from(["alice", "bob", "carol", "dave"]),
    sort_field=st.sampled_from(list(SortField)),
    sort_direction=st.sampled_from(list(SortDirection)),
    requested_page_size=st.integers(min_value=1, max_value=8),
    corpus_page_size=st.integers(min_value=1, max_value=8),
)
def test_cursor_pagination_partitions_the_scoped_corpus_for_non_operator(
    docs: list[DocumentRecord],
    viewer_identity: str,
    sort_field: SortField,
    sort_direction: SortDirection,
    requested_page_size: int,
    corpus_page_size: int,
) -> None:
    # For a non-operator the partitioned set is the owner-scoped corpus: paging
    # must still visit each authorized Document exactly once with no gaps.
    expected = {
        d.id for d in docs if d.owner is not None and d.owner == viewer_identity
    }

    seen, page_sizes, terminated_with_null = _paginate_all(
        docs,
        sort_field=sort_field,
        sort_direction=sort_direction,
        requested_page_size=requested_page_size,
        corpus_page_size=corpus_page_size,
        is_operator=False,
        viewer_identity=viewer_identity,
    )

    assert set(seen) == expected
    assert len(seen) == len(set(seen))

    effective_page_size = max(1, min(requested_page_size, max(1, corpus_page_size)))
    assert all(size <= effective_page_size for size in page_sizes)
    assert terminated_with_null is True
