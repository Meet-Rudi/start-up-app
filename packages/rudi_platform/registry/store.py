"""
MEET_RUDI platform — the RegistryStore seam (CLAUDE.md §2).

Everything the console, the router and the publishing flow need from the registry, behind one
interface. Every call takes a context first; on PlanetScale it becomes the tenant the store's
connection is bound to (rudi_platform.db.TenantBound, decision #47):

  - TenantContext(ten_…) sees and writes that tenant's rows only. Another tenant's row behaves
    as if it did not exist (NotFound), and writing a row for another tenant is Forbidden.
  - PlatformContext sees everything and is the only context for platform operations: tenants,
    cells, skill releases, accounts, roles, routing, and approving a clone version for live. On
    PlanetScale it still reads a tenant's rows tenant by tenant, with a guard bound to that
    tenant; routing uses the guard's resolvers.

Two implementations are planned: InMemoryRegistryStore (memory.py, for tests and local runs) and
a PlanetScale (Vitess) store once the database exists (USER-TODO #3). tests/test_registry.py is
written as a contract both must pass.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from ..access.catalogue import Role
from ..skills.manifest import SkillManifest
from .models import (
    Account,
    AuditEvent,
    Cell,
    Clone,
    CloneVersion,
    Context,
    Endpoint,
    Persona,
    RoleAssignmentRecord,
    RouteTarget,
    SkillVersionRecord,
    Tenant,
)


class RegistryError(Exception):
    """Base of every registry failure."""


class NotFound(RegistryError):
    """No such row, or not visible in this context (indistinguishable on purpose)."""


class Forbidden(RegistryError):
    """This context may not do that: a platform operation, or another tenant's data."""


class Conflict(RegistryError):
    """A uniqueness rule would be broken."""


class InvalidState(RegistryError):
    """The row's lifecycle does not allow it (e.g. editing a published clone version)."""


class InvalidValue(RegistryError, ValueError):
    """A value breaks a schema rule (slug shape, purpose length, OCEAN range…)."""


class RegistryStore(Protocol):
    # --- platform: cells, tenants -----------------------------------------------------------
    def create_cell(self, ctx: Context, slug: str, kind: str) -> Cell: ...
    def create_tenant(
        self, ctx: Context, slug: str, name: str, cell_id: str
    ) -> Tenant: ...
    def get_tenant(self, ctx: Context, tenant_id: str) -> Tenant: ...
    def set_tenant_status(
        self, ctx: Context, tenant_id: str, status: str
    ) -> Tenant: ...

    # --- clones and versions ----------------------------------------------------------------
    def create_clone(
        self, ctx: Context, tenant_id: str, slug: str, name: str, purpose: str
    ) -> Clone: ...
    def get_clone(self, ctx: Context, clone_id: str) -> Clone: ...
    def list_clones(self, ctx: Context, tenant_id: str) -> list[Clone]: ...
    def create_draft(
        self,
        ctx: Context,
        clone_id: str,
        from_version_id: str | None = None,
        created_by: str | None = None,
    ) -> CloneVersion: ...
    def get_version(self, ctx: Context, version_id: str) -> CloneVersion: ...
    def list_versions(self, ctx: Context, clone_id: str) -> list[CloneVersion]: ...
    def update_draft(
        self,
        ctx: Context,
        version_id: str,
        *,
        composition: Mapping[str, Any] | None = None,
        journey: Mapping[str, Any] | None = None,
        settings: Mapping[str, Any] | None = None,
    ) -> CloneVersion: ...
    def promote(self, ctx: Context, version_id: str, stage: str) -> CloneVersion: ...

    # --- personas -----------------------------------------------------------------------------
    def put_persona(
        self,
        ctx: Context,
        version_id: str,
        key: str,
        name: str,
        ocean: Mapping[str, Any],
        *,
        voice: Mapping[str, Any] | None = None,
        languages: Sequence[str] = ("en",),
        briefing: str = "",
    ) -> Persona: ...
    def remove_persona(self, ctx: Context, version_id: str, key: str) -> None: ...
    def personas(self, ctx: Context, version_id: str) -> list[Persona]: ...

    # --- endpoints and routing --------------------------------------------------------------
    def add_endpoint(
        self, ctx: Context, clone_id: str, channel: str, address: str
    ) -> Endpoint: ...
    def list_endpoints(self, ctx: Context, clone_id: str) -> list[Endpoint]: ...
    def route(self, ctx: Context, channel: str, address: str) -> RouteTarget | None: ...

    # --- skills (shared inventory) ----------------------------------------------------------
    def register_skill_version(
        self, ctx: Context, manifest: SkillManifest, brief_path: str | None = None
    ) -> SkillVersionRecord: ...
    def release_skill_version(
        self, ctx: Context, skill_version_id: str, gate_run_id: str
    ) -> SkillVersionRecord: ...
    def skill_versions(
        self, ctx: Context, slug: str | None = None
    ) -> list[SkillVersionRecord]: ...
    def released_manifests(self, ctx: Context) -> dict[str, list[SkillManifest]]: ...

    # --- accounts, roles, assignments -------------------------------------------------------
    def create_account(
        self, ctx: Context, email: str, display_name: str = ""
    ) -> Account: ...
    def account_by_email(self, ctx: Context, email: str) -> Account: ...
    def roles(self, ctx: Context) -> list[Role]: ...
    def define_role(self, ctx: Context, role: Role) -> Role: ...
    def assign_role(
        self,
        ctx: Context,
        account_id: str,
        role_key: str,
        tenant_id: str | None = None,
        granted_by: str | None = None,
    ) -> RoleAssignmentRecord: ...
    def revoke_assignment(self, ctx: Context, assignment_id: str) -> None: ...
    def assignments_for(
        self, ctx: Context, account_id: str
    ) -> list[RoleAssignmentRecord]: ...

    # --- audit ------------------------------------------------------------------------------
    def append_audit(
        self,
        ctx: Context,
        action: str,
        *,
        actor_account_id: str | None = None,
        tenant_id: str | None = None,
        target_id: str | None = None,
        decision: Mapping[str, Any] | None = None,
    ) -> AuditEvent: ...
    def audit_events(
        self, ctx: Context, tenant_id: str | None = None
    ) -> list[AuditEvent]: ...
