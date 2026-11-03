"""
MEET_RUDI platform — what the registry stores, as the application sees it.

Mirrors db/migrations/0001 (registry, access, audit schemas). Records are frozen; JSON-like
payloads (composition, journey, settings, ocean, decision) are deep-copied in and out of a store,
so changing a returned record never changes stored data.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any

from ..skills.manifest import SkillManifest

CLONE_STAGES = ("draft", "staging", "live", "retired")


@dataclass(frozen=True)
class PlatformContext:
    """Platform work: console administration, the router, jobs. Sees every tenant.
    In Postgres: SET LOCAL rudi.platform = 'on'."""


@dataclass(frozen=True)
class TenantContext:
    """Work on behalf of one tenant. Sees that tenant only.
    In Postgres: SET LOCAL rudi.tenant_id = '<ten_…>'."""

    tenant_id: str


Context = PlatformContext | TenantContext
PLATFORM = PlatformContext()


@dataclass(frozen=True)
class Cell:
    id: str
    slug: str
    kind: str  # pooled | dedicated
    region: str = "eu-central-1"


@dataclass(frozen=True)
class Tenant:
    id: str
    slug: str
    name: str
    cell_id: str
    status: str = "active"  # active | suspended | offboarding
    acceptable_use: dict[str, Any] = field(default_factory=dict)
    model_policy: dict[str, Any] = field(default_factory=dict)
    limits: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Clone:
    id: str
    tenant_id: str
    slug: str
    name: str
    purpose: str
    status: str = "draft"  # draft | live | paused | retired
    live_version_id: str | None = None


@dataclass(frozen=True)
class CloneVersion:
    id: str
    tenant_id: str
    clone_id: str
    number: int
    stage: str = "draft"
    composition: dict[str, Any] = field(default_factory=dict)
    journey: dict[str, Any] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)
    created_by: str | None = None
    published_at: datetime.datetime | None = None


@dataclass(frozen=True)
class Persona:
    id: str
    tenant_id: str
    clone_version_id: str
    key: str  # stable across versions; end-users are pinned to it
    name: str
    ocean: dict[str, int]
    voice: dict[str, Any] = field(default_factory=dict)
    languages: tuple[str, ...] = ("en",)
    briefing: str = ""


@dataclass(frozen=True)
class SkillVersionRecord:
    id: str
    skill_id: str
    manifest: SkillManifest
    status: str = "candidate"  # candidate | released | retired
    gate_run_id: str | None = None
    brief_path: str | None = None
    released_at: datetime.datetime | None = None

    @property
    def ref(self) -> str:
        return str(self.manifest.ref)


@dataclass(frozen=True)
class Endpoint:
    id: str
    tenant_id: str
    clone_id: str
    channel: str
    address: str
    status: str = "active"


@dataclass(frozen=True)
class RouteTarget:
    """Where the router sends traffic arriving at an endpoint."""

    endpoint_id: str
    tenant_id: str
    clone_id: str
    clone_version_id: str
    cell_slug: str


@dataclass(frozen=True)
class Account:
    id: str
    email: str
    display_name: str = ""
    status: str = "active"


@dataclass(frozen=True)
class RoleAssignmentRecord:
    id: str
    account_id: str
    role_key: str
    tenant_id: str | None
    granted_by: str | None = None


@dataclass(frozen=True)
class AuditEvent:
    id: str
    at: datetime.datetime
    action: str
    actor_account_id: str | None = None
    tenant_id: str | None = None
    target_id: str | None = None
    decision: dict[str, Any] = field(default_factory=dict)
