"""
meetrudi-internal-prod-db-migrate — the Lambda around rudi_platform.db.

What this guards: an apply without its confirmation, a password or full host in the answer, a
connection left open, one unreadable secret hiding the others' reports, a failure reported as
success, and a failed apply that does not say how to resume.

Run:  python -m unittest discover -s services/db-migrate/tests -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from typing import ClassVar

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "packages"))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import handler
from rudi_platform.db import migrate as m

PASSWORD = "not-a-real-Pa55word"
HOST = "aws-eu-central-1-x.connect.psdb.cloud"
SECRETS = {
    "write": json.dumps({"host": HOST, "user": "app", "password": PASSWORD, "database": "backbone"}),
    "read": json.dumps({"host": HOST, "user": "reader", "password": PASSWORD,
                        "database": "backbone"}),
}


class FakeConn:
    """MySQL as far as the runner sees it: every statement commits on its own."""

    opened: ClassVar[list[FakeConn]] = []

    def __init__(self, info, fail_on=None, version="8.0.34-Vitess", error=None):
        self.info = info
        self.fail_on = fail_on
        self.error = error or (1142, "CREATE command denied to user 'app'@'%' for table 't2'")
        self.version = version
        self.table, self.rows, self.closed, self.executed = False, {}, False, []
        FakeConn.opened.append(self)

    def query(self, sql, params=()):
        if sql == "SHOW TABLES LIKE 'meta_migrations'" or sql == "SHOW TABLES":
            return [("meta_migrations",)] if self.table else []
        if sql == "SELECT VERSION(), DATABASE()":
            return [(self.version, "backbone")]
        if sql == m.STATE_SQL:
            return [
                (r["n"], r["name"], r["sum"], r["status"], r["done"], r["total"], r["done_sum"],
                 r["token"], r["error"] or "", 0, r["at"], "meetrudi-db-migrate")
                for _, r in sorted(self.rows.items())
            ]
        raise AssertionError(sql[:60])

    def execute(self, sql, params=()):
        if sql == m.BOOTSTRAP:
            self.table = True
        elif sql == m.START_SQL:
            _id, n, name, digest, total, done_sum, token, _by, _release = params
            self.rows[n] = {"n": n, "name": name, "sum": digest, "status": "running", "done": 0,
                            "total": total, "done_sum": done_sum, "token": token, "error": None,
                            "at": None}
        elif sql == m.RESUME_SQL:
            name, digest, total, token, _by, _release, n, old = params
            if self.rows[n]["token"] != old:
                return 0
            self.rows[n].update(status="running", name=name, sum=digest, total=total, token=token)
        elif sql == m.PROGRESS_SQL:
            done, done_sum, n, _token = params
            self.rows[n].update(done=done, done_sum=done_sum)
        elif sql == m.FAILED_SQL:
            error, n, _token = params
            self.rows[n].update(status="failed", error=error)
        elif sql == m.DONE_SQL:
            _ms, n, _token = params
            self.rows[n].update(status="applied", at="2026-11-15 18:00:00")
        elif self.fail_on and self.fail_on in sql:
            raise RuntimeError(*self.error)
        else:
            self.executed.append(sql)
        return 1

    def close(self):
        self.closed = True


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        for name, text in {
            "0001_one.sql": "CREATE TABLE t1 (id CHAR(36) NOT NULL, PRIMARY KEY (id));\n",
            "0002_two.sql": (
                "CREATE TABLE t2 (id CHAR(36) NOT NULL, PRIMARY KEY (id));\n"
                "CREATE INDEX t2_idx ON t2 (id);\n"
            ),
        }.items():
            with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
                f.write(text)
        self.fail_on = None
        self.error = None
        self.version = "8.0.34-Vitess"
        self.conns: list[FakeConn] = []
        FakeConn.opened = []
        self.patch = {
            "MIGRATIONS_DIR": self.dir,
            "SECRET": "write",
            "PLAN_SECRET": "read",
            "CHECK_SECRETS": ["write", "read", "missing"],
            "RELEASE": "abc123",
            "_fetch_secret": self.fetch,
            "_open": self.open,
        }
        self.saved = {k: getattr(handler, k) for k in self.patch}
        for k, v in self.patch.items():
            setattr(handler, k, v)

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(handler, k, v)

    def open(self, info):
        # One database across invocations, like the real one.
        if self.conns:
            conn = FakeConn(info, self.fail_on, self.version, self.error)
            conn.table, conn.rows = self.conns[0].table, self.conns[0].rows
        else:
            conn = FakeConn(info, self.fail_on, self.version, self.error)
        self.conns.append(conn)
        return conn

    @staticmethod
    def fetch(name):
        if name not in SECRETS:
            raise RuntimeError(f"ResourceNotFoundException: {name}")
        return SECRETS[name]

    def run_action(self, **event):
        result = handler.handler(event)
        text = json.dumps(result)
        self.assertNotIn(PASSWORD, text)
        self.assertNotIn("aws-eu-central-1-x", text)
        self.assertTrue(all(c.closed for c in FakeConn.opened), "a connection was left open")
        return result


class Plan(Base):
    def test_plan_is_the_default_and_reads_only(self):
        r = self.run_action()
        self.assertEqual(r["action"], "plan")
        self.assertTrue(r["ok"])
        self.assertEqual(r["pending"], ["0001_one", "0002_two"])
        self.assertEqual((r["applied"], r["unfinished"]), ([], []))
        self.assertEqual(r["connection"]["region_in_host"], "eu-central-1")
        self.assertTrue(r["server"]["vitess"])
        self.assertEqual(r["warnings"], [])
        self.assertFalse(FakeConn.opened[0].table)

    def test_plan_reads_with_the_read_secret(self):
        r = self.run_action(action="plan")
        self.assertEqual(r["secret"], "read")
        self.assertEqual([c.info.user for c in FakeConn.opened], ["reader"])

    def test_a_server_that_is_not_mysql_8_is_flagged(self):
        self.version = "16.4"
        self.assertIn("not MySQL 8", self.run_action()["warnings"][0])


class Apply(Base):
    def test_apply_needs_its_confirmation(self):
        r = self.run_action(action="apply")
        self.assertFalse(r["ok"])
        self.assertEqual(FakeConn.opened, [])

    def test_apply_records_every_migration(self):
        r = self.run_action(action="apply", confirm="apply")
        self.assertTrue(r["ok"])
        self.assertEqual(r["secret"], "write")
        self.assertEqual([c.info.user for c in FakeConn.opened], ["app"])
        self.assertEqual([x["name"] for x in r["ran"]], ["0001_one", "0002_two"])
        self.assertEqual(r["pending"], [])
        self.assertEqual(len(r["applied"]), 2)
        self.assertEqual(len(self.conns[0].executed), 3)

    def test_upto(self):
        r = self.run_action(action="apply", confirm="apply", upto=1)
        self.assertEqual([x["name"] for x in r["ran"]], ["0001_one"])
        self.assertEqual(r["pending"], ["0002_two"])
        self.assertFalse(self.run_action(action="apply", confirm="apply", upto="all")["ok"])

    def test_a_failure_reports_what_stayed_applied_and_resumes(self):
        self.fail_on = "CREATE INDEX"
        r = self.run_action(action="apply", confirm="apply")
        self.assertFalse(r["ok"])
        self.assertEqual(r["failed"], "0002_two")
        self.assertIn("MySQL error 1142", r["error"])
        self.assertIn("Admin role", r["hint"])
        self.assertEqual([a["name"] for a in r["applied"]], ["0001_one"])
        self.assertEqual(r["unfinished"][0]["statements_done"], 1)
        self.assertEqual(r["pending"], ["0002_two"])

        self.fail_on = None
        again = self.run_action(action="apply", confirm="apply")
        self.assertTrue(again["ok"])
        self.assertEqual(
            [(x["name"], x["resumed_at_statement"]) for x in again["ran"]], [("0002_two", 2)]
        )

    def test_a_branch_with_safe_migrations_gets_its_hint(self):
        self.fail_on = "CREATE TABLE t1"
        self.error = (1105, "direct DDL is disabled")
        r = self.run_action(action="apply", confirm="apply")
        self.assertIn("safe migrations", r["hint"])


class Check(Base):
    def test_every_secret_is_reported_even_when_one_fails(self):
        r = self.run_action(action="check")
        self.assertFalse(r["ok"])
        by_secret = {x["secret"]: x for x in r["secrets"]}
        self.assertTrue(by_secret["write"]["ok"])
        self.assertTrue(by_secret["read"]["server"]["mysql_8"])
        self.assertEqual(by_secret["missing"]["stage"], "secret")
        self.assertEqual(len(FakeConn.opened), 2)


class Errors(Base):
    def test_unknown_action(self):
        for action in ("drop", "rehearse", "roles"):
            self.assertFalse(self.run_action(action=action)["ok"])

    def test_an_unreadable_secret_is_a_stage_not_a_crash(self):
        handler.PLAN_SECRET = "missing"
        r = self.run_action()
        self.assertEqual((r["ok"], r["stage"]), (False, "secret"))

    def test_a_refused_read_is_a_stage_not_a_crash(self):
        # PlanetScale's Writer role may write rows but not read them.
        def refusing(info):
            conn = self.open(info)
            conn.table = True
            plain = conn.query

            def query(sql, params=()):
                if sql == m.STATE_SQL:
                    raise RuntimeError(
                        1105, "Select command denied to user 'w' for table 'meta_migrations'"
                    )
                return plain(sql, params)

            conn.query = query
            return conn

        handler._open = refusing
        r = self.run_action()
        self.assertEqual((r["ok"], r["stage"]), (False, "query"))
        self.assertIn("MySQL error 1105", r["error"])
        self.assertIn("Reader", r["hint"])

    def test_a_secret_without_its_database_says_where_to_set_it(self):
        SECRETS["bare"] = json.dumps({"host": HOST, "user": "app", "password": PASSWORD})
        handler.PLAN_SECRET = "bare"
        try:
            r = self.run_action()
        finally:
            del SECRETS["bare"]
        self.assertEqual(r["stage"], "secret")
        self.assertIn("planetscale_database", r["error"])

    def test_a_bad_migration_set_is_reported(self):
        os.remove(os.path.join(self.dir, "0001_one.sql"))
        r = self.run_action()
        self.assertEqual((r["ok"], r["stage"]), (False, "migrations"))


if __name__ == "__main__":
    unittest.main()
