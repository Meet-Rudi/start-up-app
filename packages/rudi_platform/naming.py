"""
MEET_RUDI platform — AWS resource names.

Two planes, so IAM policies can grant by prefix (decision #28):

    meetrudi-internal-{stage}-{component}          platform-managing: console API, registry,
                                                   arena, skill factory, migrations
    meetrudi-tenant-{stage}-{cell}-{component}     serves tenant traffic: adapters, router,
                                                   engine, skills, scheduler

`stage` is dev or prod. `cell` is `pool` for the shared cell or `d-<slug>` for a dedicated one.
A skill Lambda is the tenant-plane component `skill-<skill-slug>`.

Resources created before the platform (meetrudi-ai-data-…, meetrudi-wa-…, meetrudi-call-…) are
the legacy stage and keep their names until their service is cut over. Nothing new may be
created without a plane.

Builders check AWS name limits up front, so a long cell slug fails here rather than halfway
through a CloudFormation deploy.
"""

from __future__ import annotations

import re
from enum import Enum

from .ids import Cat, require

PROJECT = "meetrudi"
STAGES = ("dev", "prod")
POOL = "pool"


class Plane(str, Enum):
    INTERNAL = "internal"
    TENANT = "tenant"


CELL_RE = re.compile(r"^(?:pool|d-[a-z0-9]{1,10})$")
COMPONENT_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ACCOUNT_RE = re.compile(r"^\d{12}$")

# AWS name limits, by resource kind.
LIMITS = {
    "lambda": 64,
    "role": 64,
    "policy": 128,
    "queue": 80,  # includes the .fifo suffix
    "stack": 128,
    "rule": 64,
    "bus": 256,
    "table": 255,
    "bucket": 63,
}


def _check_stage(stage: str) -> None:
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {', '.join(STAGES)}, got {stage!r}")


def _check_cell(cell: str) -> None:
    if not CELL_RE.match(cell or ""):
        raise ValueError(
            f"cell must be 'pool' or 'd-' plus 1–10 of [a-z0-9], got {cell!r}"
        )


def _check_component(component: str) -> None:
    if not COMPONENT_RE.match(component or ""):
        raise ValueError(
            f"component must be lowercase words joined by '-', got {component!r}"
        )


def _fit(name: str, kind: str) -> str:
    limit = LIMITS[kind]
    if len(name) > limit:
        raise ValueError(
            f"{kind} name {name!r} is {len(name)} characters; AWS allows {limit}"
        )
    return name


def internal_name(stage: str, component: str, kind: str = "lambda") -> str:
    """Name of a platform-managing resource, e.g. meetrudi-internal-prod-console-api."""
    _check_stage(stage)
    _check_component(component)
    return _fit(f"{PROJECT}-{Plane.INTERNAL.value}-{stage}-{component}", kind)


def tenant_name(stage: str, cell: str, component: str, kind: str = "lambda") -> str:
    """Name of a resource serving tenant traffic, e.g. meetrudi-tenant-prod-pool-engine."""
    _check_stage(stage)
    _check_cell(cell)
    _check_component(component)
    return _fit(f"{PROJECT}-{Plane.TENANT.value}-{stage}-{cell}-{component}", kind)


def skill_function(stage: str, cell: str, skill_slug: str) -> str:
    """Lambda name of a turn or background skill in one cell."""
    return tenant_name(stage, cell, "skill-" + skill_slug)


def queue_name(base: str, fifo: bool = False) -> str:
    """Queue name from a plane-qualified base name; FIFO queues need the .fifo suffix."""
    return _fit(base + (".fifo" if fifo else ""), "queue")


def bucket_name(
    plane: Plane, stage: str, account_id: str, cell: str | None = None
) -> str:
    """Data bucket: meetrudi-{plane}-{stage}[-{cell}]-data-{account}."""
    _check_stage(stage)
    if not ACCOUNT_RE.match(account_id or ""):
        raise ValueError("account id must be 12 digits")
    if plane is Plane.TENANT:
        _check_cell(cell or "")
        parts = [PROJECT, plane.value, stage, cell, "data", account_id]
    else:
        if cell is not None:
            raise ValueError("internal buckets belong to no cell")
        parts = [PROJECT, plane.value, stage, "data", account_id]
    return _fit("-".join(parts), "bucket")  # type: ignore[arg-type]


def iam_pattern(plane: Plane, stage: str = "*") -> str:
    """Name pattern for IAM resource ARNs, e.g. meetrudi-tenant-prod-*."""
    if stage != "*":
        _check_stage(stage)
    return f"{PROJECT}-{plane.value}-{stage}-*"


def secret_path(plane: Plane, stage: str, *parts: str) -> str:
    """Secrets Manager / SSM name: meetrudi/{plane}/{stage}/part/part."""
    _check_stage(stage)
    for p in parts:
        if not p or "/" in p:
            raise ValueError(
                f"secret path parts must be non-empty and contain no '/', got {p!r}"
            )
    return "/".join([PROJECT, plane.value, stage, *parts])


def tenant_secret_path(stage: str, tenant_id: str, *parts: str) -> str:
    """A secret that belongs to one tenant: meetrudi/tenant/{stage}/{ten_…}/…."""
    require(tenant_id, Cat.TENANT)
    return secret_path(Plane.TENANT, stage, tenant_id, *parts)


def kms_alias(stage: str, tenant_id: str) -> str:
    """The tenant's own KMS key alias: alias/meetrudi-tenant-{stage}-{ten_…}."""
    _check_stage(stage)
    require(tenant_id, Cat.TENANT)
    return f"alias/{PROJECT}-{Plane.TENANT.value}-{stage}-{tenant_id}"
