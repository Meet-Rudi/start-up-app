"""
MEET_RUDI platform — composing a clone from skills, checked at publish (blueprint §4.2, §5).

A clone version's composition pins each skill to a major version and may override what the
skill's manifest marks overridable:

    {
      "channels": ["whatsapp", "voice"],
      "locales": ["nl", "en"],                 # first = the clone's default language
      "skills": [
        {"skill": "goal-setting", "major": 1,
         "settings": {"max_clarifiers": 1},
         "prompt_overrides": {"examples": {"mode": "append", "text": {"nl": "…"}}}}
      ]
    }

`compose()` resolves each pin to the newest released version of that major (minors and patches
roll out automatically, decision #25) and checks the whole mix. It never raises for a bad mix:
it returns every error, warning and review reason, because a clone designer fixes them together
and a reviewer reads them together. Errors block staging; review reasons go to the person who
approves live (A7); warnings are shown in the console.

Checks:
  - each skill exists with a released version of the pinned major; no skill twice;
  - `requires` present with the same major; no `conflicts` together;
  - each skill serves at least one of the clone's channels and is fast enough for each of them
    (a voice channel needs voice-class skills in the turn; background skills are exempt);
  - overrides only where the manifest allows, with valid values;
  - prompt sections cover the clone's languages (warning) and overrides use them (error);
  - no memory field written with two different types; reads nobody writes (warning);
  - baseline skills removed or changed, outbound actions and health data (review reasons).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..i18n import normalize_locale
from .contract import SkillKind
from .manifest import CHANNEL_LATENCY, LATENCIES, SLUG_RE, SkillManifest

# Memory the platform itself provides; skills may read it without a writer.
PLATFORM_MEMORY = frozenset({"locale", "timezone", "first_seen_at", "last_inbound_at"})
OVERRIDE_MODES = ("append", "replace")


class CompositionError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class SkillChoice:
    slug: str
    major: int
    settings: Mapping[str, Any] = field(default_factory=dict)
    prompt_overrides: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class Composition:
    skills: tuple[SkillChoice, ...]
    channels: frozenset[str]
    locales: tuple[str, ...]

    @property
    def default_locale(self) -> str:
        return self.locales[0]


def parse_composition(raw: Mapping[str, Any]) -> Composition:
    """The composition stored on a clone version; CompositionError for a malformed one."""
    problems: list[str] = []
    if not isinstance(raw, Mapping):
        raise CompositionError(["a composition must be an object"])
    channels = raw.get("channels") or []
    if (
        not isinstance(channels, list)
        or not channels
        or not set(channels) <= set(CHANNEL_LATENCY)
    ):
        problems.append(
            f"channels must be a non-empty list from {sorted(CHANNEL_LATENCY)}"
        )
    locales: list[str] = []
    for loc in raw.get("locales") or []:
        code = normalize_locale(loc)
        if code is None:
            problems.append(f"bad locale {loc!r}")
        elif code not in locales:
            locales.append(code)
    if not locales:
        problems.append("locales must list at least one language")
    choices: list[SkillChoice] = []
    for i, item in enumerate(raw.get("skills") or []):
        if not isinstance(item, Mapping):
            problems.append(f"skills[{i}] must be an object")
            continue
        slug, major = item.get("skill"), item.get("major")
        if not isinstance(slug, str) or not SLUG_RE.match(slug):
            problems.append(f"skills[{i}].skill must be a skill slug")
            continue
        if not isinstance(major, int) or isinstance(major, bool) or major < 0:
            problems.append(
                f"skills[{i}] ({slug}): major must be a non-negative integer"
            )
            continue
        # `is None`, not `or`: an empty list is a malformed value, not "no overrides".
        settings = {} if item.get("settings") is None else item["settings"]
        overrides = (
            {} if item.get("prompt_overrides") is None else item["prompt_overrides"]
        )
        if not isinstance(settings, Mapping) or not isinstance(overrides, Mapping):
            problems.append(f"{slug}: settings and prompt_overrides must be objects")
            continue
        choices.append(SkillChoice(slug, major, dict(settings), dict(overrides)))
    if not choices:
        problems.append("a clone needs at least one skill")
    if problems:
        raise CompositionError(problems)
    return Composition(tuple(choices), frozenset(channels), tuple(locales))


@dataclass(frozen=True)
class ResolvedSkill:
    manifest: SkillManifest
    settings: Mapping[
        str, Any
    ]  # every setting, defaults overlaid with the clone's overrides
    prompt_overrides: Mapping[str, Mapping[str, Any]]
    channels: frozenset[str]  # where this skill runs in this clone

    @property
    def ref(self) -> str:
        return str(self.manifest.ref)


@dataclass(frozen=True)
class ComposedClone:
    skills: tuple[ResolvedSkill, ...]
    per_channel: Mapping[str, tuple[str, ...]]
    memory_schema: Mapping[str, str]
    markers: tuple[str, ...]
    data_classes: frozenset[str]
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    review_reasons: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors

    def pins(self) -> dict[str, str]:
        """slug → exact version, as recorded on every turn."""
        return {s.manifest.slug: s.ref for s in self.skills}


def _newest(versions: Sequence[SkillManifest], major: int) -> SkillManifest | None:
    same = [m for m in versions if m.major == major]
    return max(same, key=lambda m: m.version) if same else None


def _too_slow(skill_latency: str, channel: str) -> bool:
    return LATENCIES.index(skill_latency) > LATENCIES.index(CHANNEL_LATENCY[channel])


def compose(
    composition: Composition,
    inventory: Mapping[str, Sequence[SkillManifest]],
    baseline: Iterable[str] = (),
) -> ComposedClone:
    """Resolve and check a composition against the released skill inventory."""
    errors: list[str] = []
    warnings: list[str] = []
    review: list[str] = []
    resolved: list[ResolvedSkill] = []
    chosen: dict[str, int] = {}

    for choice in composition.skills:
        if choice.slug in chosen:
            errors.append(f"{choice.slug} is in the clone twice")
            continue
        chosen[choice.slug] = choice.major
        manifest = _newest(inventory.get(choice.slug, ()), choice.major)
        if manifest is None:
            errors.append(f"{choice.slug}@{choice.major}: no released version")
            continue

        effective = manifest.channels & composition.channels
        if not effective:
            errors.append(f"{choice.slug} serves none of the clone's channels")
        if manifest.kind is not SkillKind.BACKGROUND and manifest.latency:
            for ch in sorted(effective):
                if _too_slow(manifest.latency, ch):
                    errors.append(
                        f"{choice.slug} is too slow for {ch} (it is {manifest.latency}-class)"
                    )

        settings = {name: s.default for name, s in manifest.settings.items()}
        for name, value in choice.settings.items():
            spec = manifest.settings.get(name)
            if spec is None:
                errors.append(f"{choice.slug}: unknown setting {name!r}")
            elif not spec.overridable:
                errors.append(f"{choice.slug}: setting {name!r} is not overridable")
            elif (problem := spec.problem(value)) is not None:
                errors.append(f"{choice.slug}: {problem}")
            else:
                settings[name] = value

        for section, override in choice.prompt_overrides.items():
            spec_s = manifest.prompt_sections.get(section)
            where = f"{choice.slug}: prompt section {section!r}"
            if spec_s is None:
                errors.append(f"{where} does not exist")
                continue
            if not spec_s.overridable:
                errors.append(f"{where} is not overridable")
                continue
            texts = override.get("text") if isinstance(override, Mapping) else None
            if (
                not isinstance(override, Mapping)
                or override.get("mode") not in OVERRIDE_MODES
            ):
                errors.append(f"{where}: mode must be one of {list(OVERRIDE_MODES)}")
            elif not isinstance(texts, Mapping) or not texts:
                errors.append(f"{where}: text must map languages to text")
            else:
                for loc, text in texts.items():
                    if normalize_locale(loc) not in composition.locales:
                        errors.append(
                            f"{where}: {loc!r} is not one of the clone's languages"
                        )
                    elif not isinstance(text, str) or not text.strip():
                        errors.append(f"{where}: empty text for {loc!r}")

        for section, spec_s in manifest.prompt_sections.items():
            ov = choice.prompt_overrides.get(section)
            texts = ov.get("text") if isinstance(ov, Mapping) else None
            replaced = (
                {normalize_locale(loc) for loc in texts}
                if isinstance(texts, Mapping)
                else set()
            )
            gaps = [
                loc
                for loc in composition.locales
                if loc not in spec_s.locales and loc not in replaced
            ]
            if gaps:
                warnings.append(
                    f"{choice.slug}: prompt section {section!r} has no {', '.join(gaps)} text"
                )

        resolved.append(
            ResolvedSkill(
                manifest, settings, dict(choice.prompt_overrides), frozenset(effective)
            )
        )

    by_slug = {r.manifest.slug: r for r in resolved}
    for r in resolved:
        for req in r.manifest.requires:
            if chosen.get(req.slug) != req.major:
                errors.append(f"{r.manifest.slug} requires {req}")
        for other in sorted(r.manifest.conflicts):
            if other in by_slug:
                errors.append(f"{r.manifest.slug} conflicts with {other}")

    schema: dict[str, str] = {}
    writers: dict[str, str] = {}
    for r in resolved:
        for name, t in r.manifest.memory_writes.items():
            if name in schema and schema[name] != t:
                errors.append(
                    f"memory field {name!r}: {writers[name]} writes {schema[name]}, "
                    f"{r.manifest.slug} writes {t}"
                )
            else:
                schema.setdefault(name, t)
                writers.setdefault(name, r.manifest.slug)
    for r in resolved:
        for name, t in r.manifest.memory_reads.items():
            if name in PLATFORM_MEMORY:
                continue
            if name not in schema:
                warnings.append(
                    f"{r.manifest.slug} reads {name!r}, which no skill in this clone writes"
                )
            elif schema[name] != t:
                errors.append(
                    f"memory field {name!r}: {r.manifest.slug} reads {t}, written as {schema[name]}"
                )

    for slug in baseline:
        if slug not in chosen:
            review.append(
                f"baseline skill {slug} removed: needs a contract addendum, review and an arena gate"
            )
        elif slug in by_slug and any(
            c.slug == slug and (c.settings or c.prompt_overrides)
            for c in composition.skills
        ):
            review.append(
                f"baseline skill {slug} changed: needs a contract addendum, review and an arena gate"
            )
    data_classes = frozenset().union(*(r.manifest.data_classes for r in resolved))
    if "health" in data_classes:
        review.append(
            "processes health data: consent capture must be configured for this clone"
        )
    for r in resolved:
        if "outbound_webhook" in r.manifest.permissions:
            review.append(f"{r.manifest.slug} calls a tenant webhook (action skill)")

    per_channel = {
        ch: tuple(r.manifest.slug for r in resolved if ch in r.channels)
        for ch in sorted(composition.channels)
    }
    markers = tuple(dict.fromkeys(m for r in resolved for m in r.manifest.markers))
    return ComposedClone(
        skills=tuple(resolved),
        per_channel=per_channel,
        memory_schema=schema,
        markers=markers,
        data_classes=data_classes,
        errors=tuple(errors),
        warnings=tuple(warnings),
        review_reasons=tuple(review),
    )
