# db-migrate — the platform database's migration runner

The Lambda **`meetrudi-internal-prod-db-migrate`** applies [`db/migrations`](../../db/migrations)
to the platform database, PlanetScale Vitess, MySQL 8 (decisions #42 and #46). How to use it and
what it guarantees: [db/README.md](../../db/README.md).

```cmd
python deploy.py db-migrate
python db/migrate.py check
python db/migrate.py plan
python db/migrate.py apply
```

| Piece | What it is |
|---|---|
| `src/handler.py` | Fetches the secret, opens a verified-TLS PyMySQL connection (autocommit, 120 s per statement), runs one step (`check`, `plan`, `apply`) and closes it. Answers with names, counts and a hint when PlanetScale refuses a schema change; never a password or a full host. |
| `build_layer.py` | Builds the layer `meetrudi-internal-prod-db-migrate-deps`: PyMySQL and certifi for Lambda's platform, `packages/rudi_platform` (the runner), and a copy of `db/migrations` at `/opt/migrations`. Fails if the layer cannot load the runner or the runner refuses the migrations. |
| `template.yaml` | The function (invoke-only, 300 s, the legacy role `meetrudi-lambda-runner`, which may read `meetrudi*` secrets) and its layer. Secret names in the environment; host, port and database from `deploy.local.json`. |
| `tests/` | `python -m unittest discover -s services/db-migrate/tests -v` |

The Lambda applies the migrations it was **deployed with**: after adding a migration, run
`python deploy.py db-migrate` before `python db/migrate.py plan`.

It is the one process that connects to PlanetScale without the tenant guard
(`rudi_platform.db.tenant_guard`, decision #47): it changes the schema and seeds platform tables,
and `test_schema.py` checks that no migration writes a tenant's rows.
