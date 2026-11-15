"""
The migration runner and the connection secret (PlanetScale Vitess, MySQL 8; decision #46).

What this guards: a statement PlanetScale cannot run reaching the server, an edited migration
applied over the old one, a failed migration resumed after a statement that already ran was
changed, two runners applying at once, a failure reported as success, and a secret's password
reaching an error message or a log.

Run:  python -m unittest discover -s packages/rudi_platform/tests -v
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..")))

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
from rudi_platform.db import migrate as m
from rudi_platform.db.migrate import Applied, checksum, describe_error, done_checksum


class FakeError(Exception):
    """Shaped like PyMySQL's errors: (MySQL error number, message)."""


class FakeDb:
    """MySQL as far as the runner sees it: every statement commits on its own."""

    def __init__(self, fail_on=None, version="8.0.34-Vitess"):
        self.table = False
        self.rows: dict[int, dict] = {}
        self.fail_on = fail_on
        self.version = version
        self.executed: list[str] = []  # migration statements, in the order they ran
        self.log: list[str] = []  # which runner statement ran: bootstrap, start, progress…
        self.claim_hook = None  # called before a resume, to play another runner

    # -- reads
    def query(self, sql, params=()):
        if sql == "SHOW TABLES LIKE 'meta_migrations'":
            return [("meta_migrations",)] if self.table else []
        if sql == m.STATE_SQL:
            return [
                (
                    r["number"], r["name"], r["checksum"], r["status"], r["done"], r["total"],
                    r["done_checksum"], r["token"], r["error"] or "", r["age"],
                    r["applied_at"], r["applied_by"],
                )
                for _, r in sorted(self.rows.items())
            ]
        if sql == "SELECT VERSION(), DATABASE()":
            return [(self.version, "planetscale-backbone")]
        if sql == "SHOW TABLES":
            return [("meta_migrations",)] if self.table else []
        raise AssertionError(f"unexpected query: {sql[:60]}")

    # -- writes
    def execute(self, sql, params=()):
        if sql == m.BOOTSTRAP:
            self.log.append("bootstrap")
            self.table = True
            return 0
        if sql == m.START_SQL:
            self.log.append("start")
            assert self.table, "a record was written before the table exists"
            rid, number, name, digest, total, done_digest, token, by, release = params
            assert rid.startswith("mig_")
            if number in self.rows:
                raise FakeError(1062, f"Duplicate entry '{number}' for key 'migrations_number_uq'")
            self.rows[number] = {
                "number": number, "name": name, "checksum": digest, "status": "running",
                "done": 0, "total": total, "done_checksum": done_digest, "token": token,
                "error": None, "age": 0, "applied_at": None, "applied_by": by,
                "release": release,
            }
            return 1
        if sql == m.RESUME_SQL:
            self.log.append("resume")
            if self.claim_hook:
                self.claim_hook(self)
            name, digest, total, token, by, release, number, old_token = params
            r = self.rows.get(number)
            if not r or r["token"] != old_token:
                return 0
            r.update(status="running", name=name, checksum=digest, total=total, token=token,
                     applied_by=by, release=release, age=0)
            return 1
        if sql == m.PROGRESS_SQL:
            done, done_digest, number, token = params
            return self._update(number, token, done=done, done_checksum=done_digest)
        if sql == m.FAILED_SQL:
            self.log.append("failed")
            error, number, token = params
            return self._update(number, token, status="failed", error=error)
        if sql == m.DONE_SQL:
            self.log.append("done")
            _ms, number, token = params
            return self._update(number, token, status="applied", applied_at="2026-11-15", error=None)
        # A migration statement.
        assert not params, "migration statements are sent without parameters"
        if self.fail_on and self.fail_on in sql:
            raise FakeError(1142, "CREATE command denied to user 'app' for table 'a_u'")
        self.executed.append(sql)
        return 0

    def _update(self, number, token, **fields):
        r = self.rows.get(number)
        if not r or r["token"] != token:
            return 0
        r.update(fields)
        return 1


def _dir(files: dict[str, str]) -> str:
    d = tempfile.mkdtemp()
    for name, text in files.items():
        with open(os.path.join(d, name), "w", encoding="utf-8", newline="") as f:
            f.write(text)
    return d


TABLE = "CREATE TABLE {t} (id CHAR(36) NOT NULL, PRIMARY KEY (id)) ENGINE=InnoDB"
THREE = {
    "0001_one.sql": "-- the first table\n" + TABLE.format(t="a_t") + ";\n",
    "0002_two.sql": (
        TABLE.format(t="a_u") + ";\n"
        "CREATE INDEX u_idx ON a_u (id);\n"
        "ALTER TABLE a_u ADD COLUMN note VARCHAR(10);\n"
    ),
    "0003_three.sql": "INSERT INTO a_t (id) VALUES ({new_id:cel});\n",
}


def _applied(migration, status="applied", done=None, age=0, token="0" * 32, digest=None):
    done = len(migration.statements) if done is None else done
    return Applied(
        number=migration.number,
        name=migration.name,
        checksum=digest or migration.checksum,
        status=status,
        statements_done=done,
        statements_total=len(migration.statements),
        done_checksum=done_checksum(migration.statements, done),
        claim_token=token,
        last_error="",
        age_s=age,
        applied_at="",
        applied_by="tester",
    )


class Loading(unittest.TestCase):
    def test_loads_in_order_with_line_end_independent_checksums(self):
        unix = load(_dir(THREE))
        windows = load(_dir({k: v.replace("\n", "\r\n") for k, v in THREE.items()}))
        self.assertEqual([x.number for x in unix], [1, 2, 3])
        self.assertEqual(unix[0].name, "0001_one")
        self.assertEqual([x.checksum for x in unix], [x.checksum for x in windows])
        self.assertEqual(len(unix[1].statements), 3)
        self.assertNotIn("--", unix[0].statements[0])

    def test_a_byte_order_mark_is_ignored(self):
        bom = load(_dir({"0001_one.sql": "﻿" + THREE["0001_one.sql"]}))
        self.assertEqual(bom[0].checksum, load(_dir({"0001_one.sql": THREE["0001_one.sql"]}))[0].checksum)

    def test_numbers_run_without_gaps(self):
        with self.assertRaises(MigrationError):
            load(_dir({"0001_one.sql": "SELECT 1;", "0003_three.sql": "SELECT 1;"}))
        with self.assertRaises(MigrationError):
            load(_dir({"1_one.sql": "SELECT 1;"}))

    def test_what_planetscale_cannot_run_is_refused_before_anything_is_sent(self):
        for text in (
            "START TRANSACTION;",
            "SELECT 1;\nCOMMIT;",
            "CREATE TRIGGER t BEFORE UPDATE ON x FOR EACH ROW SET NEW.a = 1;",
            "CREATE DEFINER = admin PROCEDURE p() SELECT 1;",
            "CREATE VIEW v AS SELECT 1;",
            "ALTER TABLE x ADD CONSTRAINT x_fk FOREIGN KEY (a) REFERENCES y (id);",
            "ALTER TABLE x RENAME COLUMN a TO b;",
            "CREATE TABLE x (id BIGINT AUTO_INCREMENT PRIMARY KEY);",
            "CREATE DATABASE other;",
            "USE other;",
            "GRANT SELECT ON x TO app;",
            "SET GLOBAL sql_mode = '';",
            "SELECT 'a\\.b';",
            "/*! SELECT 1 */ SELECT 2;",
            "SELECT 'never closed;",
            "-- only a comment\n",
            "INSERT INTO x (id) VALUES ({new_id:usr});",
        ):
            with self.subTest(text=text), self.assertRaises(MigrationError):
                load(_dir({"0001_x.sql": text}))

    def test_refused_words_in_strings_and_comments_are_fine(self):
        text = (
            "-- BEGIN; RENAME; a trigger would go here\n"
            "INSERT INTO x (note) VALUES ('rename; references; commit');\n"
        )
        loaded = load(_dir({"0001_x.sql": text}))
        self.assertEqual(len(loaded[0].statements), 1)

    def test_new_id_becomes_a_fresh_id_each_time(self):
        statement = load(_dir(THREE))[2].statements[0]
        first, second = m.with_ids(statement), m.with_ids(statement)
        self.assertNotIn("{new_id:", first)
        self.assertRegex(first, r"'cel_[0-9a-hjkmnp-tv-z]{32}'")
        self.assertNotEqual(first, second)


class Planning(unittest.TestCase):
    def setUp(self):
        self.migrations = load(_dir(THREE))
        self.one, self.two, self.three = self.migrations

    def test_pending_after_the_applied(self):
        p = plan(self.migrations, [_applied(self.one)])
        self.assertEqual([x.number for x in p.pending], [2, 3])
        self.assertEqual((p.drift, p.unfinished), ([], []))

    def test_an_edited_applied_migration_is_drift(self):
        p = plan(self.migrations, [_applied(self.one, digest=checksum("edited"))])
        self.assertIn("changed after it was applied", p.drift[0])

    def test_a_vanished_or_renamed_file_is_drift(self):
        renamed = dataclasses.replace(_applied(self.one), name="0001_old")
        gone = dataclasses.replace(_applied(self.one), number=4, name="0004_gone")
        self.assertEqual(len(plan(self.migrations, [renamed, gone]).drift), 2)

    def test_a_failed_migration_is_unfinished_and_pending(self):
        p = plan(self.migrations, [_applied(self.one), _applied(self.two, "failed", done=1)])
        self.assertEqual([a.number for a in p.unfinished], [2])
        self.assertEqual([x.number for x in p.pending], [2, 3])
        self.assertEqual(p.drift, [])

    def test_fixing_a_statement_that_has_not_run_is_allowed(self):
        fixed = THREE | {"0002_two.sql": THREE["0002_two.sql"].replace("u_idx", "u_id_idx")}
        recorded = _applied(self.two, "failed", done=1)  # the CREATE TABLE ran, the index failed
        p = plan(load(_dir(fixed)), [_applied(self.one), recorded])
        self.assertEqual(p.drift, [])

    def test_changing_a_statement_that_already_ran_is_drift(self):
        edited = THREE | {"0002_two.sql": THREE["0002_two.sql"].replace("a_u (", "a_v (", 1)}
        recorded = _applied(self.two, "failed", done=1)
        p = plan(load(_dir(edited)), [_applied(self.one), recorded])
        self.assertIn("a statement that already ran has changed", p.drift[0])


class Applying(unittest.TestCase):
    def setUp(self):
        self.migrations = load(_dir(THREE))

    def test_a_fresh_database_gets_everything_statement_by_statement(self):
        db = FakeDb()
        ran = apply(db, self.migrations, release="abc123", applied_by="tester")
        self.assertEqual([r.number for r in ran], [1, 2, 3])
        self.assertEqual(len(db.executed), 5)
        self.assertTrue(db.executed[1].startswith("CREATE TABLE a_u"))
        self.assertTrue(all(r["status"] == "applied" for r in db.rows.values()))
        self.assertEqual({r["release"] for r in db.rows.values()}, {"abc123"})
        self.assertEqual(db.rows[2]["done"], 3)
        self.assertEqual(apply(db, self.migrations), [])  # nothing left to do
        self.assertEqual([a.number for a in read_applied(db)], [1, 2, 3])

    def test_upto_stops_early(self):
        db = FakeDb()
        self.assertEqual([r.number for r in apply(db, self.migrations, upto=1)], [1])
        self.assertEqual([r.number for r in apply(db, self.migrations)], [2, 3])

    def test_a_failure_stops_the_run_and_says_what_stayed_applied(self):
        db = FakeDb(fail_on="CREATE INDEX")
        with self.assertRaises(MigrationFailed) as caught:
            apply(db, self.migrations)
        message = str(caught.exception)
        self.assertIn("0002_two stopped at statement 2 of 3", message)
        self.assertIn("(MySQL error 1142)", message)
        self.assertIn("statements 1–1 stay applied", message)
        self.assertEqual(caught.exception.done, 1)
        self.assertEqual(db.rows[2]["status"], "failed")
        self.assertIn("MySQL error 1142", db.rows[2]["error"])
        self.assertNotIn(3, db.rows)  # the run stopped

    def test_apply_resumes_a_failed_migration_where_it_stopped(self):
        db = FakeDb(fail_on="CREATE INDEX")
        with self.assertRaises(MigrationFailed):
            apply(db, self.migrations)
        db.fail_on = None  # e.g. the user gave the role the missing right
        ran = apply(db, self.migrations)
        self.assertEqual([(r.number, r.resumed_at) for r in ran], [(2, 1), (3, 0)])
        self.assertEqual([s.split()[0] for s in db.executed].count("CREATE"), 3)  # a_u ran once
        self.assertEqual(db.rows[2]["status"], "applied")

    def test_drift_stops_everything_before_anything_runs(self):
        db = FakeDb()
        apply(db, self.migrations, upto=1)
        db.rows[1]["checksum"] = checksum("edited")
        db.log.clear()
        with self.assertRaises(MigrationError):
            apply(db, self.migrations)
        self.assertEqual(db.log, [])
        self.assertEqual(len(db.executed), 1)

    def test_a_migration_another_run_is_applying_is_left_alone(self):
        db = FakeDb(fail_on="CREATE INDEX")
        with self.assertRaises(MigrationFailed):
            apply(db, self.migrations)
        db.rows[2].update(status="running", age=30)
        with self.assertRaises(MigrationError) as caught:
            apply(db, self.migrations)
        self.assertIn("being applied by another run", str(caught.exception))

    def test_a_run_that_died_is_resumed(self):
        db = FakeDb(fail_on="CREATE INDEX")
        with self.assertRaises(MigrationFailed):
            apply(db, self.migrations)
        db.fail_on = None
        db.rows[2].update(status="running", age=m.STALE_AFTER_S + 1)
        self.assertEqual([r.number for r in apply(db, self.migrations)], [2, 3])

    def test_losing_the_claim_to_another_run_stops(self):
        db = FakeDb(fail_on="CREATE INDEX")
        with self.assertRaises(MigrationFailed):
            apply(db, self.migrations)
        db.fail_on = None

        def other_runner(fake):
            fake.rows[2]["token"] = "f" * 32

        db.claim_hook = other_runner
        with self.assertRaises(MigrationError) as caught:
            apply(db, self.migrations)
        self.assertIn("taken by another run", str(caught.exception))

    def test_a_record_another_run_just_wrote_stops(self):
        db = FakeDb()
        db.table = True
        original = db.execute

        def racing(sql, params=()):
            if sql == m.START_SQL and 1 not in db.rows:
                db.rows[1] = {"token": "f" * 32}  # the other run's record lands first
            return original(sql, params)

        db.execute = racing
        with self.assertRaises(MigrationError) as caught:
            apply(db, self.migrations)
        self.assertIn("another runner?", str(caught.exception))
        self.assertEqual(db.executed, [])


class Console(unittest.TestCase):
    """A migration prepared for the PlanetScale console (db/console_sql.py)."""

    def setUp(self):
        self.migrations = load(_dir(THREE))

    def test_ids_are_minted_and_the_record_comes_last(self):
        migration = self.migrations[2]
        text = m.console_script(migration)
        statements = m.split_statements(text)
        self.assertEqual(len(statements), len(migration.statements) + 2)
        self.assertNotIn("{new_id:", text)
        self.assertRegex(statements[0], r"'cel_[0-9a-hjkmnp-tv-z]{32}'")
        self.assertTrue(statements[-2].startswith("CREATE TABLE IF NOT EXISTS meta_migrations"))
        self.assertTrue(statements[-1].startswith("INSERT INTO meta_migrations"))
        self.assertEqual(m._problems("console", text), [])  # what the runner would accept

    def test_the_record_matches_what_the_runner_expects(self):
        for migration in self.migrations:
            with self.subTest(migration=migration.name):
                record = m.split_statements(m.console_script(migration))[-1]
                found = re.search(
                    r"VALUES \('(mig_[0-9a-hjkmnp-tv-z]{32})', (\d+), '([a-z0-9_]+)', "
                    r"'([0-9a-f]{64})', 'applied', (\d+), (\d+), '([0-9a-f]{64})', '([0-9a-f]{32})', "
                    r"UTC_TIMESTAMP\(6\), 'planetscale-console'\)$",
                    record,
                )
                self.assertIsNotNone(found, record)
                _, number, name, digest, total, done, done_digest, _ = found.groups()
                recorded = Applied(
                    int(number), name, digest, "applied", int(done), int(total), done_digest,
                    "0" * 32, "", 0, "", "planetscale-console",
                )
                p = plan(self.migrations, [recorded])
                self.assertEqual(p.drift, [])
                self.assertNotIn(migration.number, [x.number for x in p.pending])

    def test_record_only_holds_just_the_record(self):
        text = m.console_script(self.migrations[0], record_only=True)
        statements = m.split_statements(text)
        self.assertEqual(len(statements), 2)
        self.assertTrue(statements[-1].startswith("INSERT INTO meta_migrations"))

    def test_each_file_mints_its_own_ids(self):
        migration = self.migrations[2]
        self.assertNotEqual(m.console_script(migration), m.console_script(migration))


class Server(unittest.TestCase):
    def test_reports_the_server_without_writing(self):
        db = FakeDb()
        r = server_report(db)
        self.assertEqual(
            (r["vitess"], r["mysql_8"], r["database"], r["tables"]),
            (True, True, "planetscale-backbone", 0),
        )
        self.assertFalse(db.table)
        self.assertFalse(server_report(FakeDb(version="16.4"))["mysql_8"])


class Errors(unittest.TestCase):
    def test_a_database_error_reads_as_one_line(self):
        e = FakeError(1064, "You have an error in your SQL syntax;\nnear 'x'")
        self.assertEqual(
            describe_error(e),
            "You have an error in your SQL syntax; near 'x' (MySQL error 1064)",
        )
        self.assertEqual(describe_error(ValueError("x")), "ValueError: x")


class Secrets(unittest.TestCase):
    PASSWORD = "s3cr3t-Pa55"
    HOST = "aws.connect.psdb.cloud"

    def test_json_fields(self):
        info = parse_secret(
            json.dumps(
                {
                    "host": "eu-central.aws-eu-central-1-x.connect.psdb.cloud",
                    "port": "3306",
                    "username": "app",
                    "password": self.PASSWORD,
                    "database": "planetscale-backbone",
                }
            )
        )
        self.assertEqual((info.port, info.user, info.database), (3306, "app", "planetscale-backbone"))
        self.assertEqual(info.describe()["region_in_host"], "eu-central-1")
        self.assertNotIn(self.PASSWORD, repr(info))
        self.assertNotIn(self.PASSWORD, str(info.describe()))
        self.assertNotIn("aws-eu-central-1-x", str(info.describe()))

    def test_urls_plain_or_inside_json(self):
        url = f"mysql://app:{self.PASSWORD.replace('-', '%2D')}@{self.HOST}/backbone?ssl={{}}"
        for text in (url, json.dumps({"DATABASE_URL": url})):
            info = parse_secret(text)
            self.assertEqual(
                (info.user, info.password, info.port, info.database),
                ("app", self.PASSWORD, 3306, "backbone"),
            )

    def test_the_database_name_is_required(self):
        with self.assertRaises(ValueError) as caught:
            parse_secret(json.dumps({"host": "h", "user": "u", "password": "p"}))
        self.assertIn("no database", str(caught.exception))
        with self.assertRaises(ValueError):
            parse_secret(f"mysql://u:p@{self.HOST}")

    def test_a_secret_with_only_user_and_password_takes_the_deployment_settings(self):
        # As created for PlanetScale: the host is configuration, not a credential.
        settings = {"host": self.HOST, "port": "", "database": "backbone"}
        for text in (
            json.dumps({"username": "app", "password": self.PASSWORD}),
            json.dumps({"user-name": "app", "Password": self.PASSWORD}),
        ):
            info = parse_secret(text, settings)
            self.assertEqual(
                (info.host, info.port, info.database, info.user, info.password),
                (self.HOST, 3306, "backbone", "app", self.PASSWORD),
            )
        own = parse_secret(json.dumps({"host": "own", "user": "u", "password": "p"}), settings)
        self.assertEqual(own.host, "own")  # the secret's own value wins
        with self.assertRaises(ValueError) as caught:
            parse_secret(json.dumps({"username": "app", "password": "p"}), {"host": ""})
        self.assertIn("no host", str(caught.exception))

    def test_a_bad_secret_names_its_keys_never_its_values(self):
        with self.assertRaises(ValueError) as caught:
            parse_secret(json.dumps({"Host": "h", "secret_password": self.PASSWORD}))
        message = str(caught.exception)
        self.assertIn("no user, password", message)
        self.assertIn("its keys: Host, secret_password", message)
        self.assertNotIn(self.PASSWORD, message)
        for bad in (
            "not json",
            "[1]",
            '{"host": "h", "user": "u", "password": "p", "database": "d", "port": "x"}',
            f"postgresql://u:{self.PASSWORD}@h/db",
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError) as caught:
                parse_secret(bad)
            self.assertNotIn(self.PASSWORD, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
