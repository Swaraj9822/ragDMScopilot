"""Property tests for SQL Lab guard comment stripping (task 2.2).

Feature: sql-lab (Slice 1 — SQL guard secondary guardrail).

The ``SqlLabGuard`` (``rag_system.sql_lab.guard``) reuses
``rag_system.copilot._strip_sql_comments`` as its first, string-literal-aware
comment-removal step. These tests exercise that step directly.

Property 1: Comment stripping preserves string literals
    For any SQL string containing string literals or quoted identifiers that
    embed comment markers (``--``, ``/* */``), along with real comments, the
    guard's comment-stripping step removes only the real comments and preserves
    the contents of every string literal and quoted identifier verbatim.

Validates: Requirements 3.1

Strategy design (smart generators): a source string is assembled from a list of
independently generated *segments*, each carrying both the text it contributes
to the input and the text it must contribute to the stripped output:

* plain SQL text — drawn from an alphabet that excludes the quote and
  comment-marker characters (``' " - / *``) so segments cannot accidentally
  form a comment or string boundary at a join, copied verbatim;
* string literals (``'...'``) whose inner text may embed ``--``, ``/* */`` and
  ``"`` (single quotes doubled per SQL escaping) — preserved verbatim;
* quoted identifiers (``"..."``) whose inner text may embed comment markers and
  ``'`` (double quotes doubled) — preserved verbatim;
* real line comments (``-- ... \n``) — the stripper emits a single space and
  leaves the terminating newline, so the expected contribution is ``" \n"``;
* real block comments (``/* ... */``) — the stripper emits a single space.

Because string literals and quoted identifiers are always balanced (escapes
doubled) and comments always terminated, segment boundaries never let a comment
be swallowed by a literal (or vice versa), so the expected output is simply the
concatenation of each segment's expected contribution. Asserting the stripped
source equals that concatenation proves *both* halves of the property at once:
real comments are removed and every literal / quoted identifier survives byte
for byte.
"""

from __future__ import annotations

import string as _string

from hypothesis import given, settings
from hypothesis import strategies as st

from rag_system.copilot import _strip_sql_comments

# Plain text excludes the quote and comment-marker characters so a plain
# segment can never (on its own or at a boundary with a neighbour) form ``--``,
# ``/*``, ``*/`` or open a string/identifier.
_PLAIN_ALPHABET = _string.ascii_letters + _string.digits + " \n\t,.()=<>+"

# Inner content for literals/identifiers deliberately embeds comment markers and
# the *other* quote character, exactly the "markers hiding inside a literal"
# case the property is about.
_INNER_ALPHABET = _string.ascii_letters + _string.digits + " -/*'\"\n\t,;=()"

# Line-comment bodies may contain anything except a newline (which terminates
# the comment); markers/quotes inside a comment must be ignored by the stripper.
_LINE_COMMENT_BODY = _string.ascii_letters + _string.digits + " -/*'\";=(),.\t"

# Block-comment bodies exclude ``/`` and ``*`` so the (non-nested) comment is
# unambiguously terminated by the first ``*/`` we append. Nested block comments
# are covered by the existing example-based unit tests in ``test_copilot.py``.
_BLOCK_COMMENT_BODY = _string.ascii_letters + _string.digits + " -'\";=(),.\n\t"


def _plain(text: str) -> tuple[str, str]:
    return (text, text)


def _string_literal(inner: str) -> tuple[str, str]:
    literal = "'" + inner.replace("'", "''") + "'"
    return (literal, literal)


def _quoted_identifier(inner: str) -> tuple[str, str]:
    ident = '"' + inner.replace('"', '""') + '"'
    return (ident, ident)


def _line_comment(body: str) -> tuple[str, str]:
    # Stripper consumes ``--`` .. up-to-(not-including) newline, emitting a
    # single space; the terminating newline is left to be copied verbatim.
    return ("--" + body + "\n", " \n")


def _block_comment(body: str) -> tuple[str, str]:
    return ("/*" + body + "*/", " ")


_segments = st.one_of(
    st.text(alphabet=_PLAIN_ALPHABET, min_size=0, max_size=10).map(_plain),
    st.text(alphabet=_INNER_ALPHABET, min_size=0, max_size=12).map(_string_literal),
    st.text(alphabet=_INNER_ALPHABET, min_size=1, max_size=12).map(_quoted_identifier),
    st.text(alphabet=_LINE_COMMENT_BODY, min_size=0, max_size=12).map(_line_comment),
    st.text(alphabet=_BLOCK_COMMENT_BODY, min_size=0, max_size=12).map(_block_comment),
)


@settings(max_examples=400)
@given(st.lists(_segments, min_size=0, max_size=14))
def test_comment_stripping_preserves_string_literals(parts: list[tuple[str, str]]) -> None:
    """Property 1 — real comments are removed; literals/identifiers survive verbatim.

    Validates: Requirements 3.1
    """
    source = "".join(inp for inp, _ in parts)
    expected = "".join(out for _, out in parts)

    stripped = _strip_sql_comments(source)

    # Exact-shape check: proves removal of real comments *and* verbatim
    # preservation of every string literal / quoted identifier simultaneously.
    assert stripped == expected

    # Redundant-but-explicit preservation check: each literal/identifier segment
    # appears verbatim in the stripped output (documents the property's intent).
    for inp, out in parts:
        if inp and inp[0] in ("'", '"'):
            assert out in stripped


def test_comment_stripping_preserves_markers_inside_literals_example() -> None:
    """Anchored example: embedded markers survive while real comments are removed.

    Validates: Requirements 3.1
    """
    source = (
        "SELECT '/* not a comment */' AS a, "  # literal with block markers
        '"weird--col" '  # quoted identifier with line markers
        "-- a real trailing comment\n"
        "/* a real block comment */"
    )

    stripped = _strip_sql_comments(source)

    # Literal and quoted-identifier contents are preserved verbatim.
    assert "'/* not a comment */'" in stripped
    assert '"weird--col"' in stripped
    # Real comments are gone.
    assert "a real trailing comment" not in stripped
    assert "a real block comment" not in stripped
    assert "/* a real block comment */" not in stripped
