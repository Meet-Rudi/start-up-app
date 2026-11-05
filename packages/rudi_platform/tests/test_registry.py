"""
The registry contract — every RegistryStore implementation must pass this file.

Runs against InMemoryRegistryStore today. When the PlanetScale store exists, add its factory to
STORES (behind an environment switch, since it needs a database) and the same tests hold it to
the same behaviour, including the tenant isolation the database enforces with row-level security.

What this guards: one tenant seeing or writing another's data, a tenant approving its own clone
for live, a published version changing underneath running conversations, a skill released
without a gate, an endpoint address shared by two clones, and an unaudited publish.

Run:  python -m unittest discover -s packages/rudi_platform/tests -v
"""

from __future__ import annotations

import datetime
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..")))

from rudi_platform.access import Role, Scope
from rudi_platform.ids import Cat, is_id, new_id
from rudi_platform.registry import (
    PLATFORM,
    Conflict,
    Forbidden,
    InMemoryRegistryStore,
    InvalidState,
    InvalidValue,
    NotFound,
    PublishRefused,
    TenantContext,
    approve_live,
    publish_to_staging,
)
from skill_fixtures import inventory

NOW = datetime.datetime(2026, 10, 29, 9, 0, tzinfo=datetime.UTC)
STORES = {"memory": lambda: InMemoryRegistryStore(clock=lambda: NOW)}
OCEAN = {"O": 59, "C": 71, "E": 57, "A": 60, "N": 19}
COMPOSITION = {
    "channels": ["whatsapp", "voice"],
    "locales": ["nl", "en"],
    "skills": [
        {"skill": "journey-engine", "major": 1},
        {"skill": "goal-setting", "major": 1},
        {"skill": "crisis-signposting", "major": 1},
    ],
}
JOURNEY = {
    "format": "rudi-journey@1",
    "start": "goal",
    "stages": {"goal": {"skill": "goal-setting"}},
}


class Contract:
    """Mixed into one TestCase per store implementation."""

    make = staticmethod(STORES["memory"])

    def setUp(self):
        self.s = self.make()
        self.pool = self.s.create_cell(PLATFORM, "pool", "pooled")
        self.a = self.s.create_tenant(PLATFORM, "cm", "CM", self.pool.id)
        self.b = self.s.create_tenant(PLATFORM, "acme", "Acme", self.pool.id)
        self.ca, self.cb = TenantContext(self.a.id), TenantContext(self.b.id)
        self.clone = self.s.create_clone(
            self.ca,
            self.a.id,
            "rudi",
            "Rudi for CM",
            "Lifestyle coaching for CM members",
        )

    # ---------------------------------------------------------------------------- isolation
    def test_a_tenant_never_sees_another(self):
        with self.assertRaises(NotFound):
            self.s.get_clone(self.cb, self.clone.id)
        with self.assertRaises(NotFound):
            self.s.get_tenant(self.cb, self.a.id)
        self.assertEqual(self.s.list_clones(self.cb, self.a.id), [])
        self.assertEqual(self.s.get_clone(PLATFORM, self.clone.id), self.clone)
        with self.assertRaises(Forbidden):
            self.s.create_clone(
                self.cb,
                self.a.id,
                "sneaky",
                "Sneaky",
                "A clone in someone else's tenant",
            )
        with self.assertRaises(InvalidValue):
            self.s.get_clone(TenantContext("not-an-id"), self.clone.id)

    def test_platform_operations_need_the_platform(self):
        calls = [
            lambda: self.s.create_cell(self.ca, "d-cm", "dedicated"),
            lambda: self.s.create_tenant(self.ca, "x", "X", self.pool.id),
            lambda: self.s.set_tenant_status(self.ca, self.a.id, "suspended"),
            lambda: self.s.register_skill_version(
                self.ca, inventory()["journey-engine"][0]
            ),
            lambda: self.s.create_account(self.ca, "a@example.org"),
            lambda: self.s.define_role(
                self.ca, Role("analyst", "Analyst", Scope.TENANT)
            ),
            lambda: self.s.route(self.ca, "whatsapp", "+3200000000"),
        ]
        for call in calls:
            with self.assertRaises(Forbidden):
                call()

    # ------------------------------------------------------------------------ cells, tenants
    def test_cells_and_tenants(self):
        self.assertTrue(is_id(self.a.id, Cat.TENANT))
        with self.assertRaises(Conflict):
            self.s.create_cell(PLATFORM, "pool", "pooled")
        with self.assertRaises(InvalidValue):
            self.s.create_cell(PLATFORM, "d-x", "pooled")
        with self.assertRaises(Conflict):
            self.s.create_tenant(PLATFORM, "cm", "Again", self.pool.id)
        with self.assertRaises(NotFound):
            self.s.create_tenant(PLATFORM, "y", "Y", new_id(Cat.CELL))
        self.assertEqual(
            self.s.set_tenant_status(PLATFORM, self.b.id, "suspended").status,
            "suspended",
        )

    # ---------------------------------------------------------------------- clones, versions
    def test_clone_rules(self):
        with self.assertRaises(Conflict):
            self.s.create_clone(
                self.ca, self.a.id, "rudi", "Again", "Lifestyle coaching again"
            )
        self.s.create_clone(
            self.cb, self.b.id, "rudi", "Rudi for Acme", "Coaching for Acme staff"
        )
        with self.assertRaises(InvalidValue):
            self.s.create_clone(self.ca, self.a.id, "short", "Short", "too short")
        with self.assertRaises(InvalidValue):
            self.s.create_clone(
                self.ca, self.a.id, "Bad Slug", "Bad", "A perfectly fine purpose"
            )

    def test_versions_are_numbered_and_copied(self):
        v1 = self.s.create_draft(self.ca, self.clone.id)
        self.s.update_draft(
            self.ca, v1.id, composition=COMPOSITION, settings={"quiet": "21:30"}
        )
        self.s.put_persona(
            self.ca, v1.id, "rudi", "Rudi", OCEAN, languages=["nl", "en"]
        )
        v2 = self.s.create_draft(self.ca, self.clone.id, from_version_id=v1.id)
        self.assertEqual((v1.number, v2.number), (1, 2))
        self.assertEqual(v2.composition, COMPOSITION)
        self.assertEqual([p.key for p in self.s.personas(self.ca, v2.id)], ["rudi"])
        self.assertNotEqual(
            self.s.personas(self.ca, v2.id)[0].id, self.s.personas(self.ca, v1.id)[0].id
        )
        self.assertEqual(
            [v.number for v in self.s.list_versions(self.ca, self.clone.id)], [1, 2]
        )

    def test_returned_records_are_copies(self):
        v1 = self.s.create_draft(self.ca, self.clone.id)
        self.s.update_draft(self.ca, v1.id, composition=COMPOSITION)
        got = self.s.get_version(self.ca, v1.id)
        got.composition["skills"].clear()
        self.assertEqual(
            len(self.s.get_version(self.ca, v1.id).composition["skills"]), 3
        )

    def test_lifecycle_and_immutability(self):
        v1 = self.s.create_draft(self.ca, self.clone.id)
        self.s.put_persona(self.ca, v1.id, "rudi", "Rudi", OCEAN)
        with self.assertRaises(InvalidState):
            self.s.promote(PLATFORM, v1.id, "live")  # draft cannot skip staging
        staged = self.s.promote(self.ca, v1.id, "staging")
        self.assertEqual((staged.stage, staged.published_at), ("staging", NOW))
        with self.assertRaises(InvalidState):
            self.s.update_draft(self.ca, v1.id, settings={"x": 1})
        with self.assertRaises(InvalidState):
            self.s.put_persona(self.ca, v1.id, "emma", "Emma", OCEAN)
        with self.assertRaises(InvalidState):
            self.s.remove_persona(self.ca, v1.id, "rudi")
        with self.assertRaises(Forbidden):
            self.s.promote(self.ca, v1.id, "live")  # going live is our review
        self.s.promote(PLATFORM, v1.id, "live")
        v2 = self.s.create_draft(self.ca, self.clone.id, from_version_id=v1.id)
        self.s.promote(self.ca, v2.id, "staging")
        self.s.promote(PLATFORM, v2.id, "live")
        self.assertEqual(self.s.get_version(self.ca, v1.id).stage, "retired")
        clone = self.s.get_clone(self.ca, self.clone.id)
        self.assertEqual((clone.status, clone.live_version_id), ("live", v2.id))

    # ----------------------------------------------------------------------------- personas
    def test_personas(self):
        v1 = self.s.create_draft(self.ca, self.clone.id)
        first = self.s.put_persona(self.ca, v1.id, "rudi", "Rudi", OCEAN)
        again = self.s.put_persona(
            self.ca, v1.id, "rudi", "Rudi", {**OCEAN, "E": 80}, briefing="Puns."
        )
        self.assertEqual(first.id, again.id)
        self.assertEqual(self.s.personas(self.ca, v1.id)[0].ocean["E"], 80)
        with self.assertRaises(InvalidValue):
            self.s.put_persona(self.ca, v1.id, "emma", "Emma", {"O": 1})
        with self.assertRaises(NotFound):
            self.s.personas(self.cb, v1.id)
        self.s.remove_persona(self.ca, v1.id, "rudi")
        self.assertEqual(self.s.personas(self.ca, v1.id), [])

    # ------------------------------------------------------------------ endpoints, routing
    def test_endpoints_and_routing(self):
        ep = self.s.add_endpoint(self.ca, self.clone.id, "whatsapp", "+32470000001")
        other = self.s.create_clone(
            self.cb, self.b.id, "coach", "Coach", "Coaching for Acme staff"
        )
        with self.assertRaises(Conflict) as caught:
            self.s.add_endpoint(self.cb, other.id, "whatsapp", "+32470000001")
        self.assertNotIn(self.a.id, str(caught.exception))  # never reveals whose it is
        with self.assertRaises(InvalidValue):
            self.s.add_endpoint(self.ca, self.clone.id, "fax", "123")
        self.assertIsNone(
            self.s.route(PLATFORM, "whatsapp", "+32470000001")
        )  # not live yet
        v1 = self.s.create_draft(self.ca, self.clone.id)
        self.s.promote(self.ca, v1.id, "staging")
        self.s.promote(PLATFORM, v1.id, "live")
        target = self.s.route(PLATFORM, "whatsapp", "+32470000001")
        self.assertEqual(
            (
                target.endpoint_id,
                target.tenant_id,
                target.clone_version_id,
                target.cell_slug,
            ),
            (ep.id, self.a.id, v1.id, "pool"),
        )
        self.assertIsNone(self.s.route(PLATFORM, "whatsapp", "+32470000999"))
        self.s.set_tenant_status(PLATFORM, self.a.id, "suspended")
        self.assertIsNone(self.s.route(PLATFORM, "whatsapp", "+32470000001"))

    # ------------------------------------------------------------------------------- skills
    def test_skills_release_only_through_a_gate(self):
        inv = inventory()
        rec = self.s.register_skill_version(
            PLATFORM, inv["goal-setting"][0], "evals/skill-briefs/goal-setting.skill.md"
        )
        self.assertEqual(self.s.released_manifests(self.ca), {})
        with self.assertRaises(InvalidValue):
            self.s.release_skill_version(PLATFORM, rec.id, "passed")
        released = self.s.release_skill_version(PLATFORM, rec.id, new_id(Cat.GATE_RUN))
        self.assertEqual((released.status, released.released_at), ("released", NOW))
        with self.assertRaises(InvalidState):
            self.s.release_skill_version(PLATFORM, rec.id, new_id(Cat.GATE_RUN))
        self.assertEqual(
            [m.version for m in self.s.released_manifests(self.cb)["goal-setting"]],
            [(1, 0, 0)],
        )
        with self.assertRaises(Conflict):
            self.s.register_skill_version(PLATFORM, inv["goal-setting"][0])
        from skill_fixtures import m

        with self.assertRaises(Conflict):  # a slug keeps its kind
            self.s.register_skill_version(
                PLATFORM, m("goal-setting", "3.0.0", kind="background")
            )

    # -------------------------------------------------------------- accounts, roles, grants
    def test_accounts_roles_and_assignments(self):
        acc = self.s.create_account(PLATFORM, "  Ann@Example.ORG ", "Ann")
        self.assertEqual(acc.email, "ann@example.org")
        self.assertEqual(
            self.s.account_by_email(PLATFORM, "ANN@example.org").id, acc.id
        )
        with self.assertRaises(Conflict):
            self.s.create_account(PLATFORM, "ann@example.org")
        with self.assertRaises(InvalidValue):
            self.s.create_account(PLATFORM, "no-at-sign")

        analyst = self.s.define_role(
            PLATFORM,
            Role("analyst", "Analyst", Scope.TENANT, frozenset({"usage.read"}), True),
        )
        self.assertIn(analyst, self.s.roles(self.ca))
        with self.assertRaises(Conflict):
            self.s.define_role(PLATFORM, Role("tenant-admin", "Again", Scope.TENANT))
        with self.assertRaises(InvalidValue):
            self.s.define_role(
                PLATFORM,
                Role("bad", "Bad", Scope.TENANT, frozenset({"platform.arena.run"})),
            )

        in_a = self.s.assign_role(self.ca, acc.id, "tenant-admin", self.a.id)
        self.s.assign_role(PLATFORM, acc.id, "platform-support")
        with self.assertRaises(Forbidden):
            self.s.assign_role(self.cb, acc.id, "tenant-admin", self.a.id)
        with self.assertRaises(Forbidden):
            self.s.assign_role(self.ca, acc.id, "platform-skill-tester")
        with self.assertRaises(InvalidValue):
            self.s.assign_role(PLATFORM, acc.id, "platform-support", self.a.id)
        with self.assertRaises(InvalidValue):
            self.s.assign_role(PLATFORM, acc.id, "tenant-admin")
        with self.assertRaises(Conflict):
            self.s.assign_role(self.ca, acc.id, "tenant-admin", self.a.id)

        self.assertEqual(
            {a.role_key for a in self.s.assignments_for(PLATFORM, acc.id)},
            {"tenant-admin", "platform-support"},
        )
        self.assertEqual(
            [a.role_key for a in self.s.assignments_for(self.ca, acc.id)],
            ["tenant-admin"],
        )
        self.assertEqual(self.s.assignments_for(self.cb, acc.id), [])
        with self.assertRaises(NotFound):
            self.s.revoke_assignment(self.cb, in_a.id)
        self.s.revoke_assignment(self.ca, in_a.id)
        self.assertEqual(self.s.assignments_for(self.ca, acc.id), [])

    # -------------------------------------------------------------------------------- audit
    def test_audit(self):
        ev = self.s.append_audit(self.ca, "clone.read", target_id=self.clone.id)
        self.assertEqual(ev.tenant_id, self.a.id)
        with self.assertRaises(Forbidden):
            self.s.append_audit(self.ca, "x", tenant_id=self.b.id)
        self.s.append_audit(PLATFORM, "tenant.suspend", target_id=self.b.id)
        self.assertEqual(
            [e.action for e in self.s.audit_events(self.ca)], ["clone.read"]
        )
        self.assertEqual(self.s.audit_events(self.cb), [])
        self.assertEqual(len(self.s.audit_events(PLATFORM)), 2)


class Publishing(unittest.TestCase):
    def setUp(self):
        self.s = STORES["memory"]()
        pool = self.s.create_cell(PLATFORM, "pool", "pooled")
        self.t = self.s.create_tenant(PLATFORM, "cm", "CM", pool.id)
        self.ctx = TenantContext(self.t.id)
        for versions in inventory().values():
            for manifest in versions:
                rec = self.s.register_skill_version(PLATFORM, manifest)
                self.s.release_skill_version(PLATFORM, rec.id, new_id(Cat.GATE_RUN))
        self.clone = self.s.create_clone(
            self.ctx,
            self.t.id,
            "rudi",
            "Rudi for CM",
            "Lifestyle coaching for CM members",
        )
        self.s.add_endpoint(self.ctx, self.clone.id, "whatsapp", "+32470000001")
        self.v = self.s.create_draft(self.ctx, self.clone.id)
        self.s.update_draft(
            self.ctx, self.v.id, composition=COMPOSITION, journey=JOURNEY
        )

    def test_draft_to_staging_to_live(self):
        self.s.put_persona(self.ctx, self.v.id, "rudi", "Rudi", OCEAN)
        staged = publish_to_staging(
            self.s, self.ctx, self.v.id, baseline=["crisis-signposting"]
        )
        self.assertEqual(staged.version.stage, "staging")
        self.assertEqual(staged.composed.pins()["goal-setting"], "goal-setting@1.2.0")
        live = approve_live(
            self.s, PLATFORM, self.v.id, baseline=["crisis-signposting"]
        )
        self.assertEqual(live.version.stage, "live")
        self.assertEqual(
            self.s.route(PLATFORM, "whatsapp", "+32470000001").clone_version_id,
            self.v.id,
        )
        events = self.s.audit_events(self.ctx)
        self.assertEqual(
            [e.action for e in events], ["clone.publish.staging", "clone.approve.live"]
        )
        self.assertTrue(all(e.decision["allowed"] for e in events))
        self.assertEqual(
            events[0].decision["pins"]["journey-engine"], "journey-engine@1.0.0"
        )

    def test_refusals_are_explained_and_audited(self):
        with self.assertRaises(PublishRefused) as caught:  # no persona yet
            publish_to_staging(self.s, self.ctx, self.v.id)
        self.assertIn("at least one persona", " ".join(caught.exception.problems))
        self.s.put_persona(self.ctx, self.v.id, "rudi", "Rudi", OCEAN)
        self.s.update_draft(
            self.ctx,
            self.v.id,
            composition={
                **COMPOSITION,
                "locales": ["pl"],
                "skills": COMPOSITION["skills"][1:],
            },
        )
        with self.assertRaises(PublishRefused) as caught:
            publish_to_staging(self.s, self.ctx, self.v.id)
        problems = " | ".join(caught.exception.problems)
        self.assertIn("requires journey-engine@1", problems)
        self.assertIn("no pl text for", problems)
        self.assertEqual(self.s.get_version(self.ctx, self.v.id).stage, "draft")
        refused = self.s.audit_events(self.ctx)
        self.assertEqual(len(refused), 2)
        self.assertFalse(refused[-1].decision["allowed"])

    def test_bad_string_overrides_refuse(self):
        self.s.put_persona(self.ctx, self.v.id, "rudi", "Rudi", OCEAN)
        self.s.update_draft(
            self.ctx, self.v.id, settings={"strings": {"intro": {"nl": "Hoi {naam}!"}}}
        )
        with self.assertRaises(PublishRefused) as caught:
            publish_to_staging(self.s, self.ctx, self.v.id)
        self.assertIn("strings:", " ".join(caught.exception.problems))

    def test_a_tenant_cannot_approve_its_own_live(self):
        self.s.put_persona(self.ctx, self.v.id, "rudi", "Rudi", OCEAN)
        publish_to_staging(self.s, self.ctx, self.v.id)
        with self.assertRaises(Forbidden):
            approve_live(self.s, self.ctx, self.v.id)  # type: ignore[arg-type]


for _name, _factory in STORES.items():
    globals()[f"Contract_{_name}"] = type(
        f"Contract_{_name}",
        (Contract, unittest.TestCase),
        {"make": staticmethod(_factory)},
    )


if __name__ == "__main__":
    unittest.main()
