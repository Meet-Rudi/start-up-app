"""
MEET_RUDI platform — the journey: a clone version's stage graph (blueprint §2; catalogue
`conversation-phases` and `session-opening`).

    {
      "format": "rudi-journey@1",
      "start": "learn",
      "greet": {"first": "intro", "again": "welcome_back"},
      "stages": {
        "learn":     {"skill": "learn-about-rudi",
                      "on": {"wants_to_try": {"to": "goal", "clear_history": true}}},
        "goal":      {"skill": "goal-setting",
                      "on": {"accepted": "commit", "gave_up": "concluded"}},
        "commit":    {"skill": "commitment-securing",
                      "on": {"committed": "committed", "gave_up": "concluded"}},
        "committed": {"skill": "check-in-follow-up", "on": {"goal_changed": "commit"}}
      }
    }

How the engine walks it:

  - A conversation that has never spoken is greeted with the `greet.first` string (no model call)
    and opens its first session at `start`. One whose session has `concluded` is greeted with
    `greet.again` and opens a new session at `start`: session-scoped memory (manifest
    `memory.session`) is cleared, the rest of the conversation's memory carries over.
  - In a stage, that stage's skill contributes the stage prompt; stage skills of other stages stay
    silent. After the reply its `on_signals` hook reports one of its manifest `outcomes`; `on` maps
    the outcome to the next stage, or to `concluded`, which ends the session. An outcome the stage
    does not map keeps the conversation where it is.
  - `clear_history` starts the next stage with an empty model history (a fresh model session).
  - Entering a stage runs its skill's `on_enter` hook (counters reset there).
  - `channels` holds per-channel variants: another `start`, and transitions that replace the
    base ones for that channel. CM's calls end when the person commits, where a WhatsApp thread
    moves on to the check-in loop:
        "channels": {"voice": {"start": "goal",
                               "on": {"commit": {"committed": "concluded"}}}}
    A call can also name its own start stage (a get-to-know call starts in `learn`).
  - A stage must be reachable on some channel, and its skill must serve every channel on which
    it can be reached.

The stages and their transitions are configuration: a clone designer reorders or drops stages in
the console. What a stage does, and which outcomes it can report, is the skill's code.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .i18n import SPECS
from .skills.composer import ComposedClone
from .skills.contract import SkillKind
from .skills.manifest import CHANNELS

FORMAT = "rudi-journey@1"
CONCLUDED = "concluded"  # not a stage: the session is over
STAGE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
GREETING_PLACEHOLDERS = frozenset({"persona"})


class JourneyError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Transition:
    to: str
    clear_history: bool = False


@dataclass(frozen=True)
class Stage:
    key: str
    skill: str
    on: Mapping[str, Transition] = field(default_factory=dict)


@dataclass(frozen=True)
class Variant:
    """How the journey differs on one channel: another start, other transitions."""

    start: str | None = None
    on: Mapping[str, Mapping[str, Transition]] = field(default_factory=dict)


@dataclass(frozen=True)
class Journey:
    start: str
    stages: Mapping[str, Stage]
    greet_first: str = "intro"
    greet_again: str = "welcome_back"
    variants: Mapping[str, Variant] = field(default_factory=dict)

    def for_channel(self, channel: str) -> Journey:
        """The journey as it runs on `channel` (the base journey when it has no variant)."""
        v = self.variants.get(channel)
        if v is None:
            return self
        stages = {
            key: Stage(key, s.skill, {**s.on, **v.on.get(key, {})})
            for key, s in self.stages.items()
        }
        return Journey(
            v.start or self.start, stages, self.greet_first, self.greet_again
        )

    def stage(self, key: str) -> Stage:
        try:
            return self.stages[key]
        except KeyError:
            raise JourneyError([f"no stage {key!r} in this journey"]) from None

    def next(self, stage_key: str, outcome: str | None) -> Transition | None:
        """Where `outcome` leads from `stage_key`, or None to stay."""
        if not outcome:
            return None
        return self.stage(stage_key).on.get(outcome)

    @property
    def stage_skills(self) -> frozenset[str]:
        return frozenset(s.skill for s in self.stages.values())


def _transition(value: Any, where: str, problems: list[str]) -> Transition | None:
    if isinstance(value, str):
        return Transition(value)
    if isinstance(value, Mapping) and isinstance(value.get("to"), str):
        clear = value.get("clear_history", False)
        if not isinstance(clear, bool):
            problems.append(f"{where}.clear_history must be true or false")
            return None
        unknown = sorted(set(value) - {"to", "clear_history"})
        if unknown:
            problems.append(f"{where}: unknown keys {unknown}")
            return None
        return Transition(value["to"], clear)
    problems.append(
        f"{where} must be a stage name or {{'to': ..., 'clear_history': ...}}"
    )
    return None


def parse_journey(raw: Mapping[str, Any]) -> Journey:
    """A structurally valid journey, or JourneyError listing every problem found."""
    if not isinstance(raw, Mapping):
        raise JourneyError(["a journey must be an object"])
    problems: list[str] = []
    if raw.get("format") != FORMAT:
        problems.append(f"format must be {FORMAT!r}")
    stages_raw = raw.get("stages")
    stages: dict[str, Stage] = {}
    if not isinstance(stages_raw, Mapping) or not stages_raw:
        problems.append("stages must be a non-empty object")
        stages_raw = {}
    for key, spec in stages_raw.items():
        where = f"stages.{key}"
        if not isinstance(key, str) or not STAGE_RE.match(key) or key == CONCLUDED:
            problems.append(
                f"{where}: stage names are snake_case and not {CONCLUDED!r}"
            )
            continue
        if not isinstance(spec, Mapping) or not isinstance(spec.get("skill"), str):
            problems.append(f"{where}: needs the slug of the skill that serves it")
            continue
        on_raw = spec.get("on") or {}
        if not isinstance(on_raw, Mapping):
            problems.append(f"{where}.on must map outcomes to stages")
            continue
        on: dict[str, Transition] = {}
        for outcome, target in on_raw.items():
            t = _transition(target, f"{where}.on.{outcome}", problems)
            if t is not None:
                on[str(outcome)] = t
        stages[key] = Stage(key, spec["skill"], on)

    for stage in stages.values():
        for outcome, t in stage.on.items():
            if t.to != CONCLUDED and t.to not in stages:
                problems.append(f"stages.{stage.key}.on.{outcome}: no stage {t.to!r}")

    start = raw.get("start")
    if not isinstance(start, str) or start not in stages:
        problems.append("start must name one of the stages")

    variants: dict[str, Variant] = {}
    variants_raw = raw.get("channels") or {}
    if not isinstance(variants_raw, Mapping):
        problems.append("channels must map a channel to its variant")
        variants_raw = {}
    for channel, spec in variants_raw.items():
        where = f"channels.{channel}"
        if channel not in CHANNELS or not isinstance(spec, Mapping):
            problems.append(f"{where}: needs a known channel and an object")
            continue
        unknown = sorted(set(spec) - {"start", "on"})
        if unknown:
            problems.append(f"{where}: unknown keys {unknown}")
        v_start = spec.get("start")
        if v_start is not None and v_start not in stages:
            problems.append(f"{where}.start must name one of the stages")
        v_on: dict[str, dict[str, Transition]] = {}
        on_raw = spec.get("on") or {}
        if not isinstance(on_raw, Mapping):
            problems.append(f"{where}.on must map stages to their outcomes")
            on_raw = {}
        for key, outcomes in on_raw.items():
            if key not in stages or not isinstance(outcomes, Mapping):
                problems.append(f"{where}.on.{key}: no such stage")
                continue
            for outcome, target in outcomes.items():
                t = _transition(target, f"{where}.on.{key}.{outcome}", problems)
                if t is None:
                    continue
                if t.to != CONCLUDED and t.to not in stages:
                    problems.append(f"{where}.on.{key}.{outcome}: no stage {t.to!r}")
                    continue
                v_on.setdefault(key, {})[str(outcome)] = t
        variants[str(channel)] = Variant(
            v_start if isinstance(v_start, str) else None, v_on
        )
    greet = raw.get("greet") or {}
    if not isinstance(greet, Mapping):
        problems.append("greet must be an object")
        greet = {}
    first, again = greet.get("first", "intro"), greet.get("again", "welcome_back")
    for name, key in (("first", first), ("again", again)):
        spec = SPECS.get(key) if isinstance(key, str) else None
        if spec is None or not spec.placeholders <= GREETING_PLACEHOLDERS:
            problems.append(
                f"greet.{name} must be a string key that needs at most {{persona}}"
            )

    if problems:
        raise JourneyError(problems)
    journey = Journey(str(start), stages, str(first), str(again), variants)
    reached: set[str] = set()
    for view in (journey, *(journey.for_channel(c) for c in variants)):
        reached |= _reachable(view)
    for key in sorted(set(stages) - reached):
        problems.append(f"stage {key!r} cannot be reached on any channel")
    if problems:
        raise JourneyError(problems)
    return journey


def _reachable(journey: Journey) -> set[str]:
    reached, todo = {journey.start}, [journey.start]
    while todo:
        for t in journey.stages[todo.pop()].on.values():
            if t.to in journey.stages and t.to not in reached:
                reached.add(t.to)
                todo.append(t.to)
    return reached


def check_journey(journey: Journey, composed: ComposedClone) -> list[str]:
    """Problems with running `journey` on this skill mix (empty when it fits)."""
    problems: list[str] = []
    needs: dict[str, set[str]] = {}  # stage → the channels on which it can be reached
    for channel in composed.per_channel:
        for key in _reachable(journey.for_channel(channel)):
            needs.setdefault(key, set()).add(channel)
    served: dict[str, str] = {}
    for stage in journey.stages.values():
        where = f"stage {stage.key!r}"
        if stage.skill in served:
            problems.append(
                f"{where}: {stage.skill} already serves stage {served[stage.skill]!r}"
            )
            continue
        served[stage.skill] = stage.key
        resolved = composed.skill(stage.skill)
        if resolved is None:
            problems.append(f"{where}: {stage.skill} is not in the clone")
            continue
        manifest = resolved.manifest
        if (
            manifest.kind is not SkillKind.REAL_TIME
            or "contribute" not in manifest.hooks
        ):
            problems.append(f"{where}: {stage.skill} is not a real-time stage skill")
        missing = sorted(needs.get(stage.key, set()) - set(resolved.channels))
        if missing:
            problems.append(
                f"{where}: {stage.skill} does not serve {', '.join(missing)}"
            )
        outcomes = set(stage.on)
        for variant in journey.variants.values():
            outcomes |= set(variant.on.get(stage.key, {}))
        for outcome in sorted(outcomes):
            if outcome not in manifest.outcomes:
                problems.append(
                    f"{where}: {stage.skill} never reports {outcome!r} "
                    f"(it reports {sorted(manifest.outcomes)})"
                )
    return problems


__all__ = [
    "CONCLUDED",
    "FORMAT",
    "Journey",
    "JourneyError",
    "Stage",
    "Transition",
    "Variant",
    "check_journey",
    "parse_journey",
]
