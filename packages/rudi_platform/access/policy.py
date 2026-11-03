"""
MEET_RUDI platform — who may do what (decision #33).

Pure decisions over role definitions and an account's assignments; no storage. The console API
loads an account's assignments and the role definitions, asks here, and writes the returned
Decision to the audit log (`aud_`), allowed or not.

Rules:
  - A platform-scoped permission is granted only by a platform-wide assignment.
  - A tenant-scoped permission in tenant T is granted by an assignment in T, or by a platform-wide
    role that contains it (super-admin, and read-only support for `clone.read` / `usage.read`).
  - Transcripts are never in a platform role: support reaches them only through break-glass,
    which is its own audited flow.
  - Assigning a tenant role in T needs `roles.assign` in T, a role marked assignable by tenant
    admins, and every permission of that role already held in T (no escalation). Holders of
    `platform.roles.assign` may assign anything.
  - New roles need `platform.roles.define`, a fresh key, known permissions, and a tenant role
    may carry tenant permissions only. Seed roles cannot be redefined.
  - Assignments that do not fit their role's scope, or name unknown roles, grant nothing.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from ..ids import Cat, is_id
from .catalogue import PERMISSION_INDEX, SEED_ROLE_KEYS, SEED_ROLES, Role, Scope

ROLE_KEY_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class AccessError(ValueError):
    """A role definition is malformed."""


@dataclass(frozen=True)
class Assignment:
    role: str  # role key
    tenant: str | None = None  # ten_… for a tenant role; None for a platform-wide one


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    via: str | None = None  # the role key that granted it

    def __bool__(self) -> bool:
        return self.allowed


def validate_role(role: Role) -> Role:
    """Raise AccessError unless `role` is well-formed; return it otherwise."""
    if not ROLE_KEY_RE.match(role.key or ""):
        raise AccessError(
            f"role key must be lowercase words joined by '-', got {role.key!r}"
        )
    if not role.name.strip():
        raise AccessError("role needs a name")
    unknown = sorted(set(role.permissions) - set(PERMISSION_INDEX))
    if unknown:
        raise AccessError(f"unknown permissions: {', '.join(unknown)}")
    if role.scope is Scope.TENANT:
        platform_only = sorted(
            k for k in role.permissions if PERMISSION_INDEX[k].scope is Scope.PLATFORM
        )
        if platform_only:
            raise AccessError(
                f"a tenant role cannot hold platform permissions: {', '.join(platform_only)}"
            )
    elif role.assignable_by_tenant_admin:
        raise AccessError("only tenant roles can be assignable by tenant admins")
    return role


class AccessPolicy:
    def __init__(self, roles: Iterable[Role] = SEED_ROLES) -> None:
        self._roles: dict[str, Role] = {}
        for role in roles:
            validate_role(role)
            if role.key in self._roles:
                raise AccessError(f"duplicate role key {role.key!r}")
            self._roles[role.key] = role

    def role(self, key: str) -> Role | None:
        return self._roles.get(key)

    def with_role(self, role: Role) -> AccessPolicy:
        """A policy that also knows `role` (after check_definition allowed it)."""
        return AccessPolicy([*self._roles.values(), role])

    # ------------------------------------------------------------------------------ granting
    def _effective(
        self, assignments: Iterable[Assignment]
    ) -> list[tuple[Role, str | None]]:
        out = []
        for a in assignments:
            role = self._roles.get(a.role)
            if role is None:
                continue
            if role.scope is Scope.PLATFORM and a.tenant is None:
                out.append((role, None))
            elif role.scope is Scope.TENANT and is_id(a.tenant, Cat.TENANT):
                out.append((role, a.tenant))
        return out

    def can(
        self,
        assignments: Iterable[Assignment],
        permission: str,
        tenant: str | None = None,
    ) -> Decision:
        perm = PERMISSION_INDEX.get(permission)
        if perm is None:
            return Decision(False, f"unknown permission {permission!r}")
        effective = self._effective(assignments)
        if perm.scope is Scope.PLATFORM:
            for role, scope_tenant in effective:
                if scope_tenant is None and permission in role.permissions:
                    return Decision(True, "platform role", role.key)
            return Decision(False, "no platform role grants it")
        if not is_id(tenant, Cat.TENANT):
            return Decision(False, "a tenant permission needs a tenant")
        for role, scope_tenant in effective:
            if permission in role.permissions and scope_tenant in (None, tenant):
                kind = "platform role" if scope_tenant is None else "tenant role"
                return Decision(True, kind, role.key)
        return Decision(False, "no role grants it in this tenant")

    def permissions_in(
        self, assignments: Iterable[Assignment], tenant: str
    ) -> frozenset[str]:
        """Every tenant permission the assignments grant in `tenant`."""
        granted: set[str] = set()
        for role, scope_tenant in self._effective(assignments):
            if scope_tenant in (None, tenant):
                granted |= {
                    k
                    for k in role.permissions
                    if PERMISSION_INDEX[k].scope is Scope.TENANT
                }
        return frozenset(granted)

    # ---------------------------------------------------------------------------- delegating
    def can_assign(
        self, actor: Iterable[Assignment], role_key: str, tenant: str | None
    ) -> Decision:
        actor = list(actor)
        role = self._roles.get(role_key)
        if role is None:
            return Decision(False, f"unknown role {role_key!r}")
        if role.scope is Scope.PLATFORM and tenant is not None:
            return Decision(
                False, "a platform role is assigned platform-wide, not in a tenant"
            )
        if role.scope is Scope.TENANT and not is_id(tenant, Cat.TENANT):
            return Decision(False, "a tenant role needs a tenant")
        if self.can(actor, "platform.roles.assign"):
            return Decision(True, "platform role assignment", role.key)
        if role.scope is Scope.PLATFORM:
            return Decision(
                False, "only platform role assigners can assign platform roles"
            )
        if not self.can(actor, "roles.assign", tenant):
            return Decision(False, "no roles.assign in this tenant")
        if not role.assignable_by_tenant_admin:
            return Decision(False, "this role is not assignable by tenant admins")
        missing = role.permissions - self.permissions_in(actor, tenant)  # type: ignore[arg-type]
        if missing:
            return Decision(
                False, f"would grant permissions you lack: {', '.join(sorted(missing))}"
            )
        return Decision(True, "tenant delegation", role.key)

    def check_definition(self, actor: Iterable[Assignment], role: Role) -> Decision:
        """May `actor` define `role`? Malformed roles are refused with the reason."""
        if not self.can(actor, "platform.roles.define"):
            return Decision(False, "no platform.roles.define")
        if role.seed or role.key in SEED_ROLE_KEYS:
            return Decision(False, "seed roles cannot be redefined")
        if role.key in self._roles:
            return Decision(False, f"role {role.key!r} already exists")
        try:
            validate_role(role)
        except AccessError as e:
            return Decision(False, str(e))
        return Decision(True, "role definition", role.key)
