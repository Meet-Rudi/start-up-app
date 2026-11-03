"""Synthetic skill manifests shared by the composer and registry tests. Not a test module."""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..")))

from rudi_platform.skills.manifest import FORMAT, SkillManifest, parse_manifest

ALL_CHANNELS = ["whatsapp", "voice", "api", "widget", "sms", "email", "telegram"]


def raw(slug: str, version: str = "1.0.0", kind: str = "real_time", **fields) -> dict:
    base = {
        "format": FORMAT,
        "slug": slug,
        "version": version,
        "kind": kind,
        "name": slug.replace("-", " ").title(),
        "summary": f"Synthetic {slug} for tests.",
        "channels": ALL_CHANNELS,
    }
    if kind == "real_time":
        base["hooks"] = ["contribute"]
    elif kind == "turn":
        base["tool"] = {
            "name": slug.replace("-", "_"),
            "description": "x",
            "input_schema": {},
        }
        base["latency"] = "interactive"
    else:
        base["triggers"] = ["turn.completed"]
    base.update(fields)
    return base


def m(
    slug: str, version: str = "1.0.0", kind: str = "real_time", **fields
) -> SkillManifest:
    return parse_manifest(raw(slug, version, kind, **fields))


GOAL_SETTINGS = {
    "max_clarifiers": {
        "type": "int",
        "default": 2,
        "min": 0,
        "max": 5,
        "overridable": True,
    },
    "domain_list": {
        "type": "str_list",
        "default": ["fitness", "sleep"],
        "overridable": False,
    },
    "tone": {
        "type": "enum",
        "choices": ["warm", "brisk"],
        "default": "warm",
        "overridable": True,
    },
}
GOAL_SECTIONS = {
    "rules": {"overridable": False, "locales": ["en", "nl"]},
    "examples": {"overridable": True, "locales": ["en"]},
}


def inventory() -> dict[str, list[SkillManifest]]:
    """A small released inventory: an engine, goal setting in three versions, retrieval,
    a summary job, a guard and a webhook action."""
    goal = {
        "requires": ["journey-engine@1"],
        "settings": GOAL_SETTINGS,
        "prompt_sections": GOAL_SECTIONS,
        "memory": {"writes": {"goal": "str", "goal_domain": "str"}},
        "markers": ["goal_status"],
    }
    return {
        "journey-engine": [
            m("journey-engine", hooks=["on_signals"], markers=["[runtime:"])
        ],
        "goal-setting": [
            m("goal-setting", "1.0.0", **goal),
            m("goal-setting", "1.2.0", **goal),
            m("goal-setting", "2.0.0", **{**goal, "requires": []}),
        ],
        "knowledge-retrieval": [
            m("knowledge-retrieval", kind="turn", model_tier="classify")
        ],
        "profile-summary": [
            m(
                "profile-summary",
                kind="background",
                memory={
                    "reads": {"goal": "str", "locale": "str"},
                    "writes": {"summary": "str"},
                },
            )
        ],
        "crisis-signposting": [
            m("crisis-signposting", hooks=["on_inbound", "on_reply"])
        ],
        "booking": [
            m(
                "booking",
                kind="turn",
                permissions=["outbound_webhook"],
                conflicts=["knowledge-retrieval"],
                data_classes=["personal", "health"],
            )
        ],
        "goal-tracker": [m("goal-tracker", memory={"writes": {"goal": "json"}})],
    }
