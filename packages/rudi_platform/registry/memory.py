"""
MEET_RUDI platform — InMemoryRegistryStore.

The registry in a few dictionaries, for tests and local runs. It is not a mock: it enforces the
same rules as db/migrations/0001 (row-level security as contexts, unique constraints, CHECKs and
triggers), so code written against it behaves the same on PlanetScale. Where the two could
differ, the contract tests in tests/test_registry.py decide.
"""

from __future__ import annotations

import copy
import datetime
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from ..access.catalogue import SEED_ROLES, Role, Scope
from ..access.policy import AccessError, validate_role
from ..ids import Cat, is_id, new_id
from ..naming import CELL_RE
from ..persona.ocean import PersonaError, validate_ocean
from ..skills.manifest import CHANNELS, SkillManifest
from .models import (
    Account,
    AuditEvent,
    Cell,
    Clone,
    CloneVersion,
    Context,
    Endpoint,
    Persona,
    PlatformContext,
    RoleAssignmentRecord,
    RouteTarget,
    SkillVersionRecord,
    Tenant,
    TenantContext,
)
from .store import Conflict, Forbidden, InvalidState, InvalidValue, NotFound

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
TENANT_STATUSES = ("active", "suspended", "offboarding")


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _slug(value: str, what: str) -> str:
    if not isinstance(value, str) or not SLUG_RE.match(value):
        raise InvalidValue(
            f"{what} must be lowercase words joined by '-', got {value!r}"
        )
    return value


class InMemoryRegistryStore:
    def __init__(self, clock: Callable[[], datetime.datetime] = _utcnow) -> None:
        self._clock = clock
        self._cells: dict[str, Cell] = {}
        self._tenants: dict[str, Tenant] = {}
        self._clones: dict[str, Clone] = {}
        self._versions: dict[str, CloneVersion] = {}
        self._personas: dict[str, Persona] = {}
        self._endpoints: dict[str, Endpoint] = {}
        self._skills: dict[str, tuple[str, str]] = {}  # slug -> (skl_ id, kind)
        self._skill_versions: dict[str, SkillVersionRecord] = {}
        self._accounts: dict[str, Account] = {}
        self._roles: dict[str, Role] = {r.key: r for r in SEED_ROLES}
        self._assignments: dict[str, RoleAssignmentRecord] = {}
        self._audit: list[AuditEvent] = []

    # ------------------------------------------------------------------------------ contexts
    @staticmethod
    def _platform(ctx: Context) -> None:
        if not isinstance(ctx, PlatformContext):
            raise Forbidden("a platform operation")

    @staticmethod
    def _sees(ctx: Context, tenant_id: str | None) -> bool:
        if isinstance(ctx, PlatformContext):
            return True
        if not is_id(ctx.tenant_id, Cat.TENANT):
            raise InvalidValue("TenantContext needs a ten_ id")
        return tenant_id == ctx.tenant_id

    def _may_write(self, ctx: Context, tenant_id: str) -> None:
        if not self._sees(ctx, tenant_id):
            raise Forbidden("another tenant's data")

    def _get(
        self, table: Mapping[str, Any], ctx: Context, row_id: str, what: str
    ) -> Any:
        row = table.get(row_id)
        if row is None or not self._sees(ctx, getattr(row, "tenant_id", None)):
            raise NotFound(f"{what} {row_id}")
        return row

    # ------------------------------------------------------------------------ cells, tenants
    def create_cell(self, ctx: Context, slug: str, kind: str) -> Cell:
        self._platform(ctx)
        if not CELL_RE.match(slug or ""):
            raise InvalidValue(f"bad cell slug {slug!r}")
        if kind not in ("pooled", "dedicated") or (slug == "pool") != (
            kind == "pooled"
        ):
            raise InvalidValue("the pool cell is pooled; d- cells are dedicated")
        if any(c.slug == slug for c in self._cells.values()):
            raise Conflict(f"cell {slug} exists")
        cell = Cell(new_id(Cat.CELL), slug, kind)
        self._cells[cell.id] = cell
        return cell

    def create_tenant(self, ctx: Context, slug: str, name: str, cell_id: str) -> Tenant:
        self._platform(ctx)
        _slug(slug, "tenant slug")
        if not name.strip():
            raise InvalidValue("tenant needs a name")
        if cell_id not in self._cells:
            raise NotFound(f"cell {cell_id}")
        if any(t.slug == slug for t in self._tenants.values()):
            raise Conflict(f"tenant {slug} exists")
        tenant = Tenant(new_id(Cat.TENANT), slug, name.strip(), cell_id)
        self._tenants[tenant.id] = tenant
        return copy.deepcopy(tenant)

    def get_tenant(self, ctx: Context, tenant_id: str) -> Tenant:
        tenant = self._tenants.get(tenant_id)
        if tenant is None or not self._sees(ctx, tenant.id):
            raise NotFound(f"tenant {tenant_id}")
        return copy.deepcopy(tenant)

    def set_tenant_status(self, ctx: Context, tenant_id: str, status: str) -> Tenant:
        self._platform(ctx)
        if status not in TENANT_STATUSES:
            raise InvalidValue(f"status must be one of {TENANT_STATUSES}")
        tenant = self.get_tenant(ctx, tenant_id)
        self._tenants[tenant_id] = replace(tenant, status=status)
        return copy.deepcopy(self._tenants[tenant_id])

    # ---------------------------------------------------------------------- clones, versions
    def create_clone(
        self, ctx: Context, tenant_id: str, slug: str, name: str, purpose: str
    ) -> Clone:
        self._may_write(ctx, tenant_id)
        if tenant_id not in self._tenants:
            raise NotFound(f"tenant {tenant_id}")
        _slug(slug, "clone slug")
        purpose = (purpose or "").strip()
        if not 10 <= len(purpose) <= 500:
            raise InvalidValue(
                "a clone declares its business purpose in 10–500 characters"
            )
        if not name.strip():
            raise InvalidValue("clone needs a name")
        if any(
            c.tenant_id == tenant_id and c.slug == slug for c in self._clones.values()
        ):
            raise Conflict(f"clone {slug} exists in this tenant")
        clone = Clone(new_id(Cat.CLONE), tenant_id, slug, name.strip(), purpose)
        self._clones[clone.id] = clone
        return clone

    def get_clone(self, ctx: Context, clone_id: str) -> Clone:
        return self._get(self._clones, ctx, clone_id, "clone")

    def list_clones(self, ctx: Context, tenant_id: str) -> list[Clone]:
        if not self._sees(ctx, tenant_id):
            return []
        return sorted(
            (c for c in self._clones.values() if c.tenant_id == tenant_id),
            key=lambda c: c.slug,
        )

    def create_draft(
        self,
        ctx: Context,
        clone_id: str,
        from_version_id: str | None = None,
        created_by: str | None = None,
    ) -> CloneVersion:
        clone = self.get_clone(ctx, clone_id)
        source = self.get_version(ctx, from_version_id) if from_version_id else None
        if source is not None and source.clone_id != clone_id:
            raise InvalidValue("a draft copies a version of the same clone")
        number = 1 + max(
            (v.number for v in self._versions.values() if v.clone_id == clone_id),
            default=0,
        )
        version = CloneVersion(
            id=new_id(Cat.CLONE_VERSION),
            tenant_id=clone.tenant_id,
            clone_id=clone_id,
            number=number,
            composition=copy.deepcopy(source.composition) if source else {},
            journey=copy.deepcopy(source.journey) if source else {},
            settings=copy.deepcopy(source.settings) if source else {},
            created_by=created_by,
        )
        self._versions[version.id] = version
        if source is not None:
            for p in [
                p for p in self._personas.values() if p.clone_version_id == source.id
            ]:
                copied = replace(p, id=new_id(Cat.PERSONA), clone_version_id=version.id)
                self._personas[copied.id] = copy.deepcopy(copied)
        return copy.deepcopy(version)

    def get_version(self, ctx: Context, version_id: str) -> CloneVersion:
        return copy.deepcopy(
            self._get(self._versions, ctx, version_id, "clone version")
        )

    def list_versions(self, ctx: Context, clone_id: str) -> list[CloneVersion]:
        self.get_clone(ctx, clone_id)
        rows = [v for v in self._versions.values() if v.clone_id == clone_id]
        return [copy.deepcopy(v) for v in sorted(rows, key=lambda v: v.number)]

    def _draft(self, ctx: Context, version_id: str) -> CloneVersion:
        version = self._get(self._versions, ctx, version_id, "clone version")
        if version.stage != "draft":
            raise InvalidState(f"clone version {version_id} is published and immutable")
        return version

    def update_draft(
        self,
        ctx: Context,
        version_id: str,
        *,
        composition: Mapping[str, Any] | None = None,
        journey: Mapping[str, Any] | None = None,
        settings: Mapping[str, Any] | None = None,
    ) -> CloneVersion:
        version = self._draft(ctx, version_id)
        changes: dict[str, Any] = {}
        for name, value in (
            ("composition", composition),
            ("journey", journey),
            ("settings", settings),
        ):
            if value is not None:
                if not isinstance(value, Mapping):
                    raise InvalidValue(f"{name} must be an object")
                changes[name] = copy.deepcopy(dict(value))
        self._versions[version_id] = replace(version, **changes)
        return copy.deepcopy(self._versions[version_id])

    def promote(self, ctx: Context, version_id: str, stage: str) -> CloneVersion:
        version = self._get(self._versions, ctx, version_id, "clone version")
        now = self._clock()
        if (version.stage, stage) == ("draft", "staging"):
            self._versions[version_id] = replace(
                version, stage="staging", published_at=now
            )
        elif (version.stage, stage) == ("staging", "live"):
            self._platform(ctx)  # going live is our review (A7)
            clone = self._clones[version.clone_id]
            if clone.live_version_id:
                old = self._versions[clone.live_version_id]
                self._versions[old.id] = replace(old, stage="retired")
            self._versions[version_id] = replace(version, stage="live")
            self._clones[clone.id] = replace(
                clone, live_version_id=version_id, status="live"
            )
        else:
            raise InvalidState(f"cannot move a {version.stage} version to {stage}")
        return copy.deepcopy(self._versions[version_id])

    # ----------------------------------------------------------------------------- personas
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
    ) -> Persona:
        version = self._draft(ctx, version_id)
        _slug(key, "persona key")
        if not name.strip():
            raise InvalidValue("persona needs a name")
        try:
            scores = validate_ocean(ocean)
        except PersonaError as e:
            raise InvalidValue(str(e)) from None
        existing = next(
            (
                p
                for p in self._personas.values()
                if p.clone_version_id == version_id and p.key == key
            ),
            None,
        )
        persona = Persona(
            id=existing.id if existing else new_id(Cat.PERSONA),
            tenant_id=version.tenant_id,
            clone_version_id=version_id,
            key=key,
            name=name.strip(),
            ocean=scores,
            voice=copy.deepcopy(dict(voice or {})),
            languages=tuple(languages) or ("en",),
            briefing=briefing,
        )
        self._personas[persona.id] = persona
        return copy.deepcopy(persona)

    def remove_persona(self, ctx: Context, version_id: str, key: str) -> None:
        self._draft(ctx, version_id)
        for pid, p in list(self._personas.items()):
            if p.clone_version_id == version_id and p.key == key:
                del self._personas[pid]
                return
        raise NotFound(f"persona {key}")

    def personas(self, ctx: Context, version_id: str) -> list[Persona]:
        self.get_version(ctx, version_id)
        rows = [p for p in self._personas.values() if p.clone_version_id == version_id]
        return [copy.deepcopy(p) for p in sorted(rows, key=lambda p: p.key)]

    # ------------------------------------------------------------------- endpoints, routing
    def add_endpoint(
        self, ctx: Context, clone_id: str, channel: str, address: str
    ) -> Endpoint:
        clone = self.get_clone(ctx, clone_id)
        if channel not in CHANNELS:
            raise InvalidValue(f"channel must be one of {CHANNELS}")
        address = (address or "").strip()
        if not address:
            raise InvalidValue("an endpoint needs an address")
        # Unique across all tenants, but the message must not reveal whose it is.
        if any(
            e.channel == channel and e.address == address
            for e in self._endpoints.values()
        ):
            raise Conflict(f"this {channel} address is already in use")
        endpoint = Endpoint(
            new_id(Cat.ENDPOINT), clone.tenant_id, clone_id, channel, address
        )
        self._endpoints[endpoint.id] = endpoint
        return endpoint

    def list_endpoints(self, ctx: Context, clone_id: str) -> list[Endpoint]:
        self.get_clone(ctx, clone_id)
        return [e for e in self._endpoints.values() if e.clone_id == clone_id]

    def route(self, ctx: Context, channel: str, address: str) -> RouteTarget | None:
        """Where traffic for (channel, address) goes, or None if it has nowhere live to go."""
        self._platform(ctx)
        endpoint = next(
            (
                e
                for e in self._endpoints.values()
                if e.channel == channel
                and e.address == address
                and e.status == "active"
            ),
            None,
        )
        if endpoint is None:
            return None
        clone = self._clones[endpoint.clone_id]
        tenant = self._tenants[clone.tenant_id]
        if (
            tenant.status != "active"
            or clone.status != "live"
            or not clone.live_version_id
        ):
            return None
        cell = self._cells[tenant.cell_id]
        return RouteTarget(
            endpoint.id, tenant.id, clone.id, clone.live_version_id, cell.slug
        )

    # ------------------------------------------------------------------------------- skills
    def register_skill_version(
        self, ctx: Context, manifest: SkillManifest, brief_path: str | None = None
    ) -> SkillVersionRecord:
        self._platform(ctx)
        skill_id, kind = self._skills.get(manifest.slug, (None, None))
        if skill_id is None:
            skill_id, kind = new_id(Cat.SKILL), manifest.kind.value
            self._skills[manifest.slug] = (skill_id, kind)
        elif kind != manifest.kind.value:
            raise Conflict(
                f"{manifest.slug} is a {kind} skill; a version cannot change its kind"
            )
        if any(
            r.manifest.slug == manifest.slug and r.manifest.version == manifest.version
            for r in self._skill_versions.values()
        ):
            raise Conflict(f"{manifest.ref} is already registered")
        record = SkillVersionRecord(
            new_id(Cat.SKILL_VERSION), skill_id, manifest, brief_path=brief_path
        )
        self._skill_versions[record.id] = record
        return record

    def release_skill_version(
        self, ctx: Context, skill_version_id: str, gate_run_id: str
    ) -> SkillVersionRecord:
        self._platform(ctx)
        record = self._skill_versions.get(skill_version_id)
        if record is None:
            raise NotFound(f"skill version {skill_version_id}")
        if not is_id(gate_run_id, Cat.GATE_RUN):
            raise InvalidValue("a release needs the gat_ id of a passing arena gate")
        if record.status != "candidate":
            raise InvalidState(f"{record.ref} is {record.status}")
        released = replace(
            record,
            status="released",
            gate_run_id=gate_run_id,
            released_at=self._clock(),
        )
        self._skill_versions[record.id] = released
        return released

    def skill_versions(
        self, ctx: Context, slug: str | None = None
    ) -> list[SkillVersionRecord]:
        rows = [
            r
            for r in self._skill_versions.values()
            if slug is None or r.manifest.slug == slug
        ]
        return sorted(rows, key=lambda r: (r.manifest.slug, r.manifest.version))

    def released_manifests(self, ctx: Context) -> dict[str, list[SkillManifest]]:
        out: dict[str, list[SkillManifest]] = {}
        for r in self.skill_versions(ctx):
            if r.status == "released":
                out.setdefault(r.manifest.slug, []).append(r.manifest)
        return out

    # --------------------------------------------------------------- accounts, roles, grants
    def create_account(
        self, ctx: Context, email: str, display_name: str = ""
    ) -> Account:
        self._platform(ctx)
        email = (email or "").strip().lower()
        if email.find("@") < 1:
            raise InvalidValue("not an e-mail address")
        if any(a.email == email for a in self._accounts.values()):
            raise Conflict("an account with this e-mail exists")
        account = Account(new_id(Cat.ACCOUNT), email, display_name.strip())
        self._accounts[account.id] = account
        return account

    def account_by_email(self, ctx: Context, email: str) -> Account:
        self._platform(ctx)
        email = (email or "").strip().lower()
        for a in self._accounts.values():
            if a.email == email:
                return a
        raise NotFound("account")

    def roles(self, ctx: Context) -> list[Role]:
        return sorted(self._roles.values(), key=lambda r: (r.scope.value, r.key))

    def define_role(self, ctx: Context, role: Role) -> Role:
        self._platform(ctx)
        try:
            validate_role(role)
        except AccessError as e:
            raise InvalidValue(str(e)) from None
        if role.seed or role.key in self._roles:
            raise Conflict(f"role {role.key} exists")
        self._roles[role.key] = role
        return role

    def assign_role(
        self,
        ctx: Context,
        account_id: str,
        role_key: str,
        tenant_id: str | None = None,
        granted_by: str | None = None,
    ) -> RoleAssignmentRecord:
        role = self._roles.get(role_key)
        if role is None:
            raise NotFound(f"role {role_key}")
        if account_id not in self._accounts:
            raise NotFound(f"account {account_id}")
        if role.scope is Scope.PLATFORM:
            self._platform(ctx)
            if tenant_id is not None:
                raise InvalidValue(
                    f"platform role {role_key} cannot be assigned in a tenant"
                )
        else:
            if tenant_id is None:
                raise InvalidValue(f"tenant role {role_key} needs a tenant")
            self._may_write(ctx, tenant_id)
            if tenant_id not in self._tenants:
                raise NotFound(f"tenant {tenant_id}")
        if any(
            a.account_id == account_id
            and a.role_key == role_key
            and a.tenant_id == tenant_id
            for a in self._assignments.values()
        ):
            raise Conflict("this role is already assigned")
        record = RoleAssignmentRecord(
            new_id(Cat.ROLE_ASSIGNMENT), account_id, role_key, tenant_id, granted_by
        )
        self._assignments[record.id] = record
        return record

    def revoke_assignment(self, ctx: Context, assignment_id: str) -> None:
        record = self._assignments.get(assignment_id)
        if record is None or not self._sees(ctx, record.tenant_id):
            raise NotFound(f"assignment {assignment_id}")
        del self._assignments[assignment_id]

    def assignments_for(
        self, ctx: Context, account_id: str
    ) -> list[RoleAssignmentRecord]:
        return [
            a
            for a in self._assignments.values()
            if a.account_id == account_id and self._sees(ctx, a.tenant_id)
        ]

    # -------------------------------------------------------------------------------- audit
    def append_audit(
        self,
        ctx: Context,
        action: str,
        *,
        actor_account_id: str | None = None,
        tenant_id: str | None = None,
        target_id: str | None = None,
        decision: Mapping[str, Any] | None = None,
    ) -> AuditEvent:
        if isinstance(ctx, TenantContext):
            tenant_id = tenant_id or ctx.tenant_id
            self._may_write(ctx, tenant_id)
        if not action:
            raise InvalidValue("an audit event needs an action")
        event = AuditEvent(
            new_id(Cat.AUDIT),
            self._clock(),
            action,
            actor_account_id,
            tenant_id,
            target_id,
            copy.deepcopy(dict(decision or {})),
        )
        self._audit.append(event)
        return copy.deepcopy(event)

    def audit_events(
        self, ctx: Context, tenant_id: str | None = None
    ) -> list[AuditEvent]:
        return [
            copy.deepcopy(e)
            for e in self._audit
            if self._sees(ctx, e.tenant_id)
            and (tenant_id is None or e.tenant_id == tenant_id)
        ]
