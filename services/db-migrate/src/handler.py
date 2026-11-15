"""
meetrudi-internal-prod-db-migrate — applies db/migrations to the platform database
(PlanetScale Vitess, MySQL 8, decision #46; runner and schema: decision #42).

Invoke-only: no URL, no schedule. Run it with `python db/migrate.py <action>` from the repo.

Event: {"action": "plan" | "apply" | "check", "upto": 3, "confirm": "apply"}

    plan   (default) what the database recorded, what is pending or unfinished, drift, and the
           server it reached. Reads only.
    apply  applies the pending migrations statement by statement, resuming an unfinished one
           where it stopped (up to `upto` if given). Needs "confirm": "apply".
    check  connects with each configured secret and reports the server and database it reaches.
           Reads only.

This is the one process that connects to PlanetScale without the tenant guard
(rudi_platform.db.tenant_guard, decision #47): it changes the schema and seeds platform tables,
and test_schema.py checks that no migration writes a tenant's rows.

The answer never holds a password or a full host name (ConnectionInfo.describe), and the log
lines carry the action and counts only.
"""

from __future__ import annotations

import json
import logging
import os
import ssl
from collections.abc import Sequence
from typing import Any

from rudi_platform.db import (
    MigrationError,
    MigrationFailed,
    apply,
    load,
    parse_secret,
    plan,
    read_applied,
    server_report,
)
from rudi_platform.db.migrate import describe_error

log = logging.getLogger()
log.setLevel(logging.INFO)

ACTIONS = ("plan", "apply", "check")
# apply writes with SECRET (PlanetScale's Admin role); plan only reads, so it uses PLAN_SECRET,
# which needs no more than the Reader role.
SECRET = os.environ.get("DB_SECRET", "meetrudi/planetscale-backbone/primary-main-write")
PLAN_SECRET = os.environ.get("DB_PLAN_SECRET", SECRET)
CHECK_SECRETS = [s.strip() for s in os.environ.get("DB_CHECK_SECRETS", SECRET).split(",") if s.strip()]
MIGRATIONS_DIR = os.environ.get("MIGRATIONS_DIR", "/opt/migrations")
# Where the database is, for secrets that hold only a user and a password (deploy.local.json:
# planetscale_host, planetscale_port, planetscale_database).
CONNECTION_DEFAULTS = {
    "host": os.environ.get("DB_HOST", ""),
    "port": os.environ.get("DB_PORT", ""),
    "database": os.environ.get("DB_NAME", ""),
}
RELEASE = os.environ.get("RELEASE", "")
REGION = os.environ.get("AWS_REGION", "eu-central-1")
APP_NAME = "meetrudi-db-migrate"
STATEMENT_TIMEOUT_S = 120  # a statement that has not answered by then fails, and apply stops

_secrets_client = None


def _fetch_secret(name: str) -> str:
    global _secrets_client
    if _secrets_client is None:
        import boto3

        _secrets_client = boto3.client("secretsmanager", region_name=REGION)
    return _secrets_client.get_secret_value(SecretId=name)["SecretString"]


def _ssl_context() -> ssl.SSLContext:
    """Verified TLS (certificate and host name), with certifi's CA bundle when present."""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


class MySqlDatabase:
    """A PyMySQL connection as rudi_platform.db.migrate.Database: autocommit, %s placeholders."""

    def __init__(self, conn) -> None:
        self._conn = conn

    # Without parameters PyMySQL sends the text as it is, so a % in a migration stays literal.
    def query(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        with self._conn.cursor() as cursor:
            cursor.execute(sql, tuple(params) if params else None)
            return [tuple(row) for row in cursor.fetchall()]

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self._conn.cursor() as cursor:
            cursor.execute(sql, tuple(params) if params else None)
            return int(cursor.rowcount)

    def close(self) -> None:
        self._conn.close()


def _open(info) -> MySqlDatabase:
    import pymysql

    return MySqlDatabase(
        pymysql.connect(
            host=info.host,
            port=info.port,
            user=info.user,
            password=info.password,
            database=info.database,
            ssl=_ssl_context(),
            connect_timeout=10,
            read_timeout=STATEMENT_TIMEOUT_S,
            write_timeout=STATEMENT_TIMEOUT_S,
            autocommit=True,
            charset="utf8mb4",
            program_name=APP_NAME,
        )
    )


class _Stage(Exception):
    def __init__(self, stage: str, error: BaseException) -> None:
        super().__init__(stage)
        self.stage = stage
        self.error = error


def _connect(secret: str):
    try:
        info = parse_secret(_fetch_secret(secret), CONNECTION_DEFAULTS)
    except Exception as e:
        if "no host" in str(e) or "no database" in str(e):
            e = ValueError(
                f"{e}; set planetscale_host and planetscale_database in deploy.local.json, "
                "then python deploy.py db-migrate"
            )
        raise _Stage("secret", e) from e
    try:
        return info, _open(info)
    except Exception as e:
        raise _Stage("connect", e) from e


def _close(conn) -> None:
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        log.info("DB_MIGRATE close failed")


def _warnings(server: dict[str, Any]) -> list[str]:
    out = []
    if not server["mysql_8"]:
        out.append(
            f"the server reports version {server['server_version']}, not MySQL 8: is this the "
            "PlanetScale Vitess database (decision #46)?"
        )
    return out


def _hint(error_text: str) -> str | None:
    """What a refused schema change most likely means on PlanetScale."""
    text = error_text.lower()
    if "ddl" in text or "safe migration" in text:
        return (
            "PlanetScale refused a schema change on this branch: safe migrations is on. Apply to "
            "a branch with safe migrations off (db/README.md, 'Branches')"
        )
    if "denied" in text:
        return (
            "the password's role does not allow this statement: plan needs a password that may "
            "read (Reader or above), apply one with the Admin role (db/README.md, 'Passwords')"
        )
    return None


def _status(conn, migrations) -> dict[str, Any]:
    current = plan(migrations, read_applied(conn))
    return {
        "applied": [
            {"name": a.name, "applied_at": a.applied_at, "applied_by": a.applied_by}
            for a in current.applied
        ],
        "unfinished": [
            {
                "name": a.name,
                "status": a.status,
                "statements_done": a.statements_done,
                "statements_total": a.statements_total,
                "last_error": a.last_error,
            }
            for a in current.unfinished
        ],
        "pending": [m.name for m in current.pending],
        "drift": current.drift,
    }


def _ran(ran) -> list[dict[str, Any]]:
    return [
        {"name": r.name, "duration_ms": r.duration_ms, "resumed_at_statement": r.resumed_at + 1}
        if r.resumed_at
        else {"name": r.name, "duration_ms": r.duration_ms}
        for r in ran
    ]


def _check() -> dict[str, Any]:
    reports = []
    for secret in CHECK_SECRETS:
        entry: dict[str, Any] = {"secret": secret}
        try:
            info, conn = _connect(secret)
        except _Stage as e:
            entry.update(ok=False, stage=e.stage, error=describe_error(e.error))
            reports.append(entry)
            continue
        try:
            server = server_report(conn)
            entry.update(
                ok=True, connection=info.describe(), server=server, warnings=_warnings(server)
            )
        except Exception as e:  # noqa: BLE001
            entry.update(ok=False, stage="query", error=describe_error(e))
        finally:
            _close(conn)
        reports.append(entry)
    return {"ok": all(r["ok"] for r in reports), "secrets": reports}


def _migrations_action(action: str, event: dict[str, Any]) -> dict[str, Any]:
    if action == "apply" and event.get("confirm") != "apply":
        return {"ok": False, "error": 'apply needs "confirm": "apply"'}
    upto = event.get("upto")
    if upto is not None and (not isinstance(upto, int) or isinstance(upto, bool) or upto < 1):
        return {"ok": False, "error": "upto must be a migration number"}
    migrations = load(MIGRATIONS_DIR)
    secret = SECRET if action == "apply" else PLAN_SECRET
    info, conn = _connect(secret)
    try:
        server = server_report(conn)
        out: dict[str, Any] = {
            "secret": secret,
            "connection": info.describe(),
            "server": server,
            "warnings": _warnings(server),
            "migrations": len(migrations),
        }
        if action == "apply":
            try:
                out["ran"] = _ran(
                    apply(conn, migrations, upto=upto, release=RELEASE, applied_by=APP_NAME)
                )
            except MigrationFailed as e:
                out.update(_status(conn, migrations))
                out.update(ok=False, failed=e.migration.name, error=str(e))
                hint = _hint(str(e))
                if hint:
                    out["hint"] = hint
                return out
        out.update(_status(conn, migrations))
        out["ok"] = not out["drift"]
        return out
    finally:
        _close(conn)


def handler(event, context=None):
    event = event if isinstance(event, dict) else {}
    action = event.get("action") or "plan"
    if action not in ACTIONS:
        return {"ok": False, "error": f"action must be one of {', '.join(ACTIONS)}"}
    try:
        result = _check() if action == "check" else _migrations_action(action, event)
    except _Stage as e:
        result = {"ok": False, "stage": e.stage, "error": describe_error(e.error)}
    except MigrationFailed as e:
        result = {"ok": False, "failed": e.migration.name, "error": str(e)}
    except MigrationError as e:
        result = {"ok": False, "stage": "migrations", "error": str(e)}
    except Exception as e:  # noqa: BLE001 - a statement the server refused: say so, do not crash
        result = {"ok": False, "stage": "query", "error": describe_error(e)}
        hint = _hint(result["error"])
        if hint:
            result["hint"] = hint
    result = {"action": action, "release": RELEASE, **result}
    log.info(
        "DB_MIGRATE %s",
        json.dumps(
            {
                "action": action,
                "ok": result.get("ok"),
                "stage": result.get("stage"),
                "applied": len(result.get("applied", [])),
                "unfinished": len(result.get("unfinished", [])),
                "pending": len(result.get("pending", [])),
                "ran": len(result.get("ran", [])),
                "failed": result.get("failed"),
            }
        ),
    )
    return result
