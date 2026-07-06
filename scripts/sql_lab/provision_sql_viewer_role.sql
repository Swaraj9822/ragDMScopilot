-- =============================================================================
-- SQL Lab — SQL_Viewer_Role provisioning script
-- =============================================================================
--
-- This is the checked-in, authoritative provisioning artifact for the SQL Lab
-- read-only Postgres role (see .kiro/specs/sql-lab, Requirements 2.3 and 2.7,
-- and the design's "Provisioning (documentation artifact, R2.7)" section).
--
-- SECURITY STANCE
-- ---------------
-- The dedicated read-only Postgres role created here is the PRIMARY security
-- boundary for SQL Lab. The application-level `sqlglot` guard is only a
-- secondary guardrail. Even a query that slips past every application check
-- cannot write anything, because the role holds no write/DDL privilege at all.
--
-- ACCESS MODEL: BROAD READ, EXPLICIT DENY (read-by-default)
-- --------------------------------------------------------
-- This role is granted SELECT on ALL current tables in schema `public`, and —
-- via ALTER DEFAULT PRIVILEGES — on all tables created there in the future by
-- the app-owner role, so operators can browse the database as it grows without
-- a manual re-grant for every new table (the "replace pgAdmin" goal). The
-- Sensitive_Table set (`users`, `refresh_tokens`) is then explicitly REVOKED.
--
-- This is a deliberate read-by-default posture. Its consequence is spelled out
-- in step 4: a NEW sensitive table is readable the moment it is created unless
-- it is explicitly REVOKED here (and, ideally, added to the application guard's
-- SQL_LAB_SENSITIVE_TABLES denylist). Anyone adding a table that holds
-- credentials, secrets, PII, or tokens MUST add a REVOKE for it below.
--
-- This role is scoped so that it:
--   * can only SELECT (read) — it holds NO write/DDL/administrative privilege;
--   * can read every non-sensitive table in `public`, current and future;
--   * holds NO privilege whatsoever on the Sensitive_Table set
--     (`users`, `refresh_tokens`), which store bcrypt password hashes and
--     refresh tokens.
--
-- VIEWS — READ THIS BEFORE ADDING ONE
-- -----------------------------------
-- A VIEW is a distinct object with its OWN name, and the broad SELECT grant
-- (and the default privileges below) cover views too. A view named innocuously
-- (e.g. `user_report`) can expose a Sensitive_Table (`SELECT * FROM users`)
-- under a name the application guard's table-name denylist does NOT recognize,
-- so the guard will NOT block it — only this DB grant governs it. Before a view
-- becomes readable by this role, a reviewer MUST inspect its definition; if it
-- exposes sensitive data, REVOKE it here (step 4) and add its name to the guard
-- denylist. Do not rely on the guard to catch sensitive access through a view.
--
-- The role authenticates with the SQL_VIEWER_DB_USER / SQL_VIEWER_DB_PASSWORD
-- credentials over the shared COPILOT_DB_HOST/PORT/NAME/SSLMODE connection
-- endpoint (the same operational Postgres instance used by the rest of the app).
--
-- USAGE
-- -----
-- Run as a superuser or a role with CREATEROLE + ownership of the target tables,
-- against the operational database (the COPILOT_DB_NAME database):
--
--   psql "host=$COPILOT_DB_HOST port=$COPILOT_DB_PORT dbname=$COPILOT_DB_NAME \
--         sslmode=$COPILOT_DB_SSLMODE user=<admin_user>" \
--        -v viewer_role=sql_viewer \
--        -v viewer_password="$SQL_VIEWER_DB_PASSWORD" \
--        -v app_owner="$COPILOT_DB_USER" \
--        -f scripts/sql_lab/provision_sql_viewer_role.sql
--
--   * viewer_role     — the read-only role name (default: sql_viewer).
--   * viewer_password — the SQL_VIEWER_DB_PASSWORD.
--   * app_owner       — the role that OWNS/creates the application tables (e.g.
--                       COPILOT_DB_USER). ALTER DEFAULT PRIVILEGES only affects
--                       tables created by THIS role, so it must match the role
--                       your migrations run as, or future tables will not be
--                       auto-granted. Defaults to the connected user with a
--                       notice when omitted.
--
-- The script is idempotent: re-running it re-asserts the intended grants and
-- revocations without error. Pass the role name and password as psql variables
-- so no secret is hard-coded in this checked-in file.
-- =============================================================================

\set ON_ERROR_STOP on

-- Default the role name to `sql_viewer` when -v viewer_role was not supplied.
-- (psql evaluates :'viewer_role'; the guard below keeps this idempotent.)
\if :{?viewer_role}
\else
  \set viewer_role sql_viewer
\endif

-- Default the app-owner role to the connected user when -v app_owner was not
-- supplied. ALTER DEFAULT PRIVILEGES only affects tables created by app_owner,
-- so for future tables to be auto-granted this must be the role your migrations
-- run as (typically COPILOT_DB_USER). We warn when we fall back to current_user.
\if :{?app_owner}
\else
  SELECT current_user AS app_owner \gset
  \echo 'NOTICE: -v app_owner was not set; defaulting to current_user =' :'app_owner'
  \echo 'NOTICE: future-table auto-grant only covers tables created by this role.'
\endif

-- -----------------------------------------------------------------------------
-- 1. Create the role (idempotent). LOGIN role with the supplied password.
--    NOSUPERUSER / NOCREATEDB / NOCREATEROLE / NOINHERIT keep the role minimal.
-- -----------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'viewer_role') THEN
        EXECUTE format(
            'CREATE ROLE %I LOGIN PASSWORD %L '
            'NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT',
            :'viewer_role', :'viewer_password'
        );
    ELSE
        -- Role already exists: re-assert the password and minimal attributes.
        EXECUTE format(
            'ALTER ROLE %I LOGIN PASSWORD %L '
            'NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT',
            :'viewer_role', :'viewer_password'
        );
    END IF;
END
$$;

-- -----------------------------------------------------------------------------
-- 2. Strip every privilege first (fail-closed). We revoke ALL on all existing
--    tables/sequences/functions in schema `public`, and CREATE on the schema
--    itself, so the role starts from zero. The broad SELECT grant in step 3
--    then re-adds read access, and step 4 subtracts the sensitive tables.
-- -----------------------------------------------------------------------------
REVOKE ALL ON ALL TABLES    IN SCHEMA public FROM :"viewer_role";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM :"viewer_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM :"viewer_role";
REVOKE CREATE ON SCHEMA public FROM :"viewer_role";

-- Allow the role to resolve object names in `public` (USAGE only — this does
-- NOT by itself grant read access to any table; SELECT is granted in step 3).
GRANT USAGE ON SCHEMA public TO :"viewer_role";

-- -----------------------------------------------------------------------------
-- 3. Broad read: grant SELECT on ALL current tables/views, and on all FUTURE
--    tables/views created by app_owner in `public` via default privileges.
--    (R2.3/R2.6: the role can read approved, non-sensitive objects; step 4
--    removes the Sensitive_Table set from this broad grant.)
--
--    This read-by-default posture means operators never hit "permission denied"
--    on a newly added ordinary table — but it also means any new SENSITIVE
--    table is readable until step 4 revokes it. See step 4.
-- -----------------------------------------------------------------------------
GRANT SELECT ON ALL TABLES IN SCHEMA public TO :"viewer_role";

-- Future tables/views created by app_owner in `public` are auto-granted SELECT.
-- NOTE: this covers only objects created by app_owner; objects created by other
-- roles are not auto-granted (grant them explicitly, or run this per owner).
ALTER DEFAULT PRIVILEGES FOR ROLE :"app_owner" IN SCHEMA public
    GRANT SELECT ON TABLES TO :"viewer_role";

-- -----------------------------------------------------------------------------
-- 4. Sensitive_Table set — EXPLICITLY REVOKED (subtracted from the broad grant).
--
--    The broad SELECT in step 3 (and the default privileges) would otherwise
--    expose these; we revoke them so they remain unreadable by the viewer role.
--
--      * users           — bcrypt password hashes
--      * refresh_tokens  — refresh token records
--
--    A read attempt against them as the SQL_Viewer_Role MUST fail with an
--    authorization error and return zero rows (R2.1, R2.2).
--
--    >>> ADDING A NEW SENSITIVE TABLE OR VIEW? <<<
--    Because access is read-by-default (step 3), a new table/view holding
--    credentials, secrets, PII, or tokens is readable the instant it is created.
--    You MUST:
--      (a) add a `REVOKE ALL ON <name> FROM :"viewer_role";` line below, and
--      (b) add its name to the application guard's SQL_LAB_SENSITIVE_TABLES
--          denylist (defaults to `users,refresh_tokens`) so the secondary guard
--          also rejects it by name.
--    For a VIEW, revoking by name is essential — the guard cannot see that a
--    view reads a sensitive table (see the VIEWS note in the header).
-- -----------------------------------------------------------------------------
REVOKE ALL ON users          FROM :"viewer_role";
REVOKE ALL ON refresh_tokens FROM :"viewer_role";

-- Default privileges cannot target a specific table name, so there is no way to
-- pre-emptively exclude a FUTURE sensitive table here — the explicit per-object
-- REVOKE procedure documented above is the required control. Keep this REVOKE
-- list in sync with docs/sql-lab/provisioning.md and the guard's
-- SQL_LAB_SENSITIVE_TABLES denylist.

-- -----------------------------------------------------------------------------
-- 5. Verification (optional, informational). Lists the exact table privileges
--    held by the viewer role after provisioning. Expect SELECT on every
--    non-sensitive table/view and NOTHING on users / refresh_tokens.
-- -----------------------------------------------------------------------------
SELECT table_name, privilege_type
FROM information_schema.role_table_grants
WHERE grantee = :'viewer_role'
ORDER BY table_name, privilege_type;
