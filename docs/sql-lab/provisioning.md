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
Even a statement that slips past every application check cannot modify data,
because the role holds no write privilege, and cannot read a table the role has
had its grant revoked from, because the database itself refuses.

## Access model: broad read, explicit deny

SQL Lab exists to replace the pgAdmin workflow, so the role uses a
**read-by-default** posture: it is granted `SELECT` on **all** current tables in
schema `public`, and — via `ALTER DEFAULT PRIVILEGES` — on all tables created
there in the future by the **app-owner** role. The `Sensitive_Table` set
(`users`, `refresh_tokens`) is then explicitly **revoked**.

This means operators never hit "permission denied" on an ordinary new table as
the schema grows. The trade-off is the flip side of read-by-default:

> **A new sensitive table or view is readable the moment it is created**, unless
> it is explicitly revoked. See [Adding a new sensitive table or view](#adding-a-new-sensitive-table-or-view).

## What the role can and cannot do

| Capability | Allowed? | Enforced by |
| --- | --- | --- |
| `SELECT` on any non-sensitive table/view (current and future) | ✅ Yes | broad `GRANT SELECT` + `ALTER DEFAULT PRIVILEGES` |
| `SELECT` on `users` / `refresh_tokens` | ❌ No | explicit `REVOKE ALL` |
| `INSERT` / `UPDATE` / `DELETE` | ❌ No | no write grant |
| `CREATE` / `ALTER` / `DROP` / DDL | ❌ No | no DDL grant, no `CREATE` on schema |

## Excluded Sensitive_Table set (revoked)

These tables store authentication/credential data and are **explicitly revoked**
from the broad grant:

- `users` — bcrypt password hashes
- `refresh_tokens` — refresh token records

A read attempt against either table as the viewer role must fail with an
authorization error and return zero rows (Requirements 2.1 and 2.2).

## Adding a new sensitive table or view

Because access is read-by-default, **you** are responsible for excluding
anything that holds credentials, secrets, PII, or tokens. When you add such a
table or view, you MUST do both of the following:

1. **Revoke it in the script (step 4):** add a
   `REVOKE ALL ON <name> FROM :"viewer_role";` line and re-run the script.
2. **Add it to the guard denylist:** add its name to the application guard's
   `SQL_LAB_SENSITIVE_TABLES` setting (defaults to `users,refresh_tokens`) so the
   secondary guard also rejects it by name.

### Views deserve special care

A **view** is a distinct object with its own name, and the broad `SELECT` grant
(and the default privileges) cover views too. A view named innocuously — say
`user_report` defined as `SELECT * FROM users` — exposes a sensitive table under
a name the guard's table-name denylist does **not** recognize, so the
application guard will **not** block it. Only the database grant governs a view.

Therefore: **a reviewer must inspect a view's definition before it becomes
readable by the viewer role.** If it exposes sensitive data, revoke it in the
script and add its name to `SQL_LAB_SENSITIVE_TABLES`. Do not rely on the guard
to catch sensitive access reached through a view.

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
tables) against the operational database. Pass the role name, password, and the
app-owner role as `psql` variables so no secret is hard-coded in the checked-in
file:

```bash
psql "host=$COPILOT_DB_HOST port=$COPILOT_DB_PORT dbname=$COPILOT_DB_NAME \
      sslmode=$COPILOT_DB_SSLMODE user=<admin_user>" \
     -v viewer_role=sql_viewer \
     -v viewer_password="$SQL_VIEWER_DB_PASSWORD" \
     -v app_owner="$COPILOT_DB_USER" \
     -f scripts/sql_lab/provision_sql_viewer_role.sql
```

- **`app_owner`** must be the role that **owns/creates** the application tables
  (typically `COPILOT_DB_USER`, the role your migrations run as).
  `ALTER DEFAULT PRIVILEGES` only affects tables created by this role, so if it
  is wrong, future tables will not be auto-granted. When omitted, it defaults to
  the connected user with a notice.

The script is **idempotent** — re-running it re-asserts the intended grants and
revocations without error, and re-applies the supplied password.

## What the script does, step by step

1. **Create (or update) the role** — a minimal `LOGIN` role
   (`NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT`) with the supplied password.
2. **`REVOKE ALL` first (fail-closed)** — revokes every privilege on all
   existing tables, sequences, and functions in schema `public`, plus `CREATE`
   on the schema, so the role starts from zero. Grants only `USAGE` on the
   schema (name resolution only — this is not table read access).
3. **Broad `GRANT SELECT`** on all current tables/views in `public`, plus
   `ALTER DEFAULT PRIVILEGES` so tables/views created in the future by
   `app_owner` are auto-granted `SELECT`.
4. **Sensitive tables** — an explicit `REVOKE ALL` on `users` and
   `refresh_tokens` subtracts them from the broad grant.
5. **Verification query** — prints the exact table privileges the role holds.
   Expect `SELECT` on every non-sensitive table/view, and nothing on `users` /
   `refresh_tokens`.

## Verifying the scoping

After running the script, the final `SELECT` from
`information_schema.role_table_grants` should list `SELECT` on the non-sensitive
tables and views, and **nothing** on `users` / `refresh_tokens`. You can also
confirm the sensitive tables are denied by connecting as the viewer role and
attempting a read — it must raise a permission-denied error:

```sql
-- As the SQL_Viewer_Role, this MUST fail with "permission denied for table users":
SELECT * FROM users LIMIT 1;
```

The automated database-level checks for this scoping live in the SQL Lab
integration tests (spec task 8.2).
