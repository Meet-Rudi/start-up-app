"""
MEET_RUDI platform — the skill manifest (`rudi-skill@1`).

Every runtime skill package carries a manifest (skill.yaml, compiled to JSON at build time so
Lambdas never need a YAML parser). It is what the composer checks a clone against and what the
console shows. The training brief is a separate document (decision #25, C7); the two meet only
through `slug@version`.

    {
      "format": "rudi-skill@1",
      "slug": "goal-setting", "version": "1.4.2", "kind": "real_time",
      "name": "Goal setting", "summary": "Elicits one self-directed goal …",
      "channels": ["whatsapp", "voice", "api", "widget"],
      "latency": "voice",                       # fastest class it can serve (not for background)
      "model_tier": "coach",                    # gateway tier, if it calls a model
      "hooks": ["on_enter", "contribute", "on_signals"],   # real_time only
      "outcomes": ["accepted", "gave_up"],      # real_time only: what on_signals may report
      "tool": {"name": "...", "description": "...", "input_schema": {...}},   # turn only
      "triggers": ["turn.completed"],           # background only
      "settings": {"max_clarifiers": {"type": "int", "default": 2, "min": 0, "max": 5,
                                      "overridable": true}},
      "prompt_sections": {"rules": {"overridable": false, "locales": ["en", "nl"]},
                          "body": {"overridable": false, "locales": ["*"]}},
      "memory": {"reads": {"goal": "str"},
                 "writes": {"goal": "str", "goal_domain": "str", "clarifiers_used": "int"},
                 "session": ["clarifiers_used"]},
      "assets": ["about"],                      # clone knowledge the skill cannot run without
      "requires": ["persona-ocean@1"], "conflicts": [],
      "signals": ["goal_status"], "markers": ["goal_status"],
      "data_classes": ["personal"], "permissions": []
    }

Hooks (real-time skills, run in the engine process): `on_inbound` (gates the inbound message),
`on_enter` (the conversation enters the journey stage this skill serves), `contribute` (prompt
parts), `on_reply` (gates the model's reply), `on_signals` (reads the reply's signals; a stage
skill reports one of its `outcomes`, which the journey maps to the next stage).

`memory.session` lists written fields that belong to one session: they are cleared when a new
session opens, the rest of the conversation's memory carries over. A prompt section whose locales
are `["*"]` is model instruction text that serves every language (the model answers in the
person's language); a list of codes means localised text.

`parse_manifest` collects every problem before raising, so an author fixes a manifest in one go.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..gateway.endpoints import TIERS
from .contract import SKILL_REF_RE, SkillKind, SkillRef

FORMAT = "rudi-skill@1"
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
REQUIRE_RE = re.compile(r"^(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)@(?P<major>\d+)$")

HOOKS = ("on_inbound", "on_enter", "contribute", "on_reply", "on_signals")
ANY_LOCALE = "*"
TRIGGERS = (
    "turn.completed",
    "schedule.due",
    "conversation.concluded",
    "knowledge.uploaded",
)
LATENCIES = ("voice", "interactive", "messaging")  # fastest first
CHANNEL_LATENCY = {
    "voice": "voice",
    "api": "interactive",
    "widget": "interactive",
    "whatsapp": "messaging",
    "sms": "messaging",
    "email": "messaging",
    "telegram": "messaging",
}
CHANNELS = tuple(CHANNEL_LATENCY)
SETTING_TYPES = ("int", "float", "bool", "str", "enum", "str_list")
MEMORY_TYPES = ("str", "int", "float", "bool", "datetime", "json", "str_list")
DATA_CLASSES = ("personal", "health")
PERMISSIONS = ("outbound_webhook", "send_proactive", "read_knowledge")


class ManifestError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Setting:
    name: str
    type: str
    default: Any
    overridable: bool = False
    min: float | None = None
    max: float | None = None
    choices: tuple[str, ...] = ()

    def problem(self, value: Any) -> str | None:
        """Why `value` is not acceptable for this setting, or None."""
        t = self.type
        if t == "bool":
            ok = isinstance(value, bool)
        elif t == "int":
            ok = isinstance(value, int) and not isinstance(value, bool)
        elif t == "float":
            ok = isinstance(value, int | float) and not isinstance(value, bool)
        elif t == "str":
            ok = isinstance(value, str)
        elif t == "enum":
            ok = value in self.choices
        else:  # str_list
            ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
        if not ok:
            want = f"one of {list(self.choices)}" if t == "enum" else t
            return f"{self.name} must be {want}, got {value!r}"
        if t in ("int", "float"):
            if self.min is not None and value < self.min:
                return f"{self.name} must be at least {self.min}, got {value!r}"
            if self.max is not None and value > self.max:
                return f"{self.name} must be at most {self.max}, got {value!r}"
        return None


@dataclass(frozen=True)
class PromptSection:
    name: str
    overridable: bool
    locales: frozenset[str]


@dataclass(frozen=True)
class Requirement:
    slug: str
    major: int

    def __str__(self) -> str:
        return f"{self.slug}@{self.major}"


@dataclass(frozen=True)
class SkillManifest:
    slug: str
    version: tuple[int, int, int]
    kind: SkillKind
    name: str
    summary: str
    channels: frozenset[str]
    latency: str | None = None
    model_tier: str | None = None
    hooks: frozenset[str] = frozenset()
    tool: Mapping[str, Any] | None = None
    triggers: frozenset[str] = frozenset()
    settings: Mapping[str, Setting] = field(default_factory=dict)
    prompt_sections: Mapping[str, PromptSection] = field(default_factory=dict)
    memory_reads: Mapping[str, str] = field(default_factory=dict)
    memory_writes: Mapping[str, str] = field(default_factory=dict)
    memory_session: frozenset[str] = frozenset()
    outcomes: frozenset[str] = frozenset()
    assets: frozenset[str] = frozenset()
    requires: tuple[Requirement, ...] = ()
    conflicts: frozenset[str] = frozenset()
    signals: frozenset[str] = frozenset()
    markers: tuple[str, ...] = ()
    data_classes: frozenset[str] = frozenset()
    permissions: frozenset[str] = frozenset()
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def ref(self) -> SkillRef:
        return SkillRef(self.slug, self.version)

    @property
    def major(self) -> int:
        return self.version[0]


def _strings(value: Any, where: str, problems: list[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        problems.append(f"{where} must be a list of strings")
        return []
    return value


def _subset(
    values: list[str], allowed: tuple[str, ...], where: str, problems: list[str]
) -> None:
    unknown = sorted(set(values) - set(allowed))
    if unknown:
        problems.append(f"{where}: unknown {unknown} (allowed: {list(allowed)})")


def _parse_settings(raw: Any, problems: list[str]) -> dict[str, Setting]:
    out: dict[str, Setting] = {}
    if raw is None:
        return out
    if not isinstance(raw, Mapping):
        problems.append("settings must be an object")
        return out
    for name, spec in raw.items():
        where = f"settings.{name}"
        if not NAME_RE.match(str(name)) or not isinstance(spec, Mapping):
            problems.append(f"{where}: needs a snake_case name and an object")
            continue
        t = spec.get("type")
        if t not in SETTING_TYPES:
            problems.append(f"{where}.type must be one of {list(SETTING_TYPES)}")
            continue
        choices = tuple(spec.get("choices") or ())
        if t == "enum" and (
            not choices or not all(isinstance(c, str) for c in choices)
        ):
            problems.append(f"{where}: an enum needs string choices")
            continue
        setting = Setting(
            name=str(name),
            type=t,
            default=spec.get("default"),
            overridable=bool(spec.get("overridable", False)),
            min=spec.get("min"),
            max=spec.get("max"),
            choices=choices,
        )
        bad_default = setting.problem(setting.default)
        if bad_default:
            problems.append(f"{where}: default invalid ({bad_default})")
            continue
        out[setting.name] = setting
    return out


def _parse_sections(raw: Any, problems: list[str]) -> dict[str, PromptSection]:
    out: dict[str, PromptSection] = {}
    if raw is None:
        return out
    if not isinstance(raw, Mapping):
        problems.append("prompt_sections must be an object")
        return out
    for name, spec in raw.items():
        where = f"prompt_sections.{name}"
        if not NAME_RE.match(str(name)) or not isinstance(spec, Mapping):
            problems.append(f"{where}: needs a snake_case name and an object")
            continue
        locales = _strings(spec.get("locales"), f"{where}.locales", problems)
        if not locales:
            problems.append(f"{where}: lists no locales")
            continue
        if ANY_LOCALE in locales and len(locales) > 1:
            problems.append(f"{where}: '*' (every language) stands alone")
            continue
        out[str(name)] = PromptSection(
            str(name), bool(spec.get("overridable", False)), frozenset(locales)
        )
    return out


def _parse_memory(raw: Any, key: str, problems: list[str]) -> dict[str, str]:
    block = (raw or {}).get(key) if isinstance(raw or {}, Mapping) else None
    if block is None:
        return {}
    if not isinstance(block, Mapping):
        problems.append(f"memory.{key} must map field names to types")
        return {}
    out: dict[str, str] = {}
    for name, t in block.items():
        if not NAME_RE.match(str(name)) or t not in MEMORY_TYPES:
            problems.append(
                f"memory.{key}.{name}: needs a snake_case name and a type in {list(MEMORY_TYPES)}"
            )
            continue
        out[str(name)] = t
    return out


def _names(raw: Any, where: str, problems: list[str]) -> list[str]:
    names = _strings(raw, where, problems)
    bad = [n for n in names if not NAME_RE.match(n)]
    if bad:
        problems.append(f"{where}: {bad} must be snake_case names")
    return [n for n in names if NAME_RE.match(n)]


def parse_manifest(raw: Mapping[str, Any]) -> SkillManifest:
    """A validated manifest, or ManifestError listing every problem found."""
    problems: list[str] = []
    if not isinstance(raw, Mapping):
        raise ManifestError(["a manifest must be an object"])
    if raw.get("format") != FORMAT:
        problems.append(f"format must be {FORMAT!r}")
    slug = str(raw.get("slug") or "")
    if not SLUG_RE.match(slug):
        problems.append("slug must be lowercase words joined by '-'")
    version = (0, 0, 0)
    ver = str(raw.get("version") or "")
    if SKILL_REF_RE.match(f"x@{ver}"):
        version = tuple(int(x) for x in ver.split("."))  # type: ignore[assignment]
    else:
        problems.append("version must be MAJOR.MINOR.PATCH")
    try:
        kind = SkillKind(raw.get("kind"))
    except ValueError:
        problems.append(f"kind must be one of {[k.value for k in SkillKind]}")
        kind = SkillKind.REAL_TIME
    name = str(raw.get("name") or "").strip()
    summary = str(raw.get("summary") or "").strip()
    if not name or not summary:
        problems.append("name and summary are required")

    channels = _strings(raw.get("channels"), "channels", problems)
    _subset(channels, CHANNELS, "channels", problems)
    if not channels:
        problems.append("channels must list at least one channel")

    hooks = _strings(raw.get("hooks"), "hooks", problems)
    _subset(hooks, HOOKS, "hooks", problems)
    triggers = _strings(raw.get("triggers"), "triggers", problems)
    _subset(triggers, TRIGGERS, "triggers", problems)
    tool = raw.get("tool")
    latency = raw.get("latency")

    if kind is SkillKind.REAL_TIME:
        if not hooks:
            problems.append("a real-time skill implements at least one hook")
        if tool is not None or triggers:
            problems.append("a real-time skill has no tool and no triggers")
        latency = latency or "voice"
    elif kind is SkillKind.TURN:
        if hooks or triggers:
            problems.append("a turn skill has no hooks and no triggers")
        if not (
            isinstance(tool, Mapping)
            and NAME_RE.match(str(tool.get("name") or ""))
            and str(tool.get("description") or "").strip()
            and isinstance(tool.get("input_schema"), Mapping)
        ):
            problems.append(
                "a turn skill needs tool.name (snake_case), tool.description and tool.input_schema"
            )
        if latency is None:
            problems.append("a turn skill must state its latency class")
    else:
        if hooks or tool is not None:
            problems.append("a background skill has no hooks and no tool")
        if not triggers:
            problems.append("a background skill needs at least one trigger")
        if latency is not None:
            problems.append(
                "a background skill runs outside the turn and has no latency class"
            )
    if latency is not None and latency not in LATENCIES:
        problems.append(f"latency must be one of {list(LATENCIES)}")

    tier = raw.get("model_tier")
    if tier is not None and tier not in TIERS:
        problems.append(f"model_tier must be one of {list(TIERS)}")

    requires: list[Requirement] = []
    for r in _strings(raw.get("requires"), "requires", problems):
        m = REQUIRE_RE.match(r)
        if not m:
            problems.append(f"requires: {r!r} must look like 'slug@1'")
        else:
            requires.append(Requirement(m.group("slug"), int(m.group("major"))))
    conflicts = _strings(raw.get("conflicts"), "conflicts", problems)
    if slug and (slug in conflicts or any(r.slug == slug for r in requires)):
        problems.append("a skill cannot require or conflict with itself")

    data_classes = _strings(raw.get("data_classes"), "data_classes", problems)
    _subset(data_classes, DATA_CLASSES, "data_classes", problems)
    permissions = _strings(raw.get("permissions"), "permissions", problems)
    _subset(permissions, PERMISSIONS, "permissions", problems)

    outcomes = _names(raw.get("outcomes"), "outcomes", problems)
    if outcomes and kind is not SkillKind.REAL_TIME:
        problems.append(
            "only a real-time skill serves a journey stage and reports outcomes"
        )
    if outcomes and "on_signals" not in hooks:
        problems.append("a skill that reports outcomes implements on_signals")
    assets = _names(raw.get("assets"), "assets", problems)

    memory_writes = _parse_memory(raw.get("memory"), "writes", problems)
    memory_raw = raw.get("memory") if isinstance(raw.get("memory"), Mapping) else {}
    session = _names(memory_raw.get("session"), "memory.session", problems)
    unwritten = sorted(set(session) - set(memory_writes))
    if unwritten:
        problems.append(f"memory.session: {unwritten} must be fields the skill writes")

    manifest = SkillManifest(
        slug=slug,
        version=version,
        kind=kind,
        name=name,
        summary=summary,
        channels=frozenset(channels),
        latency=latency,
        model_tier=tier,
        hooks=frozenset(hooks),
        tool=dict(tool) if isinstance(tool, Mapping) else None,
        triggers=frozenset(triggers),
        settings=_parse_settings(raw.get("settings"), problems),
        prompt_sections=_parse_sections(raw.get("prompt_sections"), problems),
        memory_reads=_parse_memory(raw.get("memory"), "reads", problems),
        memory_writes=memory_writes,
        memory_session=frozenset(session),
        outcomes=frozenset(outcomes),
        assets=frozenset(assets),
        requires=tuple(requires),
        conflicts=frozenset(conflicts),
        signals=frozenset(_strings(raw.get("signals"), "signals", problems)),
        markers=tuple(_strings(raw.get("markers"), "markers", problems)),
        data_classes=frozenset(data_classes),
        permissions=frozenset(permissions),
        raw=dict(raw),
    )
    if problems:
        raise ManifestError(problems)
    return manifest
