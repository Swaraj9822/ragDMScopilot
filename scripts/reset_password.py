"""Admin password-reset CLI (finding: no password-recovery path).

Self-managed auth has no self-service password reset (there is no email plane).
An operator locked out of their account can be recovered here by an admin with
database access: this resets the password hash for an existing account and
revokes all of that user's refresh tokens so any stale sessions cannot continue.

Usage:
    python -m scripts.reset_password user@example.com
    # prompts for the new password (twice), no echo

    python scripts/reset_password.py user@example.com --password "$NEW_PASSWORD"
    # non-interactive (e.g. from a secret store); avoid shell history for secrets

Exit codes: 0 = reset (and sessions revoked), 1 = no such account / reset
succeeded but session revocation failed / other error, 2 = bad usage.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from rag_system.auth.passwords import hash_password
from rag_system.auth.refresh_store import PostgresRefreshTokenStore
from rag_system.auth.store import PostgresUserStore
from rag_system.config import get_settings


def _read_new_password(supplied: str | None) -> str:
    if supplied is not None:
        return supplied
    first = getpass.getpass("New password: ")
    second = getpass.getpass("Confirm new password: ")
    if first != second:
        print("Passwords do not match.", file=sys.stderr)
        raise SystemExit(2)
    return first


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reset a user's password.")
    parser.add_argument("email", help="Email of the account to reset.")
    parser.add_argument(
        "--password",
        default=None,
        help="New password (omit to be prompted interactively without echo).",
    )
    args = parser.parse_args(argv)

    new_password = _read_new_password(args.password)
    if not new_password:
        print("Password must not be empty.", file=sys.stderr)
        return 2

    settings = get_settings()
    store = PostgresUserStore(settings)

    record = store.set_password(args.email, hash_password(new_password))
    if record is None:
        print(f"No account found for {args.email!r}.", file=sys.stderr)
        return 1

    # Invalidate existing sessions: any refresh token issued before the reset is
    # revoked so a compromised/forgotten session cannot outlive the reset. If
    # revocation fails the password change still stands, but we must NOT claim
    # sessions were revoked when they weren't — report the partial success
    # honestly and exit non-zero so an incident responder isn't falsely assured
    # that a compromised session was invalidated.
    try:
        PostgresRefreshTokenStore(settings).revoke_all_for_user(record.id)
    except Exception as exc:  # noqa: BLE001 - password reset succeeded; report partial failure
        print(
            f"Password reset for {record.email} (id={record.id}), but revoking "
            f"existing sessions FAILED: {exc}. Existing refresh tokens may still "
            "be usable — revoke them manually before treating this account as "
            "secure.",
            file=sys.stderr,
        )
        return 1

    print(f"Password reset for {record.email} (id={record.id}). Existing sessions revoked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
