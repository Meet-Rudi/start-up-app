"""
MEET_RUDI platform — applying db/migrations to the platform database (PlanetScale Vitess, MySQL 8;
decision #46).

Migrations are numbered SQL files, never edited once they have run anywhere (db/README.md). This
module finds them, refuses what PlanetScale cannot run, compares them with what the database
recorded in `meta_migrations`, and applies the pending ones.

MySQL commits every schema change on its own, so a migration cannot land whole or not at all.
Instead:

  - A migration runs statement by statement, and its row in `meta_migrations` counts the
    statements done. A failure stops the run and leaves the row `failed`, with the error.
  - The next `apply` resumes a failed migration at the statement that failed, provided the
    statements that already ran are unchanged in the file (their checksum is kept). Fixing the
    failed statement, or a later one, is therefore allowed; changing one that ran is not.
  - The row is also the lock: a second runner finds it `running` and stops. A `running` row
    older than STALE_AFTER_S belongs to a run that died (the runner's Lambda stops at 300 s) and
    is resumed like a failed one.
  - An applied migration whose file changed, or whose file is gone, stops everything before
    anything runs ("drift").
  - `{new_id:cat}` in a migration becomes a fresh id from rudi_platform.ids each time its
    statement runs (the seed; MySQL on PlanetScale has no stored functions to mint one).

The connection is injected: anything with `query(sql, params)` returning rows and
`execute(sql, params)` returning the number of affected rows, in autocommit mode, with `%s`
placeholders (PyMySQL). Migration statements are sent without parameters, so a `%` in them is
literal.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import secrets
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ..ids import Cat, new_id

MIGRATION_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
NEW_ID_RE = re.compile(r"\{new_id:([a-z]{3})\}")
STALE_AFTER_S = 600
_MINTABLE = {c.value for c in Cat if c is not Cat.USER}  # end-user ids are derived, never minted

# What a migration may not contain on PlanetScale Vitess (decision #46), and why. Checked on the
# code with comments removed and string literals emptied.
_REFUSED = [
    (
        r"^\s*(BEGIN|COMMIT|ROLLBACK|START\s+TRANSACTION|SAVEPOINT)\b",
        "controls a transaction; MySQL commits each schema change on its own",
    ),
    (
        (
            r"\bCREATE\s+(OR\s+REPLACE\s+)?(DEFINER\s*=\s*\S+\s+)?"
            r"(PROCEDURE|FUNCTION|TRIGGER|EVENT|VIEW)\b"
        ),
        (
            "creates a stored routine, trigger, event or view: PlanetScale runs no stored "
            "routines, triggers or events, and the platform uses no views"
        ),
    ),
    (
        r"\bFOREIGN\s+KEY\b|\bREFERENCES\b",
        (
            "declares a foreign key: they stop working at the first shard, so the stores keep "
            "references"
        ),
    ),
    (
        r"\b(CREATE|DROP)\s+(DATABASE|SCHEMA)\b|^\s*USE\s",
        "changes database: PlanetScale has one per branch, and tables are named by area instead",
    ),
    (r"\bRENAME\b", "renames: deploy requests refuse renames, so add, copy and drop instead"),
    (r"\bAUTO_INCREMENT\b", "uses AUTO_INCREMENT: ids are minted by rudi_platform.ids"),
    (r"^\s*DELIMITER\b", "sets a DELIMITER, which only the mysql client understands"),
    (r"\bLOAD\s+DATA\b", "loads a file, which PlanetScale refuses"),
    (
        r"\b(GRANT|REVOKE)\b|\bCREATE\s+USER\b",
        "manages users: PlanetScale passwords are made in its console",
    ),
    (r"\bSET\s+GLOBAL\b", "sets a global variable, which PlanetScale does not allow"),
]

BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS meta_migrations (
  id                CHAR(36) NOT NULL CHECK (REGEXP_LIKE(id, '^mig_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  migration_number  INT NOT NULL CHECK (migration_number > 0),
  migration_name    VARCHAR(128) NOT NULL,
  checksum          CHAR(64) NOT NULL CHECK (REGEXP_LIKE(checksum, '^[0-9a-f]{64}$', 'c')),
  status            VARCHAR(16) NOT NULL CHECK (status IN ('running', 'applied', 'failed')),
  statements_total  INT NOT NULL CHECK (statements_total > 0),
  statements_done   INT NOT NULL DEFAULT 0 CHECK (statements_done >= 0),
  done_checksum     CHAR(64) NOT NULL CHECK (REGEXP_LIKE(done_checksum, '^[0-9a-f]{64}$', 'c')),
  claim_token       CHAR(32) NOT NULL CHECK (REGEXP_LIKE(claim_token, '^[0-9a-f]{32}$', 'c')),
  last_error        VARCHAR(1000),
  started_at        DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  applied_at        DATETIME(6),
  applied_by        VARCHAR(255) NOT NULL DEFAULT '',
  duration_ms       INT NOT NULL DEFAULT 0 CHECK (duration_ms >= 0),
  release_tag       VARCHAR(64) NOT NULL DEFAULT '',
  PRIMARY KEY (id),
  UNIQUE KEY migrations_number_uq (migration_number)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin
"""

STATE_SQL = (
    "SELECT migration_number, migration_name, checksum, status, statements_done, "
    "statements_total, done_checksum, claim_token, COALESCE(last_error, ''), "
    "TIMESTAMPDIFF(SECOND, started_at, UTC_TIMESTAMP(6)), applied_at, applied_by "
    "FROM meta_migrations ORDER BY migration_number"
)

START_SQL = (
    "INSERT INTO meta_migrations (id, migration_number, migration_name, checksum, status, "
    "statements_total, statements_done, done_checksum, claim_token, applied_by, release_tag) "
    "VALUES (%s, %s, %s, %s, 'running', %s, 0, %s, %s, %s, %s)"
)

RESUME_SQL = (
    "UPDATE meta_migrations SET status = 'running', started_at = UTC_TIMESTAMP(6), "
    "migration_name = %s, checksum = %s, statements_total = %s, claim_token = %s, "
    "applied_by = %s, release_tag = %s WHERE migration_number = %s AND claim_token = %s"
)

PROGRESS_SQL = (
    "UPDATE meta_migrations SET statements_done = %s, done_checksum = %s "
    "WHERE migration_number = %s AND claim_token = %s"
)

FAILED_SQL = (
    "UPDATE meta_migrations SET status = 'failed', last_error = %s "
    "WHERE migration_number = %s AND claim_token = %s"
)

DONE_SQL = (
    "UPDATE meta_migrations SET status = 'applied', applied_at = UTC_TIMESTAMP(6), "
    "duration_ms = duration_ms + %s, last_error = NULL "
    "WHERE migration_number = %s AND claim_token = %s"
)


class Database(Protocol):
    def query(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]: ...
    def execute(self, sql: str, params: Sequence[Any] = ()) -> int: ...


class MigrationError(RuntimeError):
    """The migrations on disk or in the database are not in a state that may be applied."""


class MigrationFailed(RuntimeError):
    """A statement raised. The statements before it in the same migration stay applied."""

    def __init__(self, migration: Migration, error: BaseException, done: int) -> None:
        self.migration = migration
        self.error = error
        self.done = done
        total = len(migration.statements)
        kept = f"statements 1–{done} stay applied" if done else "nothing of it was applied"
        super().__init__(
            f"{migration.name} stopped at statement {done + 1} of {total}: "
            f"{describe_error(error)}. {kept}; fix that statement and run apply again, "
            "it resumes there"
        )


@dataclass(frozen=True)
class Migration:
    number: int
    # The file name without .sql, e.g. 0001_registry_access_control_audit_trail.
    name: str
    sql: str
    checksum: str  # SHA-256 of the text with LF line ends, so Windows and Linux agree
    statements: tuple[str, ...]


@dataclass(frozen=True)
class Applied:
    """A migration's row in meta_migrations."""

    number: int
    name: str
    checksum: str
    status: str  # applied | running | failed
    statements_done: int
    statements_total: int
    done_checksum: str
    claim_token: str
    last_error: str
    age_s: int  # seconds since its run started
    applied_at: str
    applied_by: str


@dataclass(frozen=True)
class Ran:
    number: int
    name: str
    duration_ms: int
    resumed_at: int = 0  # statements that had run before this apply


@dataclass(frozen=True)
class Plan:
    applied: list[Applied]
    unfinished: list[Applied]  # failed or running: apply resumes them
    pending: list[Migration]  # every migration not applied, unfinished ones included
    drift: list[str]


def describe_error(error: BaseException) -> str:
    """A database error as one short line: message and MySQL error number."""
    args = error.args
    if len(args) >= 2 and isinstance(args[0], int):
        text = f"{args[1]} (MySQL error {args[0]})"
    else:
        text = f"{type(error).__name__}: {error}"
    return " ".join(text.split())[:500]


def checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def done_checksum(statements: Sequence[str], done: int) -> str:
    """The checksum of the first `done` statements: what a resumed migration must not change."""
    return checksum("\n;\n".join(statements[:done]))


# --------------------------------------------------------------------------- reading the files
def _scan(text: str) -> tuple[str, list[str], bool]:
    """(code without -- comments, statements split on ; outside quotes, unterminated quote)."""
    code: list[str] = []
    statements: list[str] = []
    current: list[str] = []
    quote = ""
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if quote:
            current.append(ch)
            code.append(ch)
            if ch == quote:
                if i + 1 < n and text[i + 1] == quote:  # '' inside a string
                    current.append(ch)
                    code.append(ch)
                    i += 2
                    continue
                quote = ""
            i += 1
            continue
        if text.startswith("--", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue
        if ch in "'\"`":
            quote = ch
        if ch == ";":
            statements.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
        code.append(ch)
        i += 1
    statements.append("".join(current).strip())
    return "".join(code), [s for s in statements if s], bool(quote)


def split_statements(text: str) -> list[str]:
    """The statements of a migration, without comments, in order."""
    return _scan(text)[1]


def _problems(name: str, text: str) -> list[str]:
    code, statements, open_quote = _scan(text)
    found = []
    if "\\" in text:
        found.append(
            f"{name} contains a backslash: MySQL reads it as an escape inside a string "
            "(write [.] for a literal dot in a pattern)"
        )
    if open_quote:
        found.append(f"{name} has a quote that is never closed")
    if "/*" in code:
        found.append(f"{name} has a /* */ comment: MySQL runs /*! … */ as code; use --")
    if not statements:
        found.append(f"{name} is empty")
    plain = re.sub(r"'(?:[^']|'')*'", "''", code)
    for pattern, reason in _REFUSED:
        if re.search(pattern, plain, re.IGNORECASE | re.MULTILINE):
            found.append(f"{name} {reason}")
    for cat in NEW_ID_RE.findall(code):
        if cat not in _MINTABLE:
            found.append(f"{name}: {{new_id:{cat}}} is not a category the runner may mint")
    return found


def load(directory: str) -> list[Migration]:
    """Every migration in `directory`, in order. Raises MigrationError on a bad set."""
    files = sorted(f for f in os.listdir(directory) if f.endswith(".sql"))
    if not files:
        raise MigrationError(f"no migrations in {directory}")
    out: list[Migration] = []
    problems: list[str] = []
    for i, file in enumerate(files, start=1):
        parsed = MIGRATION_RE.match(file)
        if not parsed or int(parsed.group(1)) != i:
            raise MigrationError(
                f"{file}: expected {i:04d}_<name>.sql (numbers run 0001, 0002, … with no gaps)"
            )
        with open(os.path.join(directory, file), encoding="utf-8-sig") as fh:
            text = fh.read().replace("\r\n", "\n")
        name = file[: -len(".sql")]
        problems += _problems(name, text)
        out.append(Migration(i, name, text, checksum(text), tuple(split_statements(text))))
    if problems:
        raise MigrationError("; ".join(problems))
    return out


def with_ids(statement: str) -> str:
    """The statement with every {new_id:cat} replaced by a fresh, quoted id."""
    return NEW_ID_RE.sub(lambda m: f"'{new_id(Cat(m.group(1)))}'", statement)


# ------------------------------------------------------------------- the PlanetScale console
CONSOLE_BY = "planetscale-console"


def record_sql(m: Migration) -> list[str]:
    """The statements that record `m` as applied in meta_migrations (creating the table the first
    time), for a migration applied by hand in the PlanetScale console."""
    total = len(m.statements)
    record = (
        "INSERT INTO meta_migrations (id, migration_number, migration_name, checksum, status, "
        "statements_total, statements_done, done_checksum, claim_token, applied_at, applied_by) "
        f"VALUES ('{new_id(Cat.MIGRATION)}', {m.number}, '{m.name}', '{m.checksum}', 'applied', "
        f"{total}, {total}, '{done_checksum(m.statements, total)}', '{secrets.token_hex(16)}', "
        f"UTC_TIMESTAMP(6), '{CONSOLE_BY}')"
    )
    return [BOOTSTRAP.strip(), record]


def console_script(m: Migration, *, record_only: bool = False) -> str:
    """`m` ready to paste into the PlanetScale console: every {new_id:cat} minted, each statement
    ending in `;`, then the statements that record it (record_only: those alone, for a migration
    already applied by hand)."""
    head = [
        f"-- {m.name}, prepared for the PlanetScale console by db/console_sql.py.",
        "-- Run the statements in order. Ids were minted for this file: apply it once; if you",
        "-- need it again, generate it again. A statement that already ran fails on its unique",
        "-- key when repeated, so a repeat changes nothing.",
    ]
    body: list[str] = []
    if not record_only:
        body += [with_ids(s) + ";" for s in m.statements]
    head.append(
        "-- The last two statements record the migration in meta_migrations: run them only after"
        if not record_only
        else "-- Records a migration that was applied by hand: run only if every statement of it"
    )
    head.append(
        "-- every statement before them succeeded."
        if not record_only
        else "-- succeeded, and the file has not changed since."
    )
    body += [s + ";" for s in record_sql(m)]
    return "\n".join(head) + "\n\n" + "\n\n".join(body) + "\n"


# ---------------------------------------------------------------------- what the database holds
def _text(value: Any) -> str:
    return "" if value is None else str(value)


def read_applied(db: Database) -> list[Applied]:
    """What the database recorded; empty on a database no runner has touched."""
    if not db.query("SHOW TABLES LIKE 'meta_migrations'"):
        return []
    return [
        Applied(
            number=int(r[0]),
            name=r[1],
            checksum=r[2],
            status=r[3],
            statements_done=int(r[4]),
            statements_total=int(r[5]),
            done_checksum=r[6],
            claim_token=r[7],
            last_error=_text(r[8]),
            age_s=int(r[9] or 0),
            applied_at=_text(r[10]),
            applied_by=_text(r[11]),
        )
        for r in db.query(STATE_SQL)
    ]


def plan(migrations: Sequence[Migration], applied: Sequence[Applied]) -> Plan:
    files = {m.number: m for m in migrations}
    drift = []
    done, unfinished = [], []
    for a in applied:
        m = files.get(a.number)
        if a.status == "applied":
            done.append(a)
            if m is None:
                drift.append(f"{a.number:04d} ({a.name}) is applied but its file is gone")
            elif m.name != a.name:
                drift.append(
                    f"{a.number:04d} was applied as {a.name}; the file is now {m.name}"
                )
            elif m.checksum != a.checksum:
                drift.append(
                    f"{m.name} changed after it was applied: never edit an applied "
                    "migration, add the next number"
                )
            continue
        unfinished.append(a)
        stopped = f"stopped after statement {a.statements_done} of {a.statements_total}"
        if m is None:
            drift.append(f"{a.number:04d} ({a.name}) {stopped} and its file is gone")
        elif m.name != a.name:
            drift.append(f"{a.number:04d} {stopped} as {a.name}; the file is now {m.name}")
        elif a.statements_done > len(m.statements) or done_checksum(
            m.statements, a.statements_done
        ) != a.done_checksum:
            drift.append(
                f"{m.name} {stopped}, and a statement that already ran has changed since: "
                "undo what it applied by hand and delete its meta_migrations row, or restore "
                "the statements that ran"
            )
    finished = {a.number for a in done}
    pending = [m for m in migrations if m.number not in finished]
    return Plan(done, unfinished, pending, drift)


def _checked_plan(db: Database, migrations: Sequence[Migration]) -> Plan:
    current = plan(migrations, read_applied(db))
    if current.drift:
        raise MigrationError("; ".join(current.drift))
    return current


# ------------------------------------------------------------------------------------ applying
def _claim(
    db: Database, m: Migration, row: Applied | None, release: str, applied_by: str
) -> tuple[int, str]:
    """Take the migration's row for this run: (statements already done, this run's token)."""
    token = secrets.token_hex(16)
    if row is None:
        try:
            db.execute(
                START_SQL,
                (
                    new_id(Cat.MIGRATION),
                    m.number,
                    m.name,
                    m.checksum,
                    len(m.statements),
                    done_checksum(m.statements, 0),
                    token,
                    applied_by,
                    release,
                ),
            )
        except Exception as e:
            raise MigrationError(
                f"{m.name}: could not record its start (another runner?): {describe_error(e)}"
            ) from e
        return 0, token
    if row.status == "running" and row.age_s < STALE_AFTER_S:
        raise MigrationError(
            f"{m.name} is being applied by another run, started {row.age_s} s ago"
        )
    claimed = db.execute(
        RESUME_SQL,
        (
            m.name,
            m.checksum,
            len(m.statements),
            token,
            applied_by,
            release,
            m.number,
            row.claim_token,
        ),
    )
    if claimed != 1:
        raise MigrationError(f"{m.name} was taken by another run while starting; run plan")
    return row.statements_done, token


def apply(
    db: Database,
    migrations: Sequence[Migration],
    *,
    upto: int | None = None,
    release: str = "",
    applied_by: str = "",
    clock: Callable[[], float] = time.monotonic,
) -> list[Ran]:
    """Apply the pending migrations (up to number `upto`), statement by statement.

    Resumes an unfinished one where it stopped. Stops at the first failure, raised as
    MigrationFailed; what ran before it stays applied and recorded.
    """
    current = _checked_plan(db, migrations)
    targets = [m for m in current.pending if upto is None or m.number <= upto]
    if not targets:
        return []
    db.execute(BOOTSTRAP)
    rows = {a.number: a for a in current.unfinished}
    ran: list[Ran] = []
    for m in targets:
        start_at, token = _claim(db, m, rows.get(m.number), release, applied_by)
        started = clock()
        done = start_at
        try:
            for i in range(start_at, len(m.statements)):
                db.execute(with_ids(m.statements[i]))
                done = i + 1
                db.execute(
                    PROGRESS_SQL,
                    (done, done_checksum(m.statements, done), m.number, token),
                )
        except Exception as e:
            # The connection may be gone; the error being raised matters more than this one.
            with contextlib.suppress(Exception):
                db.execute(FAILED_SQL, (describe_error(e)[:1000], m.number, token))
            raise MigrationFailed(m, e, done) from e
        duration_ms = int((clock() - started) * 1000)
        db.execute(DONE_SQL, (duration_ms, m.number, token))
        ran.append(Ran(m.number, m.name, duration_ms, start_at))
    return ran


def server_report(db: Database) -> dict[str, Any]:
    """What the connection reaches: the server, the database (PlanetScale: the branch's
    keyspace) and how many tables it holds."""
    version, database = db.query("SELECT VERSION(), DATABASE()")[0]
    version = _text(version)
    return {
        "server_version": version,
        "database": _text(database),
        "vitess": "vitess" in version.lower(),
        "mysql_8": version.startswith("8."),
        "tables": len(db.query("SHOW TABLES")),
    }
