"""
Static checks on db/migrations — what can be verified before a database exists.

The migrations are syntax-checked with a Postgres parser when they change; these tests hold them
to the platform's rules on every run:
  - migrations are numbered 0001, 0002, … with no gaps;
  - every table's id is checked against its registered prefix (conventions §1);
  - every table carrying tenant_id has row-level security enabled, forced and a policy (§6);
  - the seed migration is exactly what the access catalogue generates.

Run:  python -m unittest discover -s packages/rudi_platform/tests -v
"""

from __future__ import annotations

import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "packages"))

from rudi_platform.access import seed
from rudi_platform.ids import ID_RE, Cat

MIGRATIONS = os.path.join(ROOT, "db", "migrations")

EXPECTED_PREFIX = {
    "registry.cells": Cat.CELL,
    "registry.tenants": Cat.TENANT,
    "registry.clones": Cat.CLONE,
    "registry.clone_versions": Cat.CLONE_VERSION,
    "registry.personas": Cat.PERSONA,
    "registry.skills": Cat.SKILL,
    "registry.skill_versions": Cat.SKILL_VERSION,
    "registry.skill_packs": Cat.SKILL_PACK,
    "registry.endpoints": Cat.ENDPOINT,
    "registry.api_keys": Cat.API_KEY,
    "registry.webhooks": Cat.WEBHOOK,
    "registry.scope_requests": Cat.SCOPE_REQUEST,
    "registry.mint_requests": Cat.MINT_REQUEST,
    "access.accounts": Cat.ACCOUNT,
    "access.permissions": Cat.PERMISSION,
    "access.roles": Cat.ROLE,
    "access.role_assignments": Cat.ROLE_ASSIGNMENT,
    "access.sessions": Cat.SESSION,
    "access.invitations": Cat.INVITATION,
    "audit.events": Cat.AUDIT,
}
JOIN_TABLES = {"access.role_permissions"}  # composite keys, no id column

TABLE_RE = re.compile(r"CREATE TABLE (\w+\.\w+) \((.*?)\n\);", re.DOTALL)


def _read(name: str) -> str:
    with open(os.path.join(MIGRATIONS, name), encoding="utf-8") as f:
        return f.read().replace("\r\n", "\n")


def _files() -> list[str]:
    return sorted(f for f in os.listdir(MIGRATIONS) if f.endswith(".sql"))


def _all_sql() -> str:
    return "\n".join(_read(f) for f in _files())


class Numbering(unittest.TestCase):
    def test_contiguous_and_named(self):
        files = _files()
        self.assertTrue(files)
        for i, name in enumerate(files, start=1):
            self.assertRegex(name, rf"^{i:04d}_[a-z0-9_]+\.sql$")


class Ids(unittest.TestCase):
    def test_domain_matches_the_python_pattern(self):
        sql = _read("0001_registry_access_audit.sql")
        alphabet = re.search(r"\[0-9a-hjkmnp-tv-z\]\{32\}", ID_RE.pattern)
        self.assertIsNotNone(alphabet)
        self.assertIn("'^[a-z]{3}_[0-9a-hjkmnp-tv-z]{32}$'", sql)

    def test_every_table_checks_its_registered_prefix(self):
        tables = dict(TABLE_RE.findall(_all_sql()))
        self.assertEqual(set(tables) - JOIN_TABLES, set(EXPECTED_PREFIX))
        for table, body in tables.items():
            if table in JOIN_TABLES:
                continue
            found = re.search(r"CHECK \(left\(id, 4\) = '([a-z]{3})_'\)", body)
            self.assertIsNotNone(found, f"{table} has no id prefix check")
            self.assertEqual(found.group(1), EXPECTED_PREFIX[table].value, table)


class RowLevelSecurity(unittest.TestCase):
    def test_every_tenant_table_is_isolated(self):
        sql = _all_sql()
        tenant_tables = {
            t
            for t, body in TABLE_RE.findall(sql)
            if re.search(r"^\s+tenant_id\s", body, re.MULTILINE)
        } | {"registry.tenants"}
        self.assertGreaterEqual(len(tenant_tables), 12)
        for t in sorted(tenant_tables):
            with self.subTest(table=t):
                self.assertIn(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;", sql)
                self.assertIn(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY;", sql)
                self.assertIn(f"CREATE POLICY tenant_isolation ON {t}\n", sql)


class Seed(unittest.TestCase):
    def test_seed_file_is_generated_from_the_catalogue(self):
        self.assertEqual(
            _read("0002_seed_access.sql"),
            seed.render(),
            "regenerate: python -m rudi_platform.access.seed > db/migrations/0002_seed_access.sql",
        )


if __name__ == "__main__":
    unittest.main()
