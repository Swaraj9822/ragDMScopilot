# SQL Lab — SQL_Viewer_Role provisioning

This document describes how to provision the dedicated read-only Postgres role
used by SQL Lab (the Data Explorer tab). It is the human-readable companion to
the checked-in SQL script:

- Script: [`scripts/sql_lab/provision_sql_viewer_role.sql`](../../scripts/sql_lab/provision_sql_viewer_role.sql)

It satisfies SQL Lab **Requirement 2.7** ("document the exact `SELECT` grant and
role-creation steps required to provision the `SQL_Viewer_Role`, including the
approved-table list and the excluded `Sensitive_Table` set") and implements the
grant model of **Requirement 2.3**.

## Why a dedicated role

The single operational Postgres instance holds authentication data (`users`,
`refresh_tokens`), observability data (`traces`, `spans`, `log_records`), and
other business data all in one database. SQL Lab lets operators run ad-hoc
read-only SQL, so the role it connects with **must not** be able to read the
authentication tables or write anything.

The dedicated read-only role is the **primary security boundary**. The
`sqlglot`-based SQL guard in the application is only a **secondary guardrail**.
Even a statement that slips past every application check cannot read a table the
role has no `SELECT` grant on, nor modify data, because the database itself
refuses.

## What the role can and cannot do

| Capability | Allowed? | Enforced by |
| --- | --- | --- |
| `SELECT` on approved non-sensitive tables | ✅ Yes | explicit `GRANT SELECT` |
| `SELECT` on `users` / `refresh_tokens` | ❌ No | no grant + `REVOKE ALL` |
| `INSERT` / `UPDATE` / `DELETE` | ❌ No | no write grant |
| `CREATE` / `ALTER` / `DROP` / DDL | ❌ No | no DDL grant, no `CREATE` on schema |

## Approved-table list (SELECT granted)

These operational/observability tables contain **no** authentication or
credential data and are safe to read:

- `traces` — request traces
- `spans` — trace spans
- `log_records` — application log records

To approve an additional table, confirm it holds no secrets, add a
`GRANT SELECT ON <table> TO :"viewer_role";` line to the script's step 3, and
update this list so the two stay in sync.

## Excluded Sensitive_Table set (never granted)

These tables store authentication/credential data and are **intentionally never
granted** to the viewer role:

- `users` — bcrypt password hashes
- `refresh_tokens` — refresh token records

The script never issues a `GRANT` for them, and it additionally runs an explicit
`REVOKE ALL` on both as a belt-and-suspenders guarantee. A read attempt against
either table as the viewer role must fail with an authorization error and return
zero rows (Requirements 2.1 and 2.2).

## Connection settings

The viewer role authenticates with its own credentials but reuses the shared
connection endpoint:

- User: `SQL_VIEWER_DB_USER`
- Password: `SQL_VIEWER_DB_PASSWORD`
- Endpoint: `COPILOT_DB_HOST`, `COPILOT_DB_PORT`, `COPILOT_DB_NAME`,
  `COPILOT_DB_SSLMODE` (shared with the rest of the app)

These are read exclusively through `rag_system.config.Settings`; the backend
never reads environment variables directly.

## Running the script

Run as a superuser (or a role with `CREATEROLE` and ownership of the target
tables) against the operational database. Pass the role name and password as
`psql` variables so no secret is hard-coded in the checked-in file:

```bash
psql "host=$COPILOT_DB_HOST port=$COPILOT_DB_PORT dbname=$COPILOT_DB_NAME \
      sslmode=$COPILOT_DB_SSLMODE user=<admin_user>" \
     -v viewer_role=sql_viewer \
     -v viewer_password="$SQL_VIEWER_DB_PASSWORD" \
     -f scripts/sql_lab/provision_sql_viewer_role.sql
```

The script is **idempotent** — re-running it re-asserts the intended grants and
revocations without error, and re-applies the supplied password.

## What the script does, step by step

1. **Create (or update) the role** — a minimal `LOGIN` role
   (`NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT`) with the supplied password.
2. **`REVOKE ALL` first (fail-closed)** — revokes every privilege on all
   existing tables, sequences, and functions in schema `public`, plus `CREATE`
   on the schema, so the role starts from zero. Grants only `USAGE` on the
   schema (name resolution only — this is not table read access).
3. **`GRANT SELECT`** on the approved-table list above, and only that list.
4. **Sensitive tables** — no grant is issued; an explicit `REVOKE ALL` on
   `users` and `refresh_tokens` is re-asserted.
5. **Verification query** — prints the exact table privileges the role holds.
   Expect `SELECT` on the approved tables only, and nothing on `users` /
   `refresh_tokens`.

## Verifying the scoping

After running the script, the final `SELECT` from
`information_schema.role_table_grants` should list `SELECT` on `traces`,
`spans`, and `log_records` only. You can also confirm the sensitive tables are
denied by connecting as the viewer role and attempting a read — it must raise a
permission-denied error:

```sql
-- As the SQL_Viewer_Role, this MUST fail with "permission denied for table users":
SELECT * FROM users LIMIT 1;
```

The automated database-level checks for this scoping live in the SQL Lab
integration tests (spec task 8.2).
