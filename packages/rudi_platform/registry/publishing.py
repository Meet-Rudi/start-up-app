"""
MEET_RUDI platform — moving a clone version forward (blueprint §5.1, A7, B6).

    draft ──publish_to_staging──▶ staging ──approve_live──▶ live      (previous live → retired)
           tenant or platform              platform only (our review)

Both steps compose the version against the released skill inventory and refuse when the
composer finds errors, when there is no persona, or when the clone's strings miss a language it
serves. `approve_live` composes again because pinned majors resolve to the newest minor, which
may have changed since staging. Every outcome, refused or not, is written to the audit log.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ..i18n import Catalogue, I18nError
from ..skills.composer import (
    ComposedClone,
    CompositionError,
    compose,
    parse_composition,
)
from ..skills.packs import BASELINE_SKILLS
from .models import CloneVersion, Context, PlatformContext
from .store import RegistryStore


class PublishRefused(Exception):
    def __init__(
        self, problems: list[str], composed: ComposedClone | None = None
    ) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems
        self.composed = composed


@dataclass(frozen=True)
class PublishResult:
    version: CloneVersion
    composed: ComposedClone


def check_version(
    store: RegistryStore,
    ctx: Context,
    version_id: str,
    baseline: Iterable[str] = BASELINE_SKILLS,
) -> ComposedClone:
    """Compose a version and add the checks that need more than the composition."""
    version = store.get_version(ctx, version_id)
    try:
        composition = parse_composition(version.composition)
    except CompositionError as e:
        raise PublishRefused(e.problems) from None
    composed = compose(composition, store.released_manifests(ctx), baseline)
    problems = list(composed.errors)
    if not store.personas(ctx, version_id):
        problems.append("a clone version needs at least one persona")
    try:
        catalogue = Catalogue(
            version.settings.get("strings") or {},
            default_locale=composition.default_locale,
        )
        for locale, keys in catalogue.missing(composition.locales).items():
            problems.append(f"no {locale} text for: {', '.join(keys)}")
    except I18nError as e:
        problems.append(f"strings: {e}")
    if problems:
        raise PublishRefused(problems, composed)
    return composed


def _audited(
    store: RegistryStore,
    ctx: Context,
    action: str,
    version_id: str,
    actor: str | None,
    baseline: Iterable[str],
    to_stage: str,
) -> PublishResult:
    version = store.get_version(ctx, version_id)
    try:
        composed = check_version(store, ctx, version_id, baseline)
    except PublishRefused as refused:
        store.append_audit(
            ctx,
            action,
            actor_account_id=actor,
            tenant_id=version.tenant_id,
            target_id=version_id,
            decision={"allowed": False, "problems": refused.problems},
        )
        raise
    promoted = store.promote(ctx, version_id, to_stage)
    store.append_audit(
        ctx,
        action,
        actor_account_id=actor,
        tenant_id=version.tenant_id,
        target_id=version_id,
        decision={
            "allowed": True,
            "pins": composed.pins(),
            "review_reasons": list(composed.review_reasons),
            "warnings": list(composed.warnings),
        },
    )
    return PublishResult(promoted, composed)


def publish_to_staging(
    store: RegistryStore,
    ctx: Context,
    version_id: str,
    actor: str | None = None,
    baseline: Iterable[str] = BASELINE_SKILLS,
) -> PublishResult:
    return _audited(
        store, ctx, "clone.publish.staging", version_id, actor, baseline, "staging"
    )


def approve_live(
    store: RegistryStore,
    ctx: PlatformContext,
    version_id: str,
    actor: str | None = None,
    baseline: Iterable[str] = BASELINE_SKILLS,
) -> PublishResult:
    """Our go-live review. The reviewer reads `composed.review_reasons` before calling this."""
    return _audited(
        store, ctx, "clone.approve.live", version_id, actor, baseline, "live"
    )
