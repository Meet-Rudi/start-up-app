# Platform database migrations

Schema for the platform's system of record: **PlanetScale Postgres**, eu-central-1, Postgres 17+
(decision #30). Design: [docs/platform/blueprint.md](../docs/platform/blueprint.md) §7.

| File | What it does |
|---|---|
| `migrations/0001_registry_access_audit.sql` | Schemas `registry`, `access`, `audit`. Id domain and helpers, all tables, immutability and scope triggers, indexes, row-level security. |
| `migrations/0002_seed_access.sql` | Seed permissions, seed roles and the pooled cell. **Generated** from `packages/rudi_platform/access/catalogue.py`. Do not edit by hand. |

## Rules

- **Never edit a migration that has run anywhere.** Add the next number (`0003_…`) instead.
  Files are numbered contiguously from `0001`; a test checks this.
- **Ids** use the `public.rudi_id` domain plus a `left(id, 4) = 'xxx_'` check with the table's
  registered prefix (docs/platform/conventions.md §1). The application mints ids with
  `rudi_platform.ids`; `public.rudi_new_id(prefix)` is for migrations and seeds only.
- **Tenant isolation:** every table with a `tenant_id` has row-level security, forced for the
  owner too. Inside each transaction the application sets one of:

  ```sql
  SET LOCAL rudi.tenant_id = 'ten_...';   -- tenant-scoped work
  SET LOCAL rudi.platform = 'on';         -- platform work: console admin, jobs, migrations
  ```

  With neither, queries see no tenant rows. The application must connect as a role **without
  BYPASSRLS** and not as a superuser, or the policies are skipped.
- **Published clone versions and their personas are immutable** (triggers). Changes mean a new
  clone version.
- **The audit log is append-only** (trigger). It holds ids and decisions only.

## Regenerating the seed

From the repository root:

```cmd
cd packages
python -m rudi_platform.access.seed > ..\db\migrations\0002_seed_access.sql
cd ..
```

Until 0002 has run anywhere, regenerate it whenever the catalogue changes. After that, catalogue
changes need a new migration that inserts the new rows.

## Checks

- `packages/rudi_platform/tests/test_schema.py` runs with the platform tests: numbering, id
  prefixes, row-level security on every tenant table, and the seed matching the catalogue.
- Syntax is checked with a Postgres parser (`pglast`) when a migration changes. Nothing here has
  run against a real database yet; that needs the PlanetScale dev database (USER-TODO.md #3).
  A migration runner comes with it.
