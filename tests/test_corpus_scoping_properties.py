"""Property-based tests for corpus-listing role scoping (R4.2, R4.3).

Feature: rag-trust-and-observability (task 8.2).

This module implements the single property named in the design for corpus
listing role scoping. It exercises :func:`rag_system.corpus.list_corpus` across
generated document sets and viewer identities, paging through the *entire*
listing (so the assertion is about the whole paginated corpus, not a single
page):

* an authenticated Operator's listing returns **every** backend Document,
  regardless of which owner uploaded it; and
* an authenticated non-operator's listing returns **only** the Documents that
  user is authorized to access — concretely, Documents whose ``owner`` equals
  the viewer's authenticated identity (owner-based scoping, R4.3/R4.11).

**Validates: Requirements 4.2, 4.3**
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

# A small pool of owners (including ``None`` for legacy, unowned records) and
# viewer identities (including one that owns nothing) so scoping is meaningfully
# exercised across the generated space.
_OWNERS = ["alice", "bob", "carol", None]
_VIEWERS = ["alice", "bob", "carol", "dave"]


def _make_doc(doc_id: str, title: str, owner: str | None) -> DocumentRecord:
    return DocumentRecord(
        id=doc_id,
        title=title,
        version="v1",
        s3_uri=f"s3://bucket/{doc_id}",
        status=DocumentStatus.indexed,
        owner=owner,
        active_version="v1",
    )


# Generate a corpus of documents with unique ids uploaded by arbitrary owners.
_documents = st.lists(
    st.tuples(
        st.text(alphabet="abcdefghijklmnop0123456789", min_size=1, max_size=6),
        st.text(alphabet="ABCDEFGHij ", min_size=0, max_size=8),
        st.sampled_from(_OWNERS),
    ),
    min_size=0,
    max_size=25,
    unique_by=lambda t: t[0],  # unique document ids
).map(lambda triples: [_make_doc(i, title, owner) for i, title, owner in triples])


def _collect_all_ids(
    docs: list[DocumentRecord],
    *,
    viewer_identity: str | None,
    is_operator: bool,
    page_size: int,
) -> list[str]:
    """Page through the entire scoped listing and return the ids seen."""
    seen: list[str] = []
    cursor: str | None = None
    guard = 0
    while True:
        guard += 1
        assert guard < 10_000, "pagination did not terminate"
        params = CorpusListParams(
            sort_field=SortField.name,
            sort_direction=SortDirection.asc,
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
            date_of=lambda _doc: None,
        )
        seen.extend(d.id for d in page.documents)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    return seen


# Feature: rag-trust-and-observability, Property 11: Corpus listing scoping by role
@settings(max_examples=200)
@given(
    docs=_documents,
    viewer_identity=st.sampled_from(_VIEWERS),
    page_size=st.integers(min_value=1, max_value=7),
)
def test_corpus_listing_scoping_by_role(
    docs: list[DocumentRecord],
    viewer_identity: str,
    page_size: int,
) -> None:
    all_ids = {d.id for d in docs}

    # Operators see every backend Document, regardless of uploading owner (R4.2).
    operator_ids = _collect_all_ids(
        docs, viewer_identity=viewer_identity, is_operator=True, page_size=page_size
    )
    assert set(operator_ids) == all_ids
    # No duplicates across pages.
    assert len(operator_ids) == len(set(operator_ids))

    # Non-operators see only the Documents they own (R4.3).
    expected_owned = {
        d.id for d in docs if d.owner is not None and d.owner == viewer_identity
    }
    non_operator_ids = _collect_all_ids(
        docs, viewer_identity=viewer_identity, is_operator=False, page_size=page_size
    )
    assert set(non_operator_ids) == expected_owned
    assert len(non_operator_ids) == len(set(non_operator_ids))

    # A non-operator must never be shown a Document owned by someone else or
    # an unowned legacy record.
    returned = {d.id: d for d in docs}
    for doc_id in non_operator_ids:
        doc = returned[doc_id]
        assert doc.owner == viewer_identity
