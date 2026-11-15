"""
The tenant guard (decision #47): no statement on a tenant's rows reaches PlanetScale unless it
names that tenant.

What this guards: a statement that forgets the tenant, one that names the tenant where it does
not restrict the rows (under OR, inside BETWEEN or CASE, in a LEFT JOIN's ON for the left table,
inside a correlated subquery), one that names *another* tenant, an upsert that could land on
another tenant's row, a row moved between tenants, a refused statement reaching the database
anyway, and a parameter value showing up in an error.

Run:  python -m unittest discover -s packages/rudi_platform/tests -v
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..")))

from rudi_platform.db.tenant_guard import (
    RESOLVERS,
    TenantBound,
    UnscopedStatement,
    check_statement,
    resolve,
)
from rudi_platform.ids import Cat, new_id

A = new_id(Cat.TENANT)
B = new_id(Cat.TENANT)
CONV = new_id(Cat.CONVERSATION)


class RecordingDb:
    def __init__(self):
        self.sent = []

    def query(self, sql, params=()):
        self.sent.append((sql, tuple(params)))
        return []

    def execute(self, sql, params=()):
        self.sent.append((sql, tuple(params)))
        return 1


def ok(sql, params=(), tenant=A):
    check_statement(sql, params, tenant)


class Accepted(unittest.TestCase):
    """The statement shapes the platform's stores need."""

    def test_reads(self):
        ok("SELECT id, status FROM runtime_conversations WHERE tenant_id = %s AND id = %s", (A, CONV))
        ok("SELECT c.id FROM runtime_conversations c WHERE c.id = %s AND c.tenant_id = %s", (CONV, A))
        ok("SELECT COUNT(*) FROM runtime_turns AS t WHERE t.tenant_id = %s", (A,))
        ok(
            "SELECT id FROM runtime_messages WHERE tenant_id = %s AND (direction = %s OR author = %s) "
            "ORDER BY created_at DESC LIMIT %s",
            (A, "in", "user", 20),
        )
        ok("SELECT id FROM runtime_turns WHERE tenant_id = %s FOR UPDATE", (A,))

    def test_joins_each_naming_the_tenant(self):
        ok(
            "SELECT m.id, t.outcome FROM runtime_messages m "
            "JOIN runtime_turns t ON t.id = m.turn_id AND t.tenant_id = %s "
            "WHERE m.tenant_id = %s AND m.conversation_id = %s",
            (A, A, CONV),
        )
        ok(
            "SELECT c.id, e.id FROM runtime_conversations c "
            "LEFT JOIN runtime_escalations e ON e.conversation_id = c.id AND e.tenant_id = %s "
            "WHERE c.tenant_id = %s",
            (A, A),
        )
        ok(
            "SELECT c.id FROM runtime_conversations c JOIN runtime_turns t ON t.conversation_id = c.id "
            "WHERE c.tenant_id = %s AND t.tenant_id = %s",
            (A, A),
        )

    def test_subqueries_each_naming_the_tenant(self):
        ok(
            "SELECT id FROM runtime_conversations c WHERE c.tenant_id = %s AND c.id IN "
            "(SELECT t.conversation_id FROM runtime_turns t WHERE t.tenant_id = %s AND t.outcome = %s)",
            (A, A, "blocked"),
        )
        ok(
            "SELECT d.n FROM (SELECT COUNT(*) AS n FROM runtime_turns WHERE tenant_id = %s) d",
            (A,),
        )

    def test_writes(self):
        ok(
            "INSERT INTO runtime_turns (id, tenant_id, conversation_id) VALUES (%s, %s, %s), (%s, %s, %s)",
            ("trn_1", A, CONV, "trn_2", A, CONV),
        )
        ok(
            "INSERT INTO metering_budgets (id, tenant_id, spent) VALUES (%s, %s, %s) "
            "AS incoming ON DUPLICATE KEY UPDATE spent = spent + incoming.spent",
            ("bud_1", A, 3),
        )
        ok(
            "UPDATE runtime_conversations SET state_rev = state_rev + 1, status = %s "
            "WHERE tenant_id = %s AND id = %s AND state_rev = %s",
            ("open", A, CONV, 4),
        )
        ok("DELETE FROM runtime_messages WHERE tenant_id = %s AND conversation_id = %s", (A, CONV))
        ok("DELETE FROM runtime_messages m WHERE m.tenant_id = %s", (A,))

    def test_the_platforms_own_rows(self):
        ok("SELECT id FROM ops_alerts WHERE tenant_id IS NULL AND status = %s", ("open",), None)
        ok(
            "INSERT INTO audit_trail_events (id, tenant_id, action_key) VALUES (%s, NULL, %s)",
            ("aud_1", "tenant.create"),
            None,
        )
        ok("INSERT INTO ops_jobs (id, tenant_id) VALUES (%s, %s)", ("job_1", None), None)

    def test_tables_of_no_tenant_need_no_condition(self):
        for tenant in (A, None):
            ok("SELECT id, slug, cell_id FROM registry_tenants WHERE slug = %s", ("cm",), tenant)
            ok("SELECT 1", (), tenant)
            ok(
                "SELECT r.role_key FROM access_control_roles r "
                "JOIN access_control_role_permissions rp ON rp.role_id = r.id",
                (),
                tenant,
            )
            ok("UPDATE access_control_sessions SET last_seen_at = UTC_TIMESTAMP(6) WHERE id = %s",
               ("ses_1",), tenant)


class Refused(unittest.TestCase):
    def refused(self, sql, params=(), tenant=A, says=""):
        with self.assertRaises(UnscopedStatement) as caught:
            check_statement(sql, params, tenant)
        message = str(caught.exception)
        self.assertIn("decision #47", message)
        if says:
            self.assertIn(says, message)
        for p in params:
            if isinstance(p, str) and len(p) > 3:
                self.assertNotIn(p, message, "a parameter value reached the error")
        return message

    def test_no_tenant_condition(self):
        self.refused("SELECT id FROM runtime_conversations", says="runtime_conversations")
        self.refused("SELECT id FROM runtime_conversations WHERE id = %s", (CONV,))
        self.refused("DELETE FROM runtime_messages WHERE conversation_id = %s", (CONV,))
        self.refused("UPDATE runtime_turns SET outcome = %s", ("ok",))

    def test_another_tenants_id(self):
        self.refused("SELECT id FROM runtime_turns WHERE tenant_id = %s", (B,))
        self.refused("INSERT INTO runtime_turns (id, tenant_id) VALUES (%s, %s)", ("trn_1", B))
        self.refused(
            "SELECT m.id FROM runtime_messages m JOIN runtime_turns t ON t.id = m.turn_id "
            "AND t.tenant_id = %s WHERE m.tenant_id = %s",
            (B, A),
            says="runtime_turns",
        )

    def test_a_condition_that_does_not_restrict_the_rows(self):
        # Under OR: (tenant_id = A AND id = x) OR status = 'open' reads every tenant's open rows.
        self.refused(
            "SELECT id FROM runtime_conversations WHERE tenant_id = %s AND id = %s OR status = %s",
            (A, CONV, "open"),
            says="OR",
        )
        # = binds tighter than BETWEEN: a BETWEEN 1 AND (tenant_id = A).
        self.refused(
            "SELECT id FROM runtime_turns WHERE latency_ms BETWEEN 1 AND tenant_id = %s", (A,),
            says="BETWEEN",
        )
        self.refused(
            "SELECT id FROM runtime_turns WHERE CASE WHEN latency_ms > 1 AND tenant_id = %s "
            "THEN 1 END",
            (A,),
        )
        self.refused("SELECT id FROM runtime_turns WHERE NOT tenant_id = %s", (A,))
        self.refused("SELECT id FROM runtime_turns WHERE (tenant_id = %s AND id = %s)", (A, "x"))
        self.refused("SELECT id FROM runtime_turns WHERE tenant_id IN (%s)", (A,))
        self.refused("SELECT id FROM runtime_turns WHERE tenant_id = %s XOR id = %s", (A, "x"))
        self.refused(f"SELECT id FROM runtime_turns WHERE tenant_id = '{A}'")

    def test_the_left_table_of_a_left_join_needs_its_condition_in_where(self):
        self.refused(
            "SELECT c.id FROM runtime_conversations c LEFT JOIN runtime_turns t "
            "ON t.conversation_id = c.id AND c.tenant_id = %s AND t.tenant_id = %s",
            (A, A),
            says="runtime_conversations",
        )

    def test_a_condition_in_another_query_level_does_not_count(self):
        # NOT IN with the outer table's condition inside the subquery returns other tenants.
        self.refused(
            "SELECT c.id FROM runtime_conversations c WHERE c.id NOT IN "
            "(SELECT t.conversation_id FROM runtime_turns t WHERE t.tenant_id = %s "
            "AND c.tenant_id = %s)",
            (A, A),
            says="runtime_conversations",
        )
        self.refused(
            "SELECT c.id FROM runtime_conversations c WHERE c.tenant_id = %s AND EXISTS "
            "(SELECT 1 FROM runtime_turns t WHERE t.conversation_id = c.id)",
            (A,),
            says="runtime_turns",
        )

    def test_with_two_tables_the_condition_names_its_table(self):
        self.refused(
            "SELECT m.id FROM runtime_messages m JOIN runtime_turns t ON t.id = m.turn_id "
            "WHERE tenant_id = %s",
            (A,),
        )

    def test_writes_that_could_cross_tenants(self):
        self.refused("INSERT INTO runtime_turns (id, conversation_id) VALUES (%s, %s)", ("t", CONV))
        self.refused(f"INSERT INTO runtime_turns (id, tenant_id) VALUES (%s, '{A}')", ("trn_1",))
        self.refused(
            "INSERT INTO registry_endpoints (id, tenant_id, channel, address) VALUES (%s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE status = 'active'",
            ("enp_1", A, "whatsapp", "+32000000000"),
            says="another tenant's row",
        )
        self.refused(
            "INSERT INTO metering_budgets (id, tenant_id) VALUES (%s, %s) "
            "ON DUPLICATE KEY UPDATE tenant_id = %s",
            ("bud_1", A, B),
        )
        self.refused(
            "UPDATE runtime_conversations SET tenant_id = %s WHERE tenant_id = %s", (B, A),
            says="never move",
        )
        self.refused(
            "INSERT INTO runtime_turns (id, tenant_id) SELECT id, tenant_id FROM runtime_turns "
            "WHERE tenant_id = %s",
            (A,),
        )
        self.refused("REPLACE INTO runtime_turns (id, tenant_id) VALUES (%s, %s)", ("t", A))
        self.refused(
            "DELETE m FROM runtime_messages m JOIN runtime_turns t ON t.id = m.turn_id "
            "WHERE m.tenant_id = %s AND t.tenant_id = %s",
            (A, A),
        )
        self.refused(
            "UPDATE runtime_messages m JOIN runtime_turns t ON t.id = m.turn_id SET m.media = %s "
            "WHERE m.tenant_id = %s AND t.tenant_id = %s",
            ("[]", A, A),
        )

    def test_what_the_guard_cannot_read_exactly(self):
        for sql in (
            "SELECT id FROM runtime_turns WHERE tenant_id = %s; DELETE FROM runtime_turns",
            "SELECT id FROM runtime_turns WHERE tenant_id = %s -- and more",
            "SELECT id FROM runtime_turns WHERE tenant_id = %s # and more",
            "SELECT id FROM runtime_turns WHERE tenant_id = %s /* and more */",
            "SELECT id FROM `runtime_turns` WHERE tenant_id = %s",
            "SELECT id FROM runtime_turns WHERE tenant_id = %s UNION SELECT id FROM runtime_messages",
            "WITH x AS (SELECT 1) SELECT id FROM runtime_turns WHERE tenant_id = %s",
            "SELECT t.id FROM runtime_turns t, runtime_messages m WHERE t.tenant_id = %s",
            (
                "SELECT t.id FROM runtime_messages m RIGHT JOIN runtime_turns t "
                "ON t.id = m.turn_id WHERE t.tenant_id = %s"
            ),
            "SELECT id FROM runtime_turns WHERE tenant_id = %s AND id LIKE 'a%'",
            "SELECT id FROM runtime_turns WHERE tenant_id = ?",
            "SELECT id FROM other_db.runtime_turns WHERE tenant_id = %s",
            "SELECT id FROM runtime_turns WHERE tenant_id = %s || id = 'x'",
            "SELECT id FROM runtime_turns WHERE (tenant_id = %s",
            "TRUNCATE TABLE runtime_turns",
            "DROP TABLE runtime_turns",
            "SELECT id INTO OUTFILE 'x' FROM runtime_turns WHERE tenant_id = %s",
        ):
            with self.subTest(sql=sql):
                self.refused(sql, (A,) if "%s" in sql else ())

    def test_placeholders_and_parameters_must_match(self):
        self.refused("SELECT id FROM runtime_turns WHERE tenant_id = %s", ())
        self.refused("SELECT id FROM runtime_turns WHERE tenant_id = %s", (A, "extra"))
        self.refused("SELECT id FROM runtime_turns WHERE tenant_id = %s", A)  # a string, not a tuple

    def test_a_platform_guard_reaches_only_platform_rows(self):
        self.refused("SELECT id FROM ops_alerts WHERE tenant_id = %s", (A,), None)
        self.refused("SELECT id FROM ops_alerts WHERE tenant_id IS NULL", (), A)
        self.refused("INSERT INTO ops_jobs (id, tenant_id) VALUES (%s, NULL)", ("job_1",), A)

    def test_a_table_the_guard_does_not_know_is_a_tenants(self):
        self.refused("SELECT id FROM runtime_something_new", says="runtime_something_new")


class Connection(unittest.TestCase):
    def test_a_refused_statement_is_never_sent(self):
        db = RecordingDb()
        bound = TenantBound(db, A)
        with self.assertRaises(UnscopedStatement):
            bound.query("SELECT id FROM runtime_turns")
        with self.assertRaises(UnscopedStatement):
            bound.execute("DELETE FROM runtime_turns WHERE id = %s", ("trn_1",))
        self.assertEqual(db.sent, [])
        bound.query("SELECT id FROM runtime_turns WHERE tenant_id = %s", (A,))
        self.assertEqual(len(db.sent), 1)

    def test_a_guard_needs_a_tenant_id(self):
        for bad in ("", "cm", new_id(Cat.CLONE), A.upper()):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                TenantBound(RecordingDb(), bad)
        self.assertIsNone(TenantBound.platform(RecordingDb()).tenant_id)


class Resolvers(unittest.TestCase):
    def test_a_resolver_runs_its_fixed_statement(self):
        db = RecordingDb()
        resolve(db, "endpoint_by_address", ("whatsapp", "+32000000000"))
        self.assertEqual(db.sent, [(RESOLVERS["endpoint_by_address"].sql, ("whatsapp", "+32000000000"))])

    def test_anything_else_is_refused(self):
        db = RecordingDb()
        with self.assertRaises(UnscopedStatement):
            resolve(db, "conversations_of_everyone", ())
        with self.assertRaises(UnscopedStatement):
            resolve(db, "api_key_by_hash", ())
        self.assertEqual(db.sent, [])

    def test_resolvers_are_exactly_the_unscoped_reads(self):
        for key, r in RESOLVERS.items():
            with self.subTest(resolver=key):
                self.assertTrue(r.sql.startswith("SELECT tenant_id,"), "a resolver returns the tenant first")
                with self.assertRaises(UnscopedStatement):
                    check_statement(r.sql, tuple("x" for _ in r.params), A)


if __name__ == "__main__":
    unittest.main()
