"""Corpus listing service (R4).

This module implements the pure logic behind the ``GET /corpus`` endpoint: a
cursor-paginated, sortable, filterable, searchable listing of the backend
:class:`~rag_system.models.DocumentRecord` corpus with owner-based role scoping.

Design highlights (see design R4):

* **Owner-based role scoping (R4.2, R4.3, R4.11).** Operators see the complete
  backend corpus. A non-operator sees only Documents whose ``owner`` equals
  their authenticated identity. Scoping is applied *before* pagination so the
  owner filter and the cursor window compose consistently.
* **Signed, opaque cursors (R4.4, R4.5, R4.6).** A cursor is a base64url token
  carrying the sort context and the (sort-key, id) boundary of the last item on
  the previous page. The payload is signed with HMAC-SHA256 keyed by
  ``pagination_signing_key``; the signature is verified on decode and any
  tampered, truncated, or otherwise invalid token is rejected with
  ``invalid_cursor`` (never trusted or silently reset).
* **Deterministic keyset pagination.** The scoped/filtered/searched corpus is
  ordered by ``(primary_sort_value, id)`` (a total order) and the cursor resumes
  strictly after its boundary, so paging visits each Document exactly once with
  no duplicates or gaps.
* **Bounded page size (R4.4).** The effective page size is clamped to
  ``corpus_page_size``; the final page returns a ``None`` next cursor.
* **Sort / filter / search (R4.7, R4.8, R4.9, R4.14).** Sort by ``name`` /
  ``owner`` / ``date`` with direction; filter by ``status`` / ``owner`` /
  ``date`` / ``active version``; case-insensitive metadata search of 1–200
  characters. A search term longer than 200 characters is rejected with
  ``search_term_too_long``.

The listing operates on an in-memory sequence of records (the service reads the
one-per-key document records and passes them in), keeping this module a pure,
deterministic function that is trivially unit- and property-testable without any
storage or HTTP dependency.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256

from .models import CorpusPage, DocumentRecord, DocumentStatus

__all__ = [
    "SortField",
    "SortDirection",
    "CorpusListParams",
    "CorpusError",
    "InvalidCursorError",
    "SearchTermTooLongError",
    "MAX_SEARCH_TERM_LENGTH",
    "list_corpus",
    "encode_cursor",
    "decode_cursor",
]

#: Maximum length of a metadata search term (R4.9, R4.14).
MAX_SEARCH_TERM_LENGTH = 200

#: Callable that derives an (optional) ISO-8601 date string for a document, used
#: for the ``date`` sort field and date-range filter. ``DocumentRecord`` carries
#: no intrinsic date, so the caller supplies one (e.g. from the version index).
#: When omitted, dates are treated as unavailable: date sorting falls back to the
#: stable id order and date-range filters exclude undated documents.
DateAccessor = Callable[[DocumentRecord], str | None]


class SortField(StrEnum):
    """Sort fields exposed by the corpus listing (R4.7)."""

    name = "name"
    owner = "owner"
    date = "date"


class SortDirection(StrEnum):
    """Sort directions exposed by the corpus listing (R4.7)."""

    asc = "asc"
    desc = "desc"


class CorpusError(Exception):
    """Base class for corpus listing errors carrying a stable ``code``.

    The ``code`` is the machine-readable error string the endpoint (8.6) maps to
    a structured HTTP error body.
    """

    code = "corpus_error"


class InvalidCursorError(CorpusError):
    """Raised when a pagination cursor is malformed, forged, or truncated (R4.6)."""

    code = "invalid_cursor"


class SearchTermTooLongError(CorpusError):
    """Raised when a search term exceeds :data:`MAX_SEARCH_TERM_LENGTH` (R4.14)."""

    code = "search_term_too_long"


@dataclass(frozen=True)
class CorpusListParams:
    """Parameters for a single corpus listing request.

    All fields are optional; the defaults produce a name-ascending listing of the
    scoped corpus with no filters or search and the maximum page size.
    """

    sort_field: SortField = SortField.name
    sort_direction: SortDirection = SortDirection.asc
    #: Filter by document status (R4.8).
    status: DocumentStatus | None = None
    #: Filter by owner (R4.8); independent of role scoping.
    owner: str | None = None
    #: Filter by active version value (R4.8).
    active_version: str | None = None
    #: Inclusive lower bound on the document date (R4.8).
    date_from: str | None = None
    #: Inclusive upper bound on the document date (R4.8).
    date_to: str | None = None
    #: Case-insensitive metadata search term, 1–200 chars (R4.9, R4.14).
    search: str | None = None
    #: Requested page size; clamped to ``corpus_page_size`` (R4.4).
    page_size: int | None = None
    #: Opaque, signed cursor from a previous page (R4.5).
    cursor: str | None = None


# ---------------------------------------------------------------------------
# Cursor encoding / decoding (HMAC-signed, opaque) — R4.5, R4.6
# ---------------------------------------------------------------------------


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(token: str) -> bytes:
    # Restore the stripped ``=`` padding before decoding.
    padding = "=" * (-len(token) % 4)
    try:
        return base64.urlsafe_b64decode(token + padding)
    except (binascii.Error, ValueError) as exc:  # malformed base64
        raise InvalidCursorError("Cursor is not valid base64.") from exc


def _sign(payload: bytes, signing_key: str | None) -> bytes:
    key = (signing_key or "").encode("utf-8")
    return hmac.new(key, payload, sha256).digest()


def encode_cursor(
    *,
    sort_field: SortField,
    sort_direction: SortDirection,
    sort_value: str,
    last_id: str,
    signing_key: str | None,
) -> str:
    """Encode a signed, opaque cursor identifying the end of the current page.

    The payload records the sort context (so a cursor cannot be replayed against
    a differently-ordered listing) and the ``(sort_value, last_id)`` boundary the
    next page resumes strictly after.
    """
    payload_obj = {
        "sf": str(sort_field),
        "dir": str(sort_direction),
        "pk": sort_value,
        "id": last_id,
    }
    payload = json.dumps(payload_obj, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    signature = _sign(payload, signing_key)
    return f"{_b64url_encode(payload)}.{_b64url_encode(signature)}"


@dataclass(frozen=True)
class _CursorBoundary:
    sort_field: str
    sort_direction: str
    sort_value: str
    last_id: str


def decode_cursor(token: str, signing_key: str | None) -> _CursorBoundary:
    """Verify and decode a cursor, or raise :class:`InvalidCursorError` (R4.6).

    Any deviation — wrong shape, bad base64, tampered payload, or a signature
    mismatch — is rejected rather than trusted or silently reset.
    """
    if not token:
        raise InvalidCursorError("Cursor is empty.")

    parts = token.split(".")
    if len(parts) != 2:
        raise InvalidCursorError("Cursor is malformed.")

    payload_b64, signature_b64 = parts
    payload = _b64url_decode(payload_b64)
    signature = _b64url_decode(signature_b64)

    expected = _sign(payload, signing_key)
    # Constant-time comparison so a forged cursor cannot be probed byte by byte.
    if not hmac.compare_digest(signature, expected):
        raise InvalidCursorError("Cursor signature does not verify.")

    try:
        obj = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidCursorError("Cursor payload is not valid JSON.") from exc

    if not isinstance(obj, dict):
        raise InvalidCursorError("Cursor payload is not an object.")

    try:
        boundary = _CursorBoundary(
            sort_field=str(obj["sf"]),
            sort_direction=str(obj["dir"]),
            sort_value=str(obj["pk"]),
            last_id=str(obj["id"]),
        )
    except KeyError as exc:
        raise InvalidCursorError("Cursor payload is missing fields.") from exc

    # Reject values that are not part of the supported ordering vocabulary.
    if boundary.sort_field not in {f.value for f in SortField}:
        raise InvalidCursorError("Cursor has an unknown sort field.")
    if boundary.sort_direction not in {d.value for d in SortDirection}:
        raise InvalidCursorError("Cursor has an unknown sort direction.")

    return boundary


# ---------------------------------------------------------------------------
# Scoping, filtering, searching, ordering
# ---------------------------------------------------------------------------


def _scope_by_role(
    documents: Iterable[DocumentRecord],
    *,
    viewer_identity: str | None,
    is_operator: bool,
) -> list[DocumentRecord]:
    """Apply owner-based role scoping before pagination (R4.2, R4.3, R4.11)."""
    if is_operator:
        return list(documents)
    # A non-operator sees only the Documents they own. Legacy records with no
    # owner (``None``) are never matched by a concrete identity, so they stay
    # hidden from non-operators.
    return [doc for doc in documents if doc.owner is not None and doc.owner == viewer_identity]


def _passes_filters(
    doc: DocumentRecord,
    params: CorpusListParams,
    date_of: DateAccessor,
) -> bool:
    """Return whether a document satisfies the applied filters (R4.8)."""
    if params.status is not None and doc.status != params.status:
        return False
    if params.owner is not None and doc.owner != params.owner:
        return False
    if params.active_version is not None and doc.active_version != params.active_version:
        return False

    if params.date_from is not None or params.date_to is not None:
        doc_date = date_of(doc)
        if doc_date is None:
            # Undated documents cannot satisfy a date-range filter.
            return False
        if params.date_from is not None and doc_date < params.date_from:
            return False
        if params.date_to is not None and doc_date > params.date_to:
            return False

    return True


def _searchable_text(doc: DocumentRecord) -> str:
    """Concatenate a document's metadata fields for case-insensitive search."""
    parts = [
        doc.id,
        doc.title,
        doc.version,
        doc.status.value if isinstance(doc.status, DocumentStatus) else str(doc.status),
        doc.owner or "",
        doc.active_version or "",
    ]
    return "\n".join(parts).casefold()


def _matches_search(doc: DocumentRecord, term: str) -> bool:
    return term.casefold() in _searchable_text(doc)


def _primary_sort_value(
    doc: DocumentRecord,
    sort_field: SortField,
    date_of: DateAccessor,
) -> str:
    """Return the primary (pre-tiebreak) sort value for a document.

    ``name`` sorts case-insensitively by title; ``owner`` by owner; ``date`` by
    the supplied date string. Missing values collapse to the empty string so the
    ordering stays total and deterministic.
    """
    if sort_field == SortField.name:
        return doc.title.casefold()
    if sort_field == SortField.owner:
        return (doc.owner or "").casefold()
    # SortField.date
    return date_of(doc) or ""


def _ordering_key(
    doc: DocumentRecord,
    sort_field: SortField,
    date_of: DateAccessor,
) -> tuple[str, str]:
    """Total-order key: primary sort value, tie-broken by the stable document id."""
    return (_primary_sort_value(doc, sort_field, date_of), doc.id)


def _clamp_page_size(requested: int | None, maximum: int) -> int:
    """Clamp the requested page size into ``[1, maximum]`` (R4.4)."""
    limit = max(1, maximum)
    if requested is None:
        return limit
    return max(1, min(requested, limit))


def list_corpus(
    documents: Sequence[DocumentRecord],
    *,
    viewer_identity: str | None,
    is_operator: bool,
    params: CorpusListParams,
    pagination_signing_key: str | None,
    corpus_page_size: int,
    date_of: DateAccessor | None = None,
) -> CorpusPage:
    """Produce one cursor-paginated :class:`CorpusPage` of the scoped corpus.

    Pipeline (order matters): role scope → filter → search → order → resume at
    cursor → slice to the clamped page size → emit next cursor.

    Args:
        documents: The full backend corpus (one record per document).
        viewer_identity: The authenticated caller's identity, matched against
            ``DocumentRecord.owner`` for non-operators.
        is_operator: Whether the caller is an operator (sees the whole corpus).
        params: Sort, filter, search, page-size, and cursor parameters.
        pagination_signing_key: HMAC-SHA256 key for cursor signing/verification.
        corpus_page_size: The configured maximum page size (R4.4).
        date_of: Optional accessor mapping a document to an ISO date string for
            the ``date`` sort field and date-range filter.

    Returns:
        A :class:`CorpusPage` with the page of documents and a ``next_cursor``
        that is ``None`` on the final page.

    Raises:
        SearchTermTooLongError: The search term exceeds 200 characters (R4.14).
        InvalidCursorError: The cursor is malformed, forged, or truncated (R4.6).
    """
    resolve_date: DateAccessor = date_of or (lambda doc: getattr(doc, "created_at", None))

    # R4.14: reject an over-long search term before doing any work, leaving the
    # caller's currently displayed listing unchanged.
    search_term = params.search
    if search_term is not None and len(search_term) > MAX_SEARCH_TERM_LENGTH:
        raise SearchTermTooLongError(
            f"Search term exceeds {MAX_SEARCH_TERM_LENGTH} characters."
        )

    # 1. Owner-based role scoping (R4.2, R4.3, R4.11), applied before pagination.
    scoped = _scope_by_role(
        documents, viewer_identity=viewer_identity, is_operator=is_operator
    )

    # 2. Filters (R4.8).
    filtered = [doc for doc in scoped if _passes_filters(doc, params, resolve_date)]

    # 3. Case-insensitive metadata search (R4.9). A blank term is a no-op.
    if search_term:
        filtered = [doc for doc in filtered if _matches_search(doc, search_term)]

    # 4. Total ordering (R4.7), consistent across pages.
    reverse = params.sort_direction == SortDirection.desc
    ordered = sorted(
        filtered,
        key=lambda doc: _ordering_key(doc, params.sort_field, resolve_date),
        reverse=reverse,
    )

    # 5. Resume strictly after the cursor boundary (R4.5, R4.6).
    if params.cursor is not None:
        boundary = decode_cursor(params.cursor, pagination_signing_key)
        # A cursor is only valid for the ordering it was minted under; a mismatch
        # does not identify a valid position in this listing (R4.6).
        if (
            boundary.sort_field != str(params.sort_field)
            or boundary.sort_direction != str(params.sort_direction)
        ):
            raise InvalidCursorError("Cursor does not match the requested ordering.")

        boundary_key = (boundary.sort_value, boundary.last_id)
        if reverse:
            ordered = [
                doc
                for doc in ordered
                if _ordering_key(doc, params.sort_field, resolve_date) < boundary_key
            ]
        else:
            ordered = [
                doc
                for doc in ordered
                if _ordering_key(doc, params.sort_field, resolve_date) > boundary_key
            ]

    # 6. Clamp the page size and slice (R4.4).
    page_size = _clamp_page_size(params.page_size, corpus_page_size)
    page = ordered[:page_size]

    # 7. Emit a next cursor only when further documents remain (R4.4).
    next_cursor: str | None = None
    if len(ordered) > page_size and page:
        last = page[-1]
        next_cursor = encode_cursor(
            sort_field=params.sort_field,
            sort_direction=params.sort_direction,
            sort_value=_primary_sort_value(last, params.sort_field, resolve_date),
            last_id=last.id,
            signing_key=pagination_signing_key,
        )

    return CorpusPage(documents=page, next_cursor=next_cursor)
