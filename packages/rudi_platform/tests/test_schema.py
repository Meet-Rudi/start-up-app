"""
Static checks on db/migrations — what can be verified before a database exists.

The target is PlanetScale Vitess, MySQL 8 (decision #46). These tests hold the migrations to the
platform's rules on every run:
  - migrations are numbered 0001, 0002, … with no gaps, and the runner accepts them (which also
    refuses what PlanetScale cannot run: triggers, stored functions, foreign keys, renames…);
  - every table is named by its area, has a primary key and the platform's table options;
  - every id column checks its registered prefix, and every column pointing at another row checks
    the prefix of what it points at (conventions §1) — there are no foreign keys;
  - every table holds its own tenant_id column unless it is listed below as holding no tenant's
    data (decision #41, conventions §8);
  - every index of a tenant table leads with tenant_id, unless it is one of the listed global keys
    (decision #47: every statement names its tenant);
  - personal text (`*_enc` columns) is ciphertext only, and S3 key columns sit in a tenant folder;
  - times are DATETIME(6) in UTC;
  - no name we create is a SQL keyword or reserved word (decision #43, rudi_platform.db.keywords);
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
from rudi_platform.db import migrate, tenant_guard
from rudi_platform.db.keywords import clashes
from rudi_platform.ids import ID_RE, Cat

MIGRATIONS = os.path.join(ROOT, "db", "migrations")

AREAS = (
    "registry",
    "access_control",
    "audit_trail",
    "runtime",
    "schedule",
    "metering",
    "ops",
    "meta",
)
TABLE_OPTIONS = "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin"
ID_BODY = "[0-9a-hjkmnp-tv-z]{32}"

EXPECTED_PREFIX = {
    "registry_cells": Cat.CELL,
    "registry_tenants": Cat.TENANT,
    "registry_clones": Cat.CLONE,
    "registry_clone_versions": Cat.CLONE_VERSION,
    "registry_personas": Cat.PERSONA,
    "registry_skills": Cat.SKILL,
    "registry_skill_versions": Cat.SKILL_VERSION,
    "registry_skill_packs": Cat.SKILL_PACK,
    "registry_endpoints": Cat.ENDPOINT,
    "registry_api_keys": Cat.API_KEY,
    "registry_webhooks": Cat.WEBHOOK,
    "registry_scope_requests": Cat.SCOPE_REQUEST,
    "registry_mint_requests": Cat.MINT_REQUEST,
    "access_control_accounts": Cat.ACCOUNT,
    "access_control_permissions": Cat.PERMISSION,
    "access_control_roles": Cat.ROLE,
    "access_control_role_assignments": Cat.ROLE_ASSIGNMENT,
    "access_control_sessions": Cat.SESSION,
    "access_control_invitations": Cat.INVITATION,
    "audit_trail_events": Cat.AUDIT,
    "runtime_end_users": Cat.USER,
    "runtime_handles": Cat.HANDLE,
    "runtime_identity_links": Cat.IDENTITY_LINK,
    "runtime_consents": Cat.CONSENT,
    "runtime_conversations": Cat.CONVERSATION,
    "runtime_turns": Cat.TURN,
    "runtime_messages": Cat.MESSAGE,
    "runtime_escalations": Cat.ESCALATION,
    "runtime_takeovers": Cat.TAKEOVER,
    "schedule_items": Cat.SCHEDULE,
    "metering_calls": Cat.USAGE,
    "metering_budgets": Cat.BUDGET,
    "ops_alerts": Cat.ALERT,
    "ops_erasures": Cat.ERASURE,
    "ops_jobs": Cat.JOB,
}
JOIN_TABLES = {"access_control_role_permissions"}  # composite keys, no id column

# What a column that points at another row must hold. No foreign keys (decision #46), so this
# prefix check is what the database itself knows about a reference.
REFERENCE_PREFIX = {
    "tenant_id": Cat.TENANT,
    "cell_id": Cat.CELL,
    "clone_id": Cat.CLONE,
    "clone_version_id": Cat.CLONE_VERSION,
    "live_version_id": Cat.CLONE_VERSION,
    "skill_id": Cat.SKILL,
    "skill_version_id": Cat.SKILL_VERSION,
    "gate_run_id": Cat.GATE_RUN,
    "scope_request_id": Cat.SCOPE_REQUEST,
    "role_id": Cat.ROLE,
    "permission_id": Cat.PERMISSION,
    "account_id": Cat.ACCOUNT,
    "actor_account_id": Cat.ACCOUNT,
    "created_by": Cat.ACCOUNT,
    "granted_by": Cat.ACCOUNT,
    "invited_by": Cat.ACCOUNT,
    "vetted_by": Cat.ACCOUNT,
    "approved_by": Cat.ACCOUNT,
    "acknowledged_by": Cat.ACCOUNT,
    "requested_by": Cat.ACCOUNT,
    "end_user_id": Cat.USER,
    "linked_user_id": Cat.USER,
    "conversation_id": Cat.CONVERSATION,
    "turn_id": Cat.TURN,
    "budget_id": Cat.BUDGET,
    "checkpoint_id": Cat.CHECKPOINT,
}
ANY_PREFIX = {"target_id"}  # the audit trail points at any kind of row
GENERATED = {"tenant_or_platform", "open_conversation_id"}  # computed from checked columns
# Named like a reference but holding a label: what raised an escalation, not who.
LABELS = {"detected_by"}

# Tables that hold no tenant's data, so no tenant_id. Adding a table here is a decision, not a
# shortcut: anything a tenant owns carries its own tenant_id (decision #41).
NO_TENANT_DATA = {
    "registry_cells",  # the platform's deployment units
    "registry_tenants",  # the tenants themselves; their id is the tenant id
    "registry_skills",  # shared skill inventory, readable by every tenant
    "registry_skill_versions",
    "registry_skill_packs",
    "access_control_accounts",  # console accounts; tenant access is in role_assignments
    "access_control_sessions",
    "access_control_permissions",  # the access catalogue
    "access_control_roles",
    "access_control_role_permissions",
}
# tenant_id may be NULL here, with this meaning; platform work reads them with tenant_id IS NULL.
NULL_TENANT_MEANS = {
    "access_control_role_assignments": "a platform-wide role",
    "audit_trail_events": "a platform event",
    "metering_calls": "a platform call (arena, skill factory)",
    "ops_alerts": "a platform alert",
    "ops_jobs": "a platform job",
}
# Keys of tenant tables that do not lead with tenant_id: the resolvers' lookups (decision #47:
# they find the tenant of a request that arrives without one) and global uniqueness.
GLOBAL_KEYS = {
    "registry_endpoints.endpoints_address_uq": "resolver: inbound routing by channel and address",
    "registry_api_keys.api_keys_hash_uq": "resolver: an API key's tenant",
    "access_control_invitations.invitations_token_uq": "resolver: accepting an invitation",
    "access_control_role_assignments.role_assignments_uq": (
        "resolver: an account's tenants at sign-in; one assignment per account, role and tenant"
    ),
}

TABLE_RE = re.compile(r"CREATE TABLE (\w+) \((.*?)\n\) ([^;\n]*);", re.DOTALL)
COLUMN_RE = re.compile(r"^  ([a-z]\w*)\s+([^\n]*(?:\n {4,}[^\n]*)*)", re.MULTILINE)
KEY_RE = re.compile(r"^  (?:UNIQUE )?KEY (\w+) \(([^)]*)\)", re.MULTILINE)


def _read(name: str) -> str:
    with open(os.path.join(MIGRATIONS, name), encoding="utf-8") as f:
        return f.read().replace("\r\n", "\n")


def _files() -> list[str]:
    return sorted(f for f in os.listdir(MIGRATIONS) if f.endswith(".sql"))


def _code(sql: str) -> str:
    """The SQL without -- comments (rudi_platform.db.migrate's own reading of it)."""
    return ";\n".join(migrate.split_statements(sql)) + ";"


def _all_sql() -> str:
    return "\n".join(_code(_read(f)) for f in _files())


def _tables(sql: str | None = None) -> dict[str, str]:
    return {t: body for t, body, _ in TABLE_RE.findall(sql or _all_sql())}


def _columns(body: str) -> dict[str, str]:
    return {
        name: " ".join(definition.split())
        for name, definition in COLUMN_RE.findall(body)
        if name not in {"PRIMARY", "UNIQUE", "KEY", "CHECK", "CONSTRAINT", "INDEX"}
    }


def _id_check(column: str, prefix: str) -> str:
    return f"CHECK (REGEXP_LIKE({column}, '^{prefix}_{ID_BODY}$', 'c'))"


class Numbering(unittest.TestCase):
    def test_contiguous_and_named(self):
        files = _files()
        self.assertTrue(files)
        for i, name in enumerate(files, start=1):
            self.assertRegex(name, rf"^{i:04d}_[a-z0-9_]+\.sql$")

    def test_the_runner_accepts_the_migrations(self):
        loaded = migrate.load(MIGRATIONS)
        self.assertEqual([m.number for m in loaded], list(range(1, len(_files()) + 1)))


class Tables(unittest.TestCase):
    def test_named_by_area_with_a_primary_key_and_the_platform_options(self):
        found = TABLE_RE.findall(_all_sql())
        self.assertEqual(len(found), len(EXPECTED_PREFIX) + len(JOIN_TABLES))
        for table, body, options in found:
            with self.subTest(table=table):
                self.assertTrue(table.startswith(tuple(f"{a}_" for a in AREAS)), table)
                self.assertRegex(body, r"\n  PRIMARY KEY \(")
                self.assertEqual(options, TABLE_OPTIONS)

    def test_the_runners_table_follows_the_same_rules(self):
        (table, body, options), = TABLE_RE.findall(
            migrate.BOOTSTRAP.strip().replace("CREATE TABLE IF NOT EXISTS", "CREATE TABLE") + ";"
        )
        self.assertEqual(table, "meta_migrations")
        self.assertEqual(options, TABLE_OPTIONS)
        self.assertIn(_id_check("id", Cat.MIGRATION.value), _columns(body)["id"])

    def test_times_are_datetime_with_microseconds(self):
        for table, body in _tables().items():
            for name, definition in _columns(body).items():
                if name.endswith("_at"):
                    with self.subTest(column=f"{table}.{name}"):
                        self.assertTrue(definition.startswith("DATETIME(6)"), definition)


class Ids(unittest.TestCase):
    def test_the_alphabet_matches_the_python_pattern(self):
        self.assertIn(ID_BODY, ID_RE.pattern)

    def test_every_table_checks_its_registered_prefix(self):
        tables = _tables()
        self.assertEqual(set(tables) - JOIN_TABLES, set(EXPECTED_PREFIX))
        for table, body in tables.items():
            if table in JOIN_TABLES:
                continue
            with self.subTest(table=table):
                definition = _columns(body)["id"]
                self.assertTrue(definition.startswith("CHAR(36) NOT NULL"), definition)
                self.assertIn(_id_check("id", EXPECTED_PREFIX[table].value), definition)

    def test_every_reference_checks_what_it_points_at(self):
        seen = set()
        for table, body in _tables().items():
            for name, definition in _columns(body).items():
                if name == "id" or name in GENERATED or name in LABELS:
                    continue
                if not name.endswith(("_id", "_by")):
                    continue
                seen.add(name)
                with self.subTest(column=f"{table}.{name}"):
                    self.assertTrue(definition.startswith("CHAR(36)"), definition)
                    prefix = (
                        "[a-z]{3}" if name in ANY_PREFIX else REFERENCE_PREFIX[name].value
                    )
                    self.assertIn(_id_check(name, prefix), definition)
        self.assertLessEqual(set(REFERENCE_PREFIX) | ANY_PREFIX, seen, "a listed column is gone")


class TenantColumn(unittest.TestCase):
    """In multi-tenant tables the tenant is a column on the row, never worked out by a join."""

    def test_every_table_with_tenant_data_has_its_own_tenant_id(self):
        tables = _tables()
        self.assertLessEqual(NO_TENANT_DATA, set(tables), "a listed table no longer exists")
        for table, body in sorted(tables.items()):
            column = _columns(body).get("tenant_id")
            with self.subTest(table=table):
                if table in NO_TENANT_DATA:
                    self.assertIsNone(column, f"{table} has tenant_id: take it off the list")
                    continue
                self.assertIsNotNone(
                    column,
                    f"{table} needs a tenant_id column, or a reasoned entry in NO_TENANT_DATA",
                )
                self.assertEqual(column.startswith("CHAR(36) NOT NULL"), table not in NULL_TENANT_MEANS)

    def test_every_index_of_a_tenant_table_leads_with_tenant_id(self):
        """What lets every statement name its tenant cheaply, and what Vitess shards on."""
        sql = _all_sql()
        used = set()
        for table, body in _tables(sql).items():
            if table in NO_TENANT_DATA:
                continue
            for key, columns in KEY_RE.findall(body):
                with self.subTest(key=f"{table}.{key}"):
                    if f"{table}.{key}" in GLOBAL_KEYS:
                        used.add(f"{table}.{key}")
                        continue
                    self.assertEqual(columns.split(",")[0].strip(), "tenant_id", columns)
        self.assertEqual(used, set(GLOBAL_KEYS), "a listed global key is gone")


class TenantGuard(unittest.TestCase):
    """The guard (rudi_platform.db.tenant_guard) and the schema describe the same tables."""

    def test_the_guard_knows_the_same_tables_of_no_tenant(self):
        self.assertEqual(tenant_guard.NO_TENANT_DATA, NO_TENANT_DATA)

    def test_upserts_are_refused_exactly_where_a_key_is_global(self):
        self.assertEqual(tenant_guard.GLOBAL_KEY_TABLES, {k.split(".")[0] for k in GLOBAL_KEYS})

    def test_each_global_key_has_its_resolver_and_nothing_else_does(self):
        self.assertEqual({r.key for r in tenant_guard.RESOLVERS.values()}, set(GLOBAL_KEYS))

    def test_resolvers_read_columns_that_exist(self):
        tables = _tables()
        for key, r in tenant_guard.RESOLVERS.items():
            with self.subTest(resolver=key):
                found = re.fullmatch(r"SELECT (.+) FROM (\w+) WHERE (.+)", r.sql)
                self.assertIsNotNone(found, r.sql)
                selected, table, where = found.groups()
                columns = _columns(tables[table])
                for column in [c.strip() for c in selected.split(",")] + re.findall(
                    r"(\w+) = %s", where
                ):
                    self.assertIn(column, columns)
                self.assertEqual(re.findall(r"(\w+) = %s", where), list(r.params))

    def test_no_migration_writes_a_tenants_rows(self):
        """The runner connects without the guard, so migrations only touch tables of no tenant;
        changing tenant data is the guard's work, tenant by tenant."""
        for name in _files():
            for statement in migrate.split_statements(_read(name)):
                verb = statement.split(None, 1)[0].upper()
                if verb not in ("INSERT", "UPDATE", "DELETE", "REPLACE"):
                    continue
                target = re.match(r"(?:INSERT\s+INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM)\s+(\w+)",
                                  statement, re.IGNORECASE)
                with self.subTest(migration=name, statement=statement[:60]):
                    self.assertIsNotNone(target)
                    self.assertIn(target.group(1), NO_TENANT_DATA)


class PlanetScaleRules(unittest.TestCase):
    """What the runner refuses, held here too so a reader sees it in one place."""

    def test_no_foreign_keys_triggers_routines_or_auto_increment(self):
        code = re.sub(r"'(?:[^']|'')*'", "''", _all_sql()).upper()
        # Whole words: a column such as function_name is fine.
        for word in ("REFERENCES", "FOREIGN KEY", "TRIGGER", "PROCEDURE", "FUNCTION",
                     "AUTO_INCREMENT", "CREATE SCHEMA", "CREATE DATABASE", "RENAME"):
            with self.subTest(word=word):
                self.assertNotRegex(code, rf"\b{word}\b")

    def test_no_backslash_anywhere(self):
        for name in _files():
            self.assertNotIn("\\", _read(name), name)

    def test_the_runner_refuses_what_planetscale_cannot_run(self):
        for statement in (
            "CREATE TRIGGER t BEFORE UPDATE ON x FOR EACH ROW SET NEW.a = 1",
            "CREATE FUNCTION f() RETURNS INT RETURN 1",
            "ALTER TABLE x ADD FOREIGN KEY (a) REFERENCES y (id)",
            "ALTER TABLE x RENAME COLUMN a TO b",
            "CREATE TABLE x (id BIGINT AUTO_INCREMENT PRIMARY KEY)",
            "START TRANSACTION",
            "CREATE DATABASE other",
            "SELECT 'a\\.b'",
            "/*! SELECT 1 */ SELECT 2",
            "INSERT INTO x (id) VALUES ({new_id:usr})",
        ):
            with self.subTest(statement=statement):
                self.assertTrue(migrate._problems("0009_x", statement + ";"))
        self.assertEqual(migrate._problems("0009_x", "SELECT 'rename; references';"), [])


class PersonalData(unittest.TestCase):
    """Personal text is stored encrypted; S3 keys point into a tenant's folder (#41, #42)."""

    def _columns(self):
        for table, body in _tables().items():
            for name, definition in _columns(body).items():
                yield table, name, definition

    def test_every_enc_column_is_ciphertext(self):
        found = 0
        for table, name, definition in self._columns():
            if name.endswith("_enc"):
                found += 1
                with self.subTest(column=f"{table}.{name}"):
                    self.assertRegex(definition, r"^(MEDIUM)?BLOB\b")
                    self.assertIn(
                        f"CHECK (LENGTH({name}) > 1 AND ASCII({name}) IN (1, 2))", definition
                    )
        self.assertGreaterEqual(found, 3)  # handles, conversations, messages

    def test_every_s3_key_column_sits_in_a_tenant_folder(self):
        found = 0
        for table, name, definition in self._columns():
            if name.endswith("_s3_key"):
                found += 1
                with self.subTest(column=f"{table}.{name}"):
                    self.assertIn(
                        f"REGEXP_LIKE({name}, '^tenants/[a-z0-9]+(-[a-z0-9]+)*/[A-Za-z0-9]', 'c')",
                        definition,
                    )
        self.assertGreaterEqual(found, 1)


def _created_names(sql: str) -> list[tuple[str, str]]:
    """(what, name) for every name the SQL creates: tables, columns, keys, constraints and query
    aliases. Comments and string literals are skipped."""
    code = re.sub(r"'(?:[^']|'')*'", "''", _code(sql))
    found: list[tuple[str, str]] = []
    for m in re.finditer(
        r"CREATE TABLE (?:IF NOT EXISTS )?(\w+) \((.*?)\n\) ", code, re.DOTALL
    ):
        found.append(("table", m.group(1)))
        for column in _columns(m.group(2)):
            found.append((f"column of {m.group(1)}", column))
        for key, _ in KEY_RE.findall(m.group(2)):
            found.append(("key", key))
    found += [("constraint", n) for n in re.findall(r"\bCONSTRAINT (\w+)", code)]
    # Aliases are lowercase: our SQL words are uppercase, our names lowercase.
    found += [("alias", n) for n in re.findall(r"\b(?:FROM|JOIN) \w+ ([a-z_][a-z0-9_]*)\b", code)]
    return found


class ReservedWords(unittest.TestCase):
    """No name we create is a SQL keyword or reserved word, in any engine we list (#43)."""

    def _sources(self) -> dict[str, str]:
        out = {f: _read(f) for f in _files()}
        out["rudi_platform.db.migrate.BOOTSTRAP"] = migrate.BOOTSTRAP + ";"
        return out

    def test_the_scan_sees_every_kind_of_name(self):
        names = {
            (w.split(" of ")[0], n)
            for s in self._sources().values()
            for w, n in _created_names(s)
        }
        for expected in [
            ("table", "audit_trail_events"),
            ("column", "tenant_id"),
            ("column", "migration_number"),
            ("column", "tenant_or_platform"),
            ("key", "takeovers_one_open_uq"),
            ("key", "migrations_number_uq"),
            ("alias", "p"),
        ]:
            self.assertIn(expected, names)
        self.assertGreater(len(names), 250)

    def test_the_lists_know_the_words_we_replaced(self):
        for word in ("access", "audit", "usage", "name", "key", "number", "at", "release", "raw"):
            self.assertTrue(clashes(word), word)
            self.assertTrue(clashes(word.upper()), word)

    def test_no_created_name_is_a_keyword(self):
        bad = [
            f"{source}: {what} `{name}` is a keyword in {', '.join(clashes(name))}"
            for source, sql in self._sources().items()
            for what, name in _created_names(sql)
            if clashes(name)
        ]
        self.assertEqual(bad, [], "make the name specific (display_name, not name)")


class Seed(unittest.TestCase):
    def test_seed_file_is_generated_from_the_catalogue(self):
        self.assertEqual(
            _read("0002_seed_access.sql"),
            seed.render(),
            "regenerate: python -m rudi_platform.access.seed > db/migrations/0002_seed_access.sql",
        )

    def test_seed_ids_are_minted_by_the_runner(self):
        statements = migrate.split_statements(_read("0002_seed_access.sql"))
        minted = migrate.with_ids(statements[0])
        self.assertNotIn("{new_id:", minted)
        ids = re.findall(r"'(prm_[0-9a-z]{32})'", minted)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(ID_RE.match(i) for i in ids))


if __name__ == "__main__":
    unittest.main()
