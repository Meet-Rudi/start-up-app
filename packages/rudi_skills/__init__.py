"""
MEET_RUDI — the real-time skill packages the Turn Engine runs (blueprint §4).

One directory per skill: `skill.json` (the rudi-skill@1 manifest), `skill.py` (`class Skill`) and
`prompts/`. The directory name is the slug with underscores. Packs are in
`rudi_platform.skills.packs`; turn and background skills will live under services/skills/ as
their own Lambdas.

    from rudi_skills import runtime
    rt = runtime()          # every skill in this build, checked against its manifest
"""

from __future__ import annotations

import importlib
import os
import pkgutil

from rudi_platform.engine.skill import RealTimeSkill, SkillRuntime
from rudi_platform.skills.manifest import SkillManifest

HERE = os.path.dirname(os.path.abspath(__file__))


def load_all() -> list[RealTimeSkill]:
    skills: list[RealTimeSkill] = []
    for info in sorted(pkgutil.iter_modules([HERE]), key=lambda i: i.name):
        if not info.ispkg or info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{info.name}.skill")
        skill = module.Skill(os.path.join(HERE, info.name))
        if skill.slug.replace("-", "_") != info.name:
            raise ValueError(f"{info.name}/skill.json declares slug {skill.slug!r}")
        skills.append(skill)
    return skills


def runtime() -> SkillRuntime:
    return SkillRuntime(load_all())


def manifests() -> list[SkillManifest]:
    return [s.manifest for s in load_all()]
