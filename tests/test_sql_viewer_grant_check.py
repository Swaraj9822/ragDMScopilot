"""Unit tests for the SQL Lab viewer-role grant drift check (finding #5).

Exercises the pure :func:`find_grant_violations` invariant checker — the DB
connection is not touched, so these run credential-free in CI.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

# Load the script module by path (scripts/ is not an importable package).
_SPEC = importlib.util.spec_from_file_location(
    "check_viewer_grants",
    Path(__file__).resolve().parent.parent / "scripts" / "sql_lab" / "check_viewer_grants.py",
)
assert _SPEC is not None and _SPEC.loader is not None
check_viewer_grants = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_viewer_grants)
find_grant_violations = check_viewer_grants.find_grant_violations

_SENSITIVE = ("users", "refresh_tokens")


def test_select_only_on_non_sensitive_tables_is_clean() -> None:
    rows = [("products", "SELECT"), ("sales_order", "SELECT")]
    assert find_grant_violations(rows, _SENSITIVE) == []


def test_write_privilege_is_a_violation() -> None:
    rows = [("products", "SELECT"), ("products", "INSERT")]
    violations = find_grant_violations(rows, _SENSITIVE)
    assert len(violations) == 1
    assert "INSERT" in violations[0]


def test_any_privilege_on_sensitive_table_is_a_violation() -> None:
    # Even SELECT on a sensitive table is drift (it must hold no grant at all).
    rows = [("users", "SELECT")]
    violations = find_grant_violations(rows, _SENSITIVE)
    assert len(violations) == 1
    assert "users" in violations[0]


def test_write_on_sensitive_table_reports_both_violations() -> None:
    rows = [("refresh_tokens", "UPDATE")]
    violations = find_grant_violations(rows, _SENSITIVE)
    # One for the non-SELECT privilege, one for the sensitive-table reference.
    assert len(violations) == 2


def test_comparisons_are_case_insensitive() -> None:
    rows = [("Users", "select"), ("Products", "select")]
    violations = find_grant_violations(rows, _SENSITIVE)
    # 'Users' matches the sensitive denylist case-insensitively; 'Products' clean.
    assert len(violations) == 1
    assert "Users" in violations[0]


def test_empty_grants_is_clean() -> None:
    assert find_grant_violations([], _SENSITIVE) == []
