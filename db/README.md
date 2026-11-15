# Platform database migrations

Schema for the platform's system of record: **PlanetScale Vitess (MySQL 8)**, eu-central-1
(decision #46, which replaces the Postgres engine of #30; the DPA exception of #30 stands).
Design: [docs/platform/blueprint.md](../docs/platform/blueprint.md) §7 and decisions #42, #46
and #47 in [project.md](../project.md).

| File | What it does |
|---|---|
| `migrations/0001_registry_access_control_audit_trail.sql` | Tables `registry_*`, `access_control_*`, `audit_trail_events`: cells, tenants, accounts, the access catalogue, role assignments, sessions, invitations, clones and their versions and personas, skills, endpoints, API keys, webhooks, scope and mint requests, the audit trail. |
| `migrations/0002_seed_access.sql` | Seed permissions, seed roles and the pooled cell. **Generated** from `packages/rudi_platform/access/catalogue.py`. Do not edit by hand. |
| `migrations/0003_runtime_schedule.sql` | Tables `runtime_*` and `schedule_items`: end-users, channel handles, identity links, consents, conversations, turns, messages, escalations, takeovers, due items. |
| `migrations/0004_metering_ops.sql` | Tables `metering_*` and `ops_*`: metered calls, budgets, alerts, erasures with receipts, long-running jobs. |
| `migrate.py` | Runs a migration step through the runner Lambda (below). |

One database, no schemas: on Vitess a schema is a separate keyspace, so a table's first word
names its area instead (`registry_`, `runtime_`, …).

## Tenant isolation: every statement names its tenant (decision #47)

Vitess has no row-level security, so the database itself does not keep tenants apart. The
platform does, with one hard rule:

> **Every statement that touches a tenant's rows names that tenant.** A `SELECT`, `UPDATE` or
> `DELETE` carries `tenant_id = %s` (bound to the caller's tenant) as a top-level `AND` of its
> `WHERE` clause, for every tenant table it reads; an `INSERT` writes the caller's tenant into
> `tenant_id`. A statement that does not is **refused before it is sent**: it never reaches
> PlanetScale.

The rule is enforced in code by `rudi_platform.db.tenant_guard`. Every Lambda or process that
connects to PlanetScale wraps its connection in `TenantBound` before any other code may use it:

```python
from rudi_platform.db import TenantBound

db = TenantBound(connection, ctx.tenant_id)            # a tenant's rows
db.query("SELECT id, status FROM runtime_conversations WHERE tenant_id = %s AND id = %s",
         (ctx.tenant_id, conversation_id))

platform = TenantBound.platform(connection)            # the platform's own rows
platform.query("SELECT id FROM ops_alerts WHERE tenant_id IS NULL AND status = %s", ("open",))
```

- The guard checks that the `%s` bound to `tenant_id` **is the guard's own tenant**, so a
  statement cannot name another tenant by mistake.
- Joins and subqueries: each tenant table carries its own `<alias>.tenant_id = %s`, in the
  `WHERE` of its own query level or, for a joined table, in its own `JOIN … ON`.
- Platform work across tenants runs **tenant by tenant**. The platform's own rows
  (`tenant_id` NULL: platform alerts, jobs, audit events, platform-wide roles) are read with
  `TenantBound.platform` and `tenant_id IS NULL`.
- Tables of no tenant (`NO_TENANT_DATA` in the guard and in `test_schema.py`: cells, tenants,
  accounts, sessions, the access catalogue, the shared skill inventory) need no condition. Any
  other table, including a new one, is treated as a tenant's.
- The guard reads a deliberately plain dialect so its check is exact: no comments or `;`, no
  `UNION`/`WITH`, no `RIGHT`/`FULL`/`CROSS`/comma joins, no `OR`/`XOR`/`BETWEEN`/`CASE` at the top
  of a `WHERE` or `ON` (inside parentheses they are fine), positional `%s` only. It refuses what
  it cannot read.
- **Resolvers** are the only unscoped reads of a tenant table: the closed list in
  `tenant_guard.RESOLVERS` that finds the tenant of a request arriving without one (an inbound
  message's address, an API key, an invitation token, an account's assignments at sign-in). They
  return ids only. Adding one is a decision.
- Every index of a tenant table leads with `tenant_id` (apart from the resolvers' global keys),
  which keeps these statements cheap and is the key Vitess would shard on.
- The **migration runner** is the one process without the guard: it changes the schema and seeds
  platform tables; a test checks that no migration writes a tenant table.

## Applying migrations in the PlanetScale console (how it is done today)

The user applies migrations by hand in PlanetScale's web console (decision #44). The files in
`migrations/` are not pasted directly: `{new_id:cat}` is a placeholder the console cannot fill.
Prepare each migration first, from the repository root:

```cmd
python db/console_sql.py 2
```

This writes `db/console/0002_seed_access.sql` (git-ignored): every statement ready to paste, ids
minted, followed by two statements that record the migration in `meta_migrations` (creating that
table the first time). Run the statements in order, and the record only after every statement
before it succeeded, so the database itself says what is applied and `python db/migrate.py plan`
stays right. Ids are minted per file: apply a file once. A migration applied by hand before this
existed gets its record with `python db/console_sql.py 1 --record-only`. Claude prepares the
files; the user runs them.

## The runner Lambda (optional)

The runner is the Lambda **`meetrudi-internal-prod-db-migrate`** (`services/db-migrate`). It
reads the credentials from Secrets Manager (`meetrudi/planetscale-backbone/…`) with its own role,
so they never reach this machine. It applies the migrations it was **deployed with**: after
adding one, deploy first. Each step is one command from the repository root:

```cmd
python deploy.py db-migrate
python db/migrate.py check
python db/migrate.py plan
python db/migrate.py apply
```

| Step | What it does | Writes? |
|---|---|---|
| `check` | Connects with each of the three secrets and reports the server (MySQL 8, Vitess) and database it reaches | No |
| `plan` | What `meta_migrations` records: applied, unfinished (failed or interrupted, with the statement it stopped at), pending, and any drift | No |
| `apply` | Applies the pending migrations statement by statement, resuming an unfinished one where it stopped; `apply 3` stops after 0003. Asks you to type `APPLY` | Yes |

**You apply the DDL, not Claude** (CLAUDE.md §7). Claude writes and checks migrations and may run
`check` and `plan` when asked; `apply` is yours. It refuses to run without an interactive
terminal.

How the runner keeps the database safe (`packages/rudi_platform/db/migrate.py`). MySQL commits
every schema change on its own, so a migration cannot land all or nothing, and there is no
rehearsal that rolls back:

- **Refused before anything is sent:** what PlanetScale cannot run (triggers, stored routines,
  events, views, foreign keys, renames, `AUTO_INCREMENT`, `CREATE DATABASE`/`USE`, users and
  grants, `SET GLOBAL`, `LOAD DATA`), transaction control, `/* */` comments (MySQL runs `/*! */`
  as code) and backslashes (an escape inside MySQL strings).
- **Statement by statement, recorded:** a migration's row in `meta_migrations` counts the
  statements done. A failure stops the run, leaves the row `failed` with the error, and keeps
  what ran before it.
- **Resume:** the next `apply` restarts a failed migration at the statement that failed, provided
  the statements that already ran are unchanged (their checksum is kept). Fixing the failed
  statement or a later one is allowed; changing one that ran is drift.
- **One runner at a time:** the row is the lock. A second runner finds it `running` and stops; a
  `running` row older than 10 minutes belongs to a run that died and is resumed.
- **Drift stops everything:** if an applied migration's file changed (SHA-256 of the text with LF
  line ends) or is gone, nothing runs.
- `{new_id:cat}` in a migration becomes a fresh id each time its statement runs (the seed; there
  are no stored functions to mint one).
- Each statement has 120 s to answer.

**Where the database is.** The three secrets hold only a user name and a password, so the host,
port and database name come from `deploy.local.json` (`planetscale_host`, `planetscale_port`
3306, `planetscale_database`: the PlanetScale database's name; git-ignored) and reach the Lambda
as settings. A secret that carries its own `host` overrides them. The connection always verifies
the server's certificate and host name.

**Passwords.** On PlanetScale only a password with the **Admin** role may change the schema, so
the secret `apply` uses (`…/primary-main-write`) must hold one. `plan` only reads, with
`…/primary-main-read` (Reader is enough); PlanetScale's **Writer** role may write rows but not
read them, so it can serve neither. The application's passwords need only read or read/write. A
PlanetScale password belongs to one branch.

**Where it is today (2026-11-15):** database `meet-rudi-backbone` (eu-central-1), host
`aws.connect.psdb.cloud`, port 3306. `…/primary-main-write` holds a Writer password, so `apply`
through the runner needs an Admin password there first; migrations go in through the console.

**Branches.** PlanetScale refuses direct schema changes on a branch with **safe migrations** on.
Until the platform holds production data, `main` keeps safe migrations off and the runner applies
to it directly. Once safe migrations is turned on for `main`, migrations are applied with the
runner to a development branch and reach `main` through a PlanetScale deploy request, with the
database's "copy migration data" setting naming the table `meta_migrations`. That change of
workflow is a decision of its own when it comes.

## Rules

- **Never edit a migration that has run anywhere.** Add the next number instead. Files are
  numbered contiguously from `0001`; a test checks this, and the runner refuses an edited one.
- **Every statement on a tenant's rows names its tenant** (decision #47, above), through
  `TenantBound`. No raw connection to PlanetScale outside the migration runner.
- **Every multi-tenant table has its own `tenant_id` column** (decision #41):
  `tenant_id CHAR(36) NOT NULL` with the `ten_` prefix check, even where a parent row implies the
  tenant, and every index of the table leads with it. A table without one holds no tenant's data
  and goes on the `NO_TENANT_DATA` lists in `test_schema.py` and the guard, with the reason.
  Details: conventions §8.
- **Ids** are `CHAR(36)` with a `REGEXP_LIKE(id, '^xxx_[0-9a-hjkmnp-tv-z]{32}$', 'c')` check for
  the table's registered prefix (docs/platform/conventions.md §1); a column pointing at another
  row checks the prefix of what it points at. The application mints ids with `rudi_platform.ids`.
- **No foreign keys, triggers or stored routines** (PlanetScale runs none, and foreign keys stop
  at the first shard). What they would enforce is the stores' job, held by contract tests: a
  reference points at an existing row of the same tenant; **a tenant's slug never changes** (it
  names the tenant's S3 folder, `tenants/{slug}/`, conventions §2); **published clone versions
  and their personas are immutable**; **the audit trail is append-only** (ids and decisions
  only); erasure deletes an end-user's rows table by table.
- **Personal text is ciphertext only** (decision #42): message text, channel handles and the
  engine's conversation state live in `*_enc` `BLOB`/`MEDIUMBLOB` columns whose check accepts only
  an AWS Encryption SDK message (first byte 1 or 2) encrypted under the tenant's KMS key. Plain
  text cannot be stored there by mistake. Everything else is ids, labels, numbers and times.
- **S3 keys in columns** are `*_s3_key`, checked to start with `tenants/{slug}/` (decision #41).
- **Times** are `DATETIME(6)` in UTC (PlanetScale's time zone, which cannot be changed). Text
  compares exactly (`utf8mb4_0900_bin`). Every table is `ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_0900_bin` with a primary key.
- **No name is a SQL keyword or reserved word** (decision #43): not a table, column, index,
  constraint or alias, in any letter case. The word lists are in
  `packages/rudi_platform/db/keywords.py` (all PostgreSQL keywords, even unreserved ones, plus the
  reserved words of the SQL standard, Oracle, MySQL and SQL Server). Make the name specific
  instead: `display_name`, `role_key`, `version_number`, `occurred_at`. Never quote it or add an
  underscore. Check a name before using it:

  ```cmd
  python -c "import sys; sys.path.insert(0, 'packages'); from rudi_platform.db.keywords import clashes; print(clashes('name'))"
  ```

## Regenerating the seed

From the repository root (written from Python, so the file keeps LF line ends and no byte-order
mark):

```cmd
python -c "import sys; sys.path.insert(0, 'packages'); from rudi_platform.access import seed; open('db/migrations/0002_seed_access.sql', 'wb').write(seed.render().encode('utf-8'))"
```

Until 0002 has run anywhere, regenerate it whenever the catalogue changes. After that, catalogue
changes need a new migration that inserts the new rows.

## Checks

- `packages/rudi_platform/tests/test_schema.py` runs with the platform tests: numbering, area
  names and table options, id prefixes and reference checks, a `tenant_id` column on every table
  holding tenant data and every index leading with it, the guard's lists matching the schema, no
  migration writing a tenant table, ciphertext-only `*_enc` columns, tenant-folder S3 key
  columns, no name that is a SQL keyword (the runner's `meta_migrations` included), the runner
  accepting the set, and the seed matching the catalogue.
- `packages/rudi_platform/tests/test_tenant_guard.py`: the guard, including the ways a statement
  could name its tenant without restricting its rows.
- `packages/rudi_platform/tests/test_migrate.py`: the runner (refusals, statement-by-statement
  progress, resume, lock, drift) and the connection secret. `services/db-migrate/tests`: the
  Lambda around it.
- The real server's check is `python db/migrate.py apply` itself: it stops at the first statement
  PlanetScale refuses and says which.
