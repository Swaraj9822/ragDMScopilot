"""Property test for missing SQL Lab viewer credentials (task 1.3).

Feature: sql-lab (Slice 1 — viewer role + configuration).

Property 7: Missing or failed viewer credentials produce a keyed, secret-free error

    *For any* combination of present/absent viewer credentials and any secret
    values, when execution is attempted with a credential missing or the
    connection failing, the surfaced error names the missing configuration key
    (or indicates a viewer connection failure) and its text contains none of the
    provided secret values.

**Validates: Requirements 1.5, 1.6**

This test focuses on the *credential-missing* path (R1.5): when either
``SQL_VIEWER_DB_USER`` or ``SQL_VIEWER_DB_PASSWORD`` is absent,
``Settings.require_sql_viewer_credentials()`` raises ``SqlLabConfigError`` whose
message names exactly the missing key(s) and never embeds the (secret) value of
the credential that *was* provided. (The connection-failure half of R1.6 — that
``SqlLabConnectionError`` is likewise value-free — is exercised at the executor
level against a live Postgres in the role-scoping integration tests.)

Strategy design (smart generators): the input space is the cross product of
(which credentials are present) × (arbitrary secret values for the present
ones). We restrict the presence combinations to those with *at least one*
credential absent (so the keyed error is raised) and draw each present secret as
an arbitrary non-empty string, deliberately including adversarial values that
embed the configuration key names and comment/quote characters. Secrets that
happen to be a substring of *any* possible error message are filtered out so the
"no secret leaks" assertion is both meaningful and non-flaky.
"""

from __future__ import annotations

import pytest
from hypothesis import assume, example, given, settings
from hypothesis import strategies as st

from rag_system.config import Settings
from rag_system.sql_lab.errors import SqlLabConfigError

_USER_KEY = "SQL_VIEWER_DB_USER"
_PASSWORD_KEY = "SQL_VIEWER_DB_PASSWORD"

# The three possible keyed messages, precomputed. A secret value that is a
# substring of any of these could appear in the message without being a leak, so
# we exclude such values from the generated secrets to keep the leak assertion
# unambiguous. (The message is built purely from constant key-name strings, so
# no genuine secret can ever appear in it.)
_ALL_POSSIBLE_MESSAGES = "\n".join(
    (
        f"Missing SQL Lab configuration: {_USER_KEY}",
        f"Missing SQL Lab configuration: {_PASSWORD_KEY}",
        f"Missing SQL Lab configuration: {_USER_KEY}, {_PASSWORD_KEY}",
    )
)

# The required Settings fields supplied by alias so the model can be built in
# isolation without depending on a ``.env`` file (mirrors the convention in
# ``test_config_sample_rate_properties.py``).
_REQUIRED_BY_ALIAS = {
    "RAG_GCS_BUCKET": "test-bucket",
    "LLAMA_CLOUD_API_KEY": "test-llama-key",
    "PINECONE_API_KEY": "test-pinecone-key",
    "PINECONE_INDEX_NAME": "test-index",
}


def _build_settings(user: str | None, password: str | None) -> Settings:
    """Construct ``Settings`` with the given viewer credentials.

    The credentials are assigned directly onto the constructed model (rather
    than via env/alias) so the test controls presence/absence exactly, immune to
    any ambient ``.env`` values or alias coercion.
    """
    config = Settings(**_REQUIRED_BY_ALIAS)  # type: ignore[arg-type]
    config.sql_viewer_db_user = user
    config.sql_viewer_db_password = password
    return config


# Arbitrary secret values: non-empty, drawn from a broad printable range, and
# excluding any value that is a substring of a possible error message (so the
# leak check below is meaningful). Explicit examples cover the adversarial
# "secret that looks like a key name / embeds markers" cases.
_secret_values = st.text(min_size=1, max_size=40).filter(
    lambda s: s not in _ALL_POSSIBLE_MESSAGES
)

# Presence combinations with at least one credential absent, so
# ``require_sql_viewer_credentials`` raises.
_missing_combos = st.sampled_from([(True, False), (False, True), (False, False)])


# Feature: sql-lab, Property 7: Missing or failed viewer credentials produce a keyed, secret-free error
# Validates: Requirements 1.5, 1.6
@settings(max_examples=300)
@given(
    combo=_missing_combos,
    user_secret=_secret_values,
    password_secret=_secret_values,
)
@example(combo=(True, False), user_secret="hunter2", password_secret="s3cr3t!")
@example(combo=(False, True), user_secret=_USER_KEY, password_secret=_PASSWORD_KEY)
@example(combo=(False, False), user_secret="' OR 1=1 --", password_secret="/* p */")
def test_missing_viewer_credentials_raise_keyed_secret_free_error(
    combo: tuple[bool, bool],
    user_secret: str,
    password_secret: str,
) -> None:
    """A missing viewer credential yields a keyed, secret-free config error.

    Validates: Requirements 1.5
    """
    user_present, password_present = combo

    user = user_secret if user_present else None
    password = password_secret if password_present else None
    config = _build_settings(user, password)

    with pytest.raises(SqlLabConfigError) as excinfo:
        config.require_sql_viewer_credentials()

    message = str(excinfo.value)

    # The error names the missing configuration key(s) by name.
    if not user_present:
        assert _USER_KEY in message
    if not password_present:
        assert _PASSWORD_KEY in message

    # A credential that WAS provided is not reported as missing.
    if user_present:
        assert _USER_KEY not in message
    if password_present:
        assert _PASSWORD_KEY not in message

    # The message is exactly the keyed template built only from key names, which
    # proves it is byte-for-byte independent of the secret values.
    expected_missing = [
        key
        for key, present in ((_USER_KEY, user_present), (_PASSWORD_KEY, password_present))
        if not present
    ]
    assert message == "Missing SQL Lab configuration: " + ", ".join(expected_missing)

    # No provided secret value appears anywhere in the surfaced error text.
    for present, secret in ((user_present, user_secret), (password_present, password_secret)):
        if present:
            assert secret not in message


# Feature: sql-lab, Property 7 (complementary direction): both credentials present
# Validates: Requirements 1.5
@settings(max_examples=200)
@given(user_secret=_secret_values, password_secret=_secret_values)
def test_present_viewer_credentials_return_without_error(
    user_secret: str, password_secret: str
) -> None:
    """When both credentials are present, the pair is returned and no error is raised."""
    # Guard against the degenerate case where a generated value is falsy after
    # coercion (empty string is treated as missing by the accessor).
    assume(user_secret and password_secret)

    config = _build_settings(user_secret, password_secret)

    returned = config.require_sql_viewer_credentials()

    assert returned == (user_secret, password_secret)
