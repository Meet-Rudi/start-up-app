"""
Access as data — permissions, seed roles, delegation and role definitions (decision #33).

What this guards: a tenant admin reaching into another tenant, support reading transcripts
without break-glass, a delegated assigner granting more than they hold, and a seed role being
quietly redefined.

Run:  python -m unittest discover -s packages/rudi_platform/tests -v
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..")))

from rudi_platform.access import (
    PERMISSIONS,
    SEED_ROLES,
    AccessError,
    AccessPolicy,
    Assignment,
    Role,
    Scope,
    validate_role,
)
from rudi_platform.ids import Cat, new_id

T1, T2 = new_id(Cat.TENANT), new_id(Cat.TENANT)
SUPER = [Assignment("platform-super-admin")]
SUPPORT = [Assignment("platform-support")]
ADMIN_T1 = [Assignment("tenant-admin", T1)]
DESIGNER_T1 = [Assignment("clone-designer", T1)]


class Catalogue(unittest.TestCase):
    def test_keys_are_unique_and_seeds_valid(self):
        keys = [p.key for p in PERMISSIONS]
        self.assertEqual(len(keys), len(set(keys)))
        AccessPolicy()  # validates every seed role
        self.assertIn("platform-skill-tester", {r.key for r in SEED_ROLES})

    def test_no_platform_role_reads_transcripts(self):
        """Support reaches transcripts only through audited break-glass, never by role."""
        for role in SEED_ROLES:
            if role.scope is Scope.PLATFORM and role.key != "platform-super-admin":
                self.assertNotIn("transcripts.read", role.permissions, role.key)


class Can(unittest.TestCase):
    def setUp(self):
        self.policy = AccessPolicy()

    def test_super_admin_everywhere(self):
        self.assertTrue(self.policy.can(SUPER, "platform.tenants.manage"))
        self.assertTrue(self.policy.can(SUPER, "keys.manage", T2))

    def test_tenant_admin_stays_in_its_tenant(self):
        self.assertTrue(self.policy.can(ADMIN_T1, "keys.manage", T1))
        self.assertFalse(self.policy.can(ADMIN_T1, "keys.manage", T2))
        self.assertFalse(self.policy.can(ADMIN_T1, "platform.tenants.read"))

    def test_support_is_read_only_and_needs_break_glass(self):
        self.assertTrue(self.policy.can(SUPPORT, "clone.read", T2))
        self.assertFalse(self.policy.can(SUPPORT, "transcripts.read", T2))
        self.assertFalse(self.policy.can(SUPPORT, "clone.configure", T2))
        self.assertTrue(self.policy.can(SUPPORT, "platform.breakglass"))

    def test_decision_names_the_granting_role(self):
        d = self.policy.can(DESIGNER_T1, "clone.configure", T1)
        self.assertEqual(
            (d.allowed, d.via, d.reason), (True, "clone-designer", "tenant role")
        )

    def test_malformed_requests_and_assignments_grant_nothing(self):
        self.assertFalse(
            self.policy.can(SUPER, "clone.teleport", T1)
        )  # unknown permission
        self.assertFalse(
            self.policy.can(ADMIN_T1, "keys.manage")
        )  # tenant permission, no tenant
        self.assertFalse(self.policy.can(ADMIN_T1, "keys.manage", "ten_nope"))
        self.assertFalse(
            self.policy.can([Assignment("tenant-admin")], "keys.manage", T1)
        )
        self.assertFalse(
            self.policy.can([Assignment("platform-support", T1)], "clone.read", T1)
        )
        self.assertFalse(
            self.policy.can([Assignment("ghost-role", T1)], "clone.read", T1)
        )

    def test_permissions_in(self):
        self.assertEqual(
            self.policy.permissions_in([Assignment("tenant-operator", T1)], T1),
            {"clone.read", "transcripts.read", "conversation.takeover"},
        )
        self.assertEqual(
            self.policy.permissions_in([Assignment("tenant-operator", T1)], T2), set()
        )


class Delegation(unittest.TestCase):
    def setUp(self):
        # A custom tenant role that may invite and assign, but holds no key management.
        self.user_manager = Role(
            "user-manager",
            "User manager",
            Scope.TENANT,
            frozenset({"clone.read", "users.manage", "roles.assign"}),
            assignable_by_tenant_admin=True,
        )
        self.locked = Role(
            "auditor", "Auditor", Scope.TENANT, frozenset({"usage.read"})
        )
        self.policy = AccessPolicy([*SEED_ROLES, self.user_manager, self.locked])

    def test_tenant_admin_assigns_within_its_tenant(self):
        self.assertTrue(self.policy.can_assign(ADMIN_T1, "clone-designer", T1))
        self.assertFalse(self.policy.can_assign(ADMIN_T1, "clone-designer", T2))
        self.assertFalse(self.policy.can_assign(ADMIN_T1, "platform-support", None))

    def test_only_assignable_roles_are_delegated(self):
        d = self.policy.can_assign(ADMIN_T1, "auditor", T1)
        self.assertFalse(d)
        self.assertIn("not assignable", d.reason)

    def test_no_escalation(self):
        manager = [Assignment("user-manager", T1)]
        self.assertTrue(self.policy.can_assign(manager, "user-manager", T1))
        d = self.policy.can_assign(manager, "tenant-admin", T1)
        self.assertFalse(d)
        self.assertIn("keys.manage", d.reason)

    def test_without_roles_assign_nothing_is_delegated(self):
        self.assertFalse(self.policy.can_assign(DESIGNER_T1, "tenant-operator", T1))

    def test_super_admin_assigns_anything_in_the_right_scope(self):
        self.assertTrue(self.policy.can_assign(SUPER, "platform-support", None))
        self.assertTrue(self.policy.can_assign(SUPER, "auditor", T2))
        self.assertFalse(self.policy.can_assign(SUPER, "platform-support", T1))
        self.assertFalse(self.policy.can_assign(SUPER, "tenant-admin", None))
        self.assertFalse(self.policy.can_assign(SUPER, "nobody", T1))


class Definitions(unittest.TestCase):
    def setUp(self):
        self.policy = AccessPolicy()

    def test_super_admin_defines_a_role_that_then_works(self):
        analyst = Role(
            "analyst", "Analyst", Scope.TENANT, frozenset({"usage.read"}), True
        )
        self.assertTrue(self.policy.check_definition(SUPER, analyst))
        extended = self.policy.with_role(analyst)
        self.assertTrue(extended.can([Assignment("analyst", T1)], "usage.read", T1))
        self.assertTrue(extended.can_assign(ADMIN_T1, "analyst", T1))

    def test_refusals(self):
        fine = Role("analyst", "Analyst", Scope.TENANT, frozenset({"usage.read"}))
        self.assertFalse(self.policy.check_definition(ADMIN_T1, fine))
        cases = [
            Role("tenant-admin", "Again", Scope.TENANT, frozenset()),  # seed key
            Role("x", "Seed-flagged", Scope.TENANT, frozenset(), seed=True),
            Role("bad", "Bad", Scope.TENANT, frozenset({"platform.arena.run"})),
            Role(
                "bad",
                "Bad",
                Scope.PLATFORM,
                frozenset(),
                assignable_by_tenant_admin=True,
            ),
            Role("bad", "Bad", Scope.TENANT, frozenset({"clone.teleport"})),
            Role("Bad Key", "Bad", Scope.TENANT, frozenset()),
            Role("bad", " ", Scope.TENANT, frozenset()),
        ]
        for role in cases:
            with self.subTest(role=role.key):
                self.assertFalse(self.policy.check_definition(SUPER, role))

    def test_validate_role_raises(self):
        with self.assertRaises(AccessError):
            validate_role(
                Role("bad", "Bad", Scope.TENANT, frozenset({"platform.arena.run"}))
            )
        with self.assertRaises(AccessError):
            AccessPolicy([*SEED_ROLES, SEED_ROLES[0]])  # duplicate key


if __name__ == "__main__":
    unittest.main()
