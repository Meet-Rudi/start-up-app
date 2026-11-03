"""
MEET_RUDI platform — the permission catalogue and the seed roles (decision #33).

Access is configured, not coded. Code checks permissions; roles are data that bundle them.
This module holds the two things that must exist before anyone can configure anything:

  - PERMISSIONS: every permission the platform checks. A permission is either platform-scoped
    (meaningful only platform-wide) or tenant-scoped (granted within one tenant).
  - SEED_ROLES: the roles every environment starts with. A super-admin defines further roles from
    the same catalogue and marks tenant-scoped ones as assignable by tenant admins
    (policy.AccessPolicy.check_definition).

db/migrations/0002_seed_access.sql is generated from this file (`python -m
rudi_platform.access.seed`); a test fails if the two disagree. Never rename a permission key:
stored roles reference it. Add a new key and retire the old one instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Scope(str, Enum):
    PLATFORM = "platform"
    TENANT = "tenant"


@dataclass(frozen=True)
class Permission:
    key: str
    scope: Scope
    description: str


@dataclass(frozen=True)
class Role:
    key: str
    name: str
    scope: Scope
    permissions: frozenset[str] = field(default_factory=frozenset)
    assignable_by_tenant_admin: bool = False
    seed: bool = False


P, T = Scope.PLATFORM, Scope.TENANT

PERMISSIONS: tuple[Permission, ...] = (
    # Platform-wide: only our team holds these.
    Permission("platform.tenants.manage", P, "Create, suspend and offboard tenants; assign cells"),
    Permission("platform.tenants.read", P, "Read every tenant's configuration (no transcripts)"),
    Permission("platform.breakglass", P, "Open an audited, time-boxed view into one tenant's data"),
    Permission("platform.roles.define", P, "Define new roles from the permission catalogue"),
    Permission("platform.roles.assign", P, "Assign any role, platform-wide or in any tenant"),
    Permission("platform.scope.vet", P, "Vet the legal side of a tenant's scope request"),
    Permission("platform.clone.review", P, "Approve a clone version for live"),
    Permission("platform.skills.mint", P, "Run minting rounds and register skill versions"),
    Permission("platform.skills.release", P, "Release a skill version that passed its gate"),
    Permission("platform.arena.run", P, "Run Training Arena batches"),
    # Within one tenant.
    Permission("clone.read", T, "See clones, versions, personas and settings"),
    Permission("clone.configure", T, "Edit draft clone versions: personas, skills, overrides"),
    Permission("clone.publish.staging", T, "Move a draft clone version to staging"),
    Permission("clone.publish.live.request", T, "Ask for a staging version to go live"),
    Permission("knowledge.manage", T, "Upload and remove knowledge documents"),
    Permission("scope.request", T, "Describe desired clone capabilities for matching and minting"),
    Permission("transcripts.read", T, "Read pseudonymised conversation transcripts"),
    Permission("conversation.takeover", T, "Take over a live conversation as an operator"),
    Permission("keys.manage", T, "Create and revoke API keys"),
    Permission("webhooks.manage", T, "Create, change and remove webhooks"),
    Permission("templates.manage", T, "Manage out-of-window message templates"),
    Permission("usage.read", T, "See usage and cost"),
    Permission("users.manage", T, "Invite and remove the tenant's console users"),
    Permission("roles.assign", T, "Assign roles marked assignable by tenant admins"),
)  # fmt: skip

PERMISSION_INDEX: dict[str, Permission] = {p.key: p for p in PERMISSIONS}
TENANT_PERMISSIONS = frozenset(p.key for p in PERMISSIONS if p.scope is T)
PLATFORM_PERMISSIONS = frozenset(p.key for p in PERMISSIONS if p.scope is P)

SEED_ROLES: tuple[Role, ...] = (
    Role(
        "platform-super-admin",
        "Platform super-admin",
        P,
        PLATFORM_PERMISSIONS | TENANT_PERMISSIONS,
        seed=True,
    ),
    Role(
        "platform-support",
        "Platform support",
        P,
        frozenset(
            {"platform.tenants.read", "platform.breakglass", "clone.read", "usage.read"}
        ),
        seed=True,
    ),
    Role(
        "platform-skill-tester",
        "Platform skill tester",
        P,
        frozenset({"platform.arena.run", "platform.skills.mint", "clone.read"}),
        seed=True,
    ),
    Role(
        "tester",
        "Tester (Training Arena)",
        P,
        frozenset({"platform.arena.run"}),
        seed=True,
    ),
    Role(
        "tenant-admin",
        "Tenant admin",
        T,
        TENANT_PERMISSIONS,
        assignable_by_tenant_admin=True,
        seed=True,
    ),
    Role(
        "clone-designer",
        "Clone designer",
        T,
        frozenset(
            {
                "clone.read",
                "clone.configure",
                "clone.publish.staging",
                "knowledge.manage",
                "scope.request",
                "transcripts.read",
                "usage.read",
            }
        ),
        assignable_by_tenant_admin=True,
        seed=True,
    ),
    Role(
        "tenant-operator",
        "Tenant operator",
        T,
        frozenset({"clone.read", "transcripts.read", "conversation.takeover"}),
        assignable_by_tenant_admin=True,
        seed=True,
    ),
)

SEED_ROLE_KEYS = frozenset(r.key for r in SEED_ROLES)
