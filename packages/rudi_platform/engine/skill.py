"""
MEET_RUDI platform — the base of every real-time skill package, and the runtime that holds them.

A real-time skill is a directory (packages/rudi_skills/<slug_with_underscores>/):

    skill.json             the `rudi-skill@1` manifest
    skill.py               `class Skill(RealTimeSkill)` implementing the manifest's hooks
    prompts/<section>.md          a section whose locales are ["*"] (instruction text)
    prompts/<section>.<locale>.md a localised section

Every hook takes a `SkillRequest` and returns a `SkillResult` (the one envelope, decision #25),
so a skill can later move into its own Lambda without its caller changing. What the engine puts
in `request.input` is documented on `TurnEngine`; `request.settings` are the skill's resolved
settings (defaults overlaid with the clone's overrides).

`SkillRuntime` refuses a build in which a skill implements a hook its manifest does not declare,
or declares one it does not implement: the manifest is what the composer and the console trust.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from typing import Any

from ..i18n import normalize_locale
from ..skills.contract import SkillKind, SkillRequest, SkillResult
from ..skills.manifest import ANY_LOCALE, HOOKS, SkillManifest, parse_manifest
from .prompt import fill


class SkillLoadError(Exception):
    """A skill package is inconsistent with its manifest."""


class RealTimeSkill:
    """Subclass per skill; override exactly the hooks the manifest lists."""

    def __init__(self, directory: str) -> None:
        self.directory = directory
        with open(os.path.join(directory, "skill.json"), encoding="utf-8") as f:
            self.manifest: SkillManifest = parse_manifest(json.load(f))
        if self.manifest.kind is not SkillKind.REAL_TIME:
            raise SkillLoadError(f"{self.ref}: only real-time skills run in the engine")
        self._prompts: dict[tuple[str, str], str] = {}
        for section, spec in self.manifest.prompt_sections.items():
            for loc in spec.locales:
                name = f"{section}.md" if loc == ANY_LOCALE else f"{section}.{loc}.md"
                path = os.path.join(directory, "prompts", name)
                try:
                    with open(path, encoding="utf-8") as f:
                        self._prompts[(section, loc)] = f.read().rstrip()
                except OSError:
                    raise SkillLoadError(
                        f"{self.ref}: missing prompts/{name}"
                    ) from None

    # ------------------------------------------------------------------------------ identity
    @property
    def ref(self) -> str:
        return str(self.manifest.ref)

    @property
    def slug(self) -> str:
        return self.manifest.slug

    def implemented_hooks(self) -> frozenset[str]:
        return frozenset(
            h for h in HOOKS if getattr(type(self), h) is not getattr(RealTimeSkill, h)
        )

    # ------------------------------------------------------------------------------- helpers
    def values(self, req: SkillRequest) -> dict[str, object]:
        """Placeholder values for this skill's prompt text: `{persona}` and the skill's text and
        number settings (`{helpline_url}`). Override to add derived values."""
        out: dict[str, object] = {
            k: v
            for k, v in req.settings.items()
            if isinstance(v, str | int | float) and not isinstance(v, bool)
        }
        out["persona"] = (req.input.get("persona") or {}).get("name", "")
        return out

    def section(self, req: SkillRequest, name: str) -> str:
        """Prompt section `name` in the person's language, with the clone's override applied and
        placeholders filled."""
        spec = self.manifest.prompt_sections[name]
        overrides: Mapping[str, Any] = req.input.get("prompt_overrides") or {}
        override = overrides.get(name)
        if ANY_LOCALE in spec.locales:
            key = ANY_LOCALE
        else:
            wanted = normalize_locale(req.input.get("locale"))
            texts = override.get("text", {}) if isinstance(override, Mapping) else {}
            localised = {normalize_locale(k) for k in texts}
            if wanted in spec.locales or wanted in localised:
                key = str(wanted)
            else:
                key = "en" if "en" in spec.locales else min(spec.locales)
        text = self._prompts.get((name, key), "")
        if isinstance(override, Mapping):
            texts = {
                (k if k == ANY_LOCALE else normalize_locale(k)): v
                for k, v in (override.get("text") or {}).items()
            }
            extra = texts.get(key)
            if isinstance(extra, str) and extra.strip():
                if override.get("mode") == "replace":
                    text = extra.rstrip()
                else:
                    text = f"{text}\n\n{extra.rstrip()}" if text else extra.rstrip()
        return fill(text, self.values(req))

    @staticmethod
    def part(slot: str, text: str, order: int = 50) -> dict[str, Any]:
        return {"slot": slot, "text": text, "order": order}

    # --------------------------------------------------------------------------------- hooks
    def on_inbound(self, req: SkillRequest) -> SkillResult:
        raise NotImplementedError

    def on_enter(self, req: SkillRequest) -> SkillResult:
        raise NotImplementedError

    def contribute(self, req: SkillRequest) -> SkillResult:
        raise NotImplementedError

    def on_reply(self, req: SkillRequest) -> SkillResult:
        raise NotImplementedError

    def on_signals(self, req: SkillRequest) -> SkillResult:
        raise NotImplementedError


class SkillRuntime:
    """The real-time skills one build ships, by exact version."""

    def __init__(self, skills: Iterable[RealTimeSkill]) -> None:
        self._by_ref: dict[str, RealTimeSkill] = {}
        problems: list[str] = []
        for skill in skills:
            declared, built = set(skill.manifest.hooks), set(skill.implemented_hooks())
            if declared != built:
                problems.append(
                    f"{skill.ref}: manifest hooks {sorted(declared)} but code implements "
                    f"{sorted(built)}"
                )
            if skill.ref in self._by_ref:
                problems.append(f"{skill.ref} is in the build twice")
            self._by_ref[skill.ref] = skill
        if problems:
            raise SkillLoadError("; ".join(problems))

    def get(self, ref: str) -> RealTimeSkill | None:
        return self._by_ref.get(ref)

    def manifests(self) -> list[SkillManifest]:
        return [s.manifest for s in self._by_ref.values()]

    def __iter__(self):
        return iter(self._by_ref.values())
