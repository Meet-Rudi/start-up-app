"""
MEET_RUDI platform — a clone version, loaded and ready to run turns.

`load_clone` reads a version from the registry, runs the same checks as publishing (composition,
journey, personas, strings, assets), binds every real-time skill to the code of its exact pinned
version, and reads the clone's knowledge assets. Published versions are immutable, so a warm
engine caches a LoadedClone for as long as it likes.

Knowledge assets are the clone's own documents, named in `settings.assets`
(`{"about": "contexts/rudi-context.md"}`): the "About me" context, a guidance library. A skill
names the assets it cannot run without in its manifest; those must load. Any other configured
asset is optional and reads as "" when missing, as the legacy health guidance does today. Until
knowledge libraries exist (Phase 3), an asset is a key in a store (CM: the legacy data bucket).

An asset may have a version for one channel, named `<name>@<channel>`
(`"health_guidance@voice": "contexts/health-coaching-guidance.md"`). On that channel it replaces
the base version when it has text; skills always ask for the base name.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from ..i18n import Catalogue
from ..journey import Journey, parse_journey
from ..registry.models import Context, Persona
from ..registry.publishing import PublishRefused, check_version
from ..registry.store import RegistryStore
from ..skills.composer import ComposedClone, ResolvedSkill, parse_composition
from ..skills.contract import SkillKind
from .skill import RealTimeSkill, SkillRuntime

log = logging.getLogger(__name__)


class CloneLoadError(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


class AssetStore(Protocol):
    def text(self, key: str) -> str: ...


class DictAssetStore:
    def __init__(self, texts: Mapping[str, str]) -> None:
        self._texts = dict(texts)

    def text(self, key: str) -> str:
        return self._texts[key]


class S3AssetStore:
    """Assets from a bucket, cached per warm container with the version id that was read."""

    def __init__(self, s3: Any, bucket: str) -> None:
        self._s3, self._bucket = s3, bucket
        self._cache: dict[str, str] = {}
        self.versions: dict[str, str | None] = {}

    def text(self, key: str) -> str:
        if key not in self._cache:
            obj = self._s3.get_object(Bucket=self._bucket, Key=key)
            self._cache[key] = obj["Body"].read().decode("utf-8")
            self.versions[key] = obj.get("VersionId")
        return self._cache[key]


@dataclass(frozen=True)
class BoundSkill:
    resolved: ResolvedSkill
    # None for turn and background skills: they run elsewhere.
    impl: RealTimeSkill | None

    @property
    def slug(self) -> str:
        return self.resolved.manifest.slug

    @property
    def ref(self) -> str:
        return self.resolved.ref

    def has(self, hook: str) -> bool:
        return self.impl is not None and hook in self.resolved.manifest.hooks


@dataclass(frozen=True)
class LoadedClone:
    tenant_id: str
    clone_id: str
    version_id: str
    version_number: int
    composed: ComposedClone
    journey: Journey
    personas: Mapping[str, Persona]
    default_persona: str
    catalogue: Catalogue
    assets: Mapping[str, str]
    skills: tuple[BoundSkill, ...]

    def persona(self, key: str | None = None) -> Persona:
        """The persona an end-user is pinned to; the clone's default for an unknown key."""
        return self.personas.get(key or "") or self.personas[self.default_persona]

    @property
    def persona_names(self) -> frozenset[str]:
        return frozenset(p.name for p in self.personas.values())

    @property
    def default_locale(self) -> str:
        return self.catalogue.default_locale

    def assets_for(self, channel: str) -> dict[str, str]:
        """The clone's assets as skills on `channel` see them: base names, channel versions
        applied where they have text."""
        out = {k: v for k, v in self.assets.items() if "@" not in k}
        suffix = f"@{channel}"
        for key, text in self.assets.items():
            if key.endswith(suffix) and text:
                out[key[: -len(suffix)]] = text
        return out

    def memory_schema(self, channel: str) -> dict[str, str]:
        """The memory fields of the skills that run on `channel`: its stored state's shape."""
        return self.composed.memory_schema_for(channel)

    def skill(self, slug: str) -> BoundSkill | None:
        return next((b for b in self.skills if b.slug == slug), None)


def load_clone(
    registry: RegistryStore,
    ctx: Context,
    version_id: str,
    runtime: SkillRuntime,
    assets: AssetStore,
) -> LoadedClone:
    version = registry.get_version(ctx, version_id)
    try:
        composed = check_version(registry, ctx, version_id)
    except PublishRefused as e:
        raise CloneLoadError(e.problems) from None
    problems: list[str] = []
    bound: list[BoundSkill] = []
    for resolved in composed.skills:
        impl = None
        if resolved.manifest.kind is SkillKind.REAL_TIME:
            impl = runtime.get(resolved.ref)
            if impl is None:
                problems.append(f"{resolved.ref}: this build has no code for it")
        bound.append(BoundSkill(resolved, impl))

    texts: dict[str, str] = {}
    for name, key in (version.settings.get("assets") or {}).items():
        try:
            texts[name] = assets.text(key)
        except Exception as e:  # noqa: BLE001 - reported below or tolerated as optional
            if name in composed.assets:
                problems.append(
                    f"asset {name!r} could not be read ({type(e).__name__})"
                )
            else:
                log.info("optional asset %s unavailable (%s)", name, type(e).__name__)
                texts[name] = ""
    if problems:
        raise CloneLoadError(problems)

    personas = {p.key: p for p in registry.personas(ctx, version_id)}
    default = version.settings.get("default_persona") or min(personas)
    composition = parse_composition(version.composition)
    return LoadedClone(
        tenant_id=version.tenant_id,
        clone_id=version.clone_id,
        version_id=version.id,
        version_number=version.number,
        composed=composed,
        journey=parse_journey(version.journey),
        personas=personas,
        default_persona=default,
        catalogue=Catalogue(
            version.settings.get("strings") or {},
            default_locale=composition.default_locale,
        ),
        assets=texts,
        skills=tuple(bound),
    )
