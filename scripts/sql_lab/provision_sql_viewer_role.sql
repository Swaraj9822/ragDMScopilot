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
-- cannot read a table this role has no SELECT grant on, nor write anything,
-- because the database itself refuses.
--
-- This role is scoped so that it:
--   * can only SELECT (read) — it holds NO write/DDL/administrative privilege;
--   * can read ONLY the explicitly approved, non-sensitive tables listed below;
--   * holds NO privilege whatsoever on the Sensitive_Table set
--     (`users`, `refresh_tokens`), which store bcrypt password hashes and
--     refresh tokens.
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
--        -f scripts/sql_lab/provision_sql_viewer_role.sql
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
--    tables/sequences in schema `public`, and CREATE on the schema itself, so
--    the role starts from zero and can only do what we explicitly grant below.
-- -----------------------------------------------------------------------------
REVOKE ALL ON ALL TABLES    IN SCHEMA public FROM :"viewer_role";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM :"viewer_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM :"viewer_role";
REVOKE CREATE ON SCHEMA public FROM :"viewer_role";

-- Allow the role to resolve object names in `public` (USAGE only — this does
-- NOT grant read access to any table; SELECT is granted per-table below).
GRANT USAGE ON SCHEMA public TO :"viewer_role";

-- Ensure the role gets NO privileges on tables created in the future by the
-- app-owner role. New tables must be explicitly approved and granted here,
-- so a newly-added table is never silently readable.
-- (Replace <app_owner_role> with the role that owns/creates app tables, e.g.
--  the COPILOT_DB_USER role, if you use ALTER DEFAULT PRIVILEGES.)
-- ALTER DEFAULT PRIVILEGES FOR ROLE <app_owner_role> IN SCHEMA public
--     REVOKE ALL ON TABLES FROM :"viewer_role";

-- -----------------------------------------------------------------------------
-- 3. Grant SELECT ONLY on the explicitly approved, non-sensitive tables.
--    (R2.3: SELECT privileges only on explicitly approved, non-Sensitive_Table
--    tables as the primary access-control mechanism.)
--
--    Approved-table list (operational / observability data — no secrets):
--      * traces       — request traces
--      * spans        — trace spans
--      * log_records  — application log records
--
--    Add a table to this list ONLY after confirming it holds no authentication
--    or credential data. Keep this list in sync with the SQL Lab docs at
--    docs/sql-lab/provisioning.md.
-- -----------------------------------------------------------------------------
GRANT SELECT ON traces      TO :"viewer_role";
GRANT SELECT ON spans       TO :"viewer_role";
GRANT SELECT ON log_records TO :"viewer_role";

-- -----------------------------------------------------------------------------
-- 4. Sensitive_Table set — INTENTIONALLY NOT GRANTED.
--
--    The following tables store authentication/credential data and MUST remain
--    unreadable by the SQL_Viewer_Role. There is deliberately NO GRANT for them
--    anywhere in this script. The REVOKE ALL in step 2 also guarantees the role
--    holds no residual privilege on them.
--
--      * users           — bcrypt password hashes  (NO GRANT)
--      * refresh_tokens  — refresh token records    (NO GRANT)
--
--    Do NOT add GRANT statements for these tables. A read attempt against them
--    as the SQL_Viewer_Role MUST fail with an authorization error and return
--    zero rows (R2.1, R2.2).
-- -----------------------------------------------------------------------------
-- (No GRANT on users.)
-- (No GRANT on refresh_tokens.)

-- Belt-and-suspenders: explicitly re-revoke on the sensitive tables in case a
-- prior manual grant ever leaked in. Safe to run repeatedly.
REVOKE ALL ON users          FROM :"viewer_role";
REVOKE ALL ON refresh_tokens FROM :"viewer_role";

-- -----------------------------------------------------------------------------
-- 5. Verification (optional, informational). Lists the exact table privileges
--    held by the viewer role after provisioning. Expect SELECT on the approved
--    tables only and NOTHING on users / refresh_tokens.
-- -----------------------------------------------------------------------------
SELECT table_name, privilege_type
FROM information_schema.role_table_grants
WHERE grantee = :'viewer_role'
ORDER BY table_name, privilege_type;
