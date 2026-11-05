"""
MEET_RUDI platform — the Turn Engine: one inbound message through one clone version.

It is today's `webchat.chat_turn` + `responder.respond` (services/whatsapp) and the call brain
(services/call/src/brain.py) with everything CM-specific moved into skills, clone configuration
and channel profiles. One turn, in order:

    de-identify the inbound text (Tier 1 redaction, Tier 2 aliases; persona names kept)
    inbound gates          on_inbound — a halt (e.g. the objection gate) ends the turn here
    opening                never spoken → `greet.first`; session concluded → `greet.again` and a
                           new session (no model call). On a channel that does not restart (a
                           call), a concluded conversation is simply over.
    prompt                 the active stage's skill and every non-stage skill contribute parts;
                           the channel adds its brief; render_system orders them
    model                  one JSON-mode generation through the ModelGateway, within the
                           channel's time budget
    reply gates            on_reply — a blocked reply is replaced and its signals dropped
    signals                the stage skill's on_signals reports an outcome → the journey (this
                           channel's variant) moves, on_enter of the next stage runs; then every
                           other skill's on_signals (any may conclude the session: a call that
                           is out of time)
    delivery form          channel style (WhatsApp bold, speakable text), names restored

`open()` is the conversation's first move before anyone has spoken: the clone's canned greeting,
or, on a channel with an opening cue (a call), a model-written greeting from the start stage.

De-identification happens at the conversation boundary, as in today's channels: the inbound text
is scrubbed before anything stores or sends it, the stored history and reply stay de-identified,
and names come back only in `reply`, the text the person reads. The gateway is therefore called
without a vault: its restore step would put real names into the stored reply.

What a hook receives:
  `request.context`   the turn context the channel adapter supplies (a call: the model-safe call
                      brief and the elapsed time), or None
  `request.input`     hook, stage, memory (before this turn's writes), locale, tz, now (UTC ISO),
                      channel, opening (true only for a model-written opening), persona {key,
                      name, ocean, briefing}, text (de-identified inbound), assets {name: text},
                      prompt_overrides (this skill's, from the clone version)
    on_reply adds:    reply, system, lang, markers, strings {catalogue key: text in lang}
    on_signals adds:  reply, signals
    on_enter adds:    from_stage
A hook's `memory_writes` may only name fields its manifest writes; a None value clears the field.
On_signals output `{"conclude": true}` ends the session after this turn.

Errors from the gateway (AllRateLimited, AIError) propagate: the channel decides what to say.
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .. import timeline
from ..gateway.core import Generation, ModelGateway, UsageTags
from ..gateway.endpoints import ModelEndpoint
from ..gateway.envelope import parse_envelope
from ..i18n import SPECS, normalize_locale
from ..journey import CONCLUDED, Journey
from ..memory.state import ConversationState
from ..privacy.deid import (
    AliasVault,
    HeuristicDetector,
    restore_outbound,
    scrub_inbound,
)
from ..registry.models import Persona
from ..skills.contract import SkillRef, SkillRequest, SkillResult
from .channels import ChannelProfile, profile
from .clone import BoundSkill, LoadedClone
from .prompt import PromptPart, fill, render_system

MAX_INPUT_CHARS = 4000


class EngineError(Exception):
    """A skill broke the engine's contract (unknown memory field, malformed result)."""


@dataclass
class TurnOutcome:
    reply: str  # what the person reads or hears: channel style applied, names restored
    reply_masked: str  # what is stored: channel style applied, de-identified
    state: ConversationState
    vault: AliasVault
    masked_inbound: str = ""
    found: dict[str, int] = field(default_factory=dict)  # Tier-1 identifiers removed
    stage_before: str | None = None
    stage: str | None = None
    signals: dict[str, Any] = field(default_factory=dict)
    lang: str | None = None  # the language the model detected this turn
    leak_blocked: bool = False
    # Diagnostics, safe to log: why a reply gate blocked ("marker", "verbatim") and the model
    # output's envelope shape (Envelope.shape).
    leak_reason: str = ""
    envelope: str = ""
    greeted: bool = False
    new_contact: bool = False
    halt: dict[str, Any] | None = None  # set when an inbound gate stopped the turn
    generation: Generation | None = None
    # The system prompt sent (for the arena and parity tests; never logged).
    system: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ended(self) -> bool:
        """The session is over after this turn (on a call: hang up after the reply)."""
        return self.stage == CONCLUDED


class TurnEngine:
    def __init__(
        self,
        clone: LoadedClone,
        gateway: ModelGateway,
        cascade: Sequence[ModelEndpoint],
    ) -> None:
        self.clone = clone
        self.gateway = gateway
        self.cascade = list(cascade)
        self.detector = HeuristicDetector(keep=clone.persona_names)

    # ---------------------------------------------------------------------------- public api
    def open(
        self,
        *,
        channel: str = "whatsapp",
        now: datetime.datetime | None = None,
        persona_key: str = "",
        redacted: str = "",
        vault: AliasVault | None = None,
        locale: str | None = None,
        tz: str = "",
        context: Mapping[str, Any] | None = None,
        start_stage: str | None = None,
        end_user: str | None = None,
    ) -> TurnOutcome:
        """The conversation's first move, before anyone has spoken."""
        now = now or timeline.now_utc()
        turn = _Turn(
            self,
            channel,
            self.clone.persona(persona_key),
            "",
            locale or self.clone.default_locale,
            tz,
            now,
            end_user,
            context,
        )
        vault = vault if vault is not None else AliasVault()
        if turn.profile.opening_cue is not None:
            return turn.model_opening(vault, start_stage, redacted)
        return turn.greet(ConversationState(), vault, first=True, redacted=redacted)

    def turn(
        self,
        state: ConversationState,
        text: str,
        *,
        vault: AliasVault,
        channel: str = "whatsapp",
        locale: str = "en",
        tz: str = "",
        now: datetime.datetime | None = None,
        persona_key: str = "",
        redacted: str = "",
        end_user: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> TurnOutcome:
        now = now or timeline.now_utc()
        persona = self.clone.persona(persona_key)
        masked, found = scrub_inbound(vault, text, self.detector, locale)
        turn = _Turn(self, channel, persona, masked, locale, tz, now, end_user, context)
        outcome = turn.run(state.copy(), vault, redacted)
        outcome.masked_inbound, outcome.found = masked, found
        return outcome


class _Turn:
    """The working set of one turn."""

    def __init__(
        self,
        engine: TurnEngine,
        channel: str,
        persona: Persona,
        masked: str,
        locale: str,
        tz: str,
        now: datetime.datetime,
        end_user: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        self.e = engine
        self.c = engine.clone
        self.channel = channel
        self.profile: ChannelProfile = profile(channel)
        self.journey: Journey = self.c.journey.for_channel(channel)
        self.persona = persona
        self.masked = masked
        self.locale = normalize_locale(locale) or self.c.default_locale
        self.tz = tz
        self.now = now
        self.end_user = end_user
        self.context = dict(context) if context is not None else None
        self.here = [
            b for b in self.c.skills if b.impl and channel in b.resolved.channels
        ]
        self.assets = self.c.assets_for(channel)
        # The model's `lang` signal counts only where a skill asks for it (language mirroring);
        # elsewhere the conversation's language stands (a call is in the language it was placed in).
        self.lang_signal = any("lang" in b.resolved.manifest.signals for b in self.here)

    # ---------------------------------------------------------------------------- the turn
    def run(
        self, state: ConversationState, vault: AliasVault, redacted: str
    ) -> TurnOutcome:
        base = self._base(state)
        for b in self.here:
            if b.has("on_inbound"):
                halt = self._call(b, "on_inbound", base).output.get("halt")
                if halt:
                    return self._halted(state, vault, dict(halt), redacted)

        if state.is_new:
            return self.greet(state, vault, first=True, redacted=redacted)
        if state.stage == CONCLUDED or state.stage not in self.journey.stages:
            if not self.profile.restart_after_conclusion:
                return self._over(state, vault)
            # A concluded session, or a stage a newer clone version no longer has: start over
            # politely rather than fail every turn of that conversation.
            return self.greet(state, vault, first=False, redacted=redacted)

        journey = self.journey
        stage = journey.stage(state.stage)
        stage_skill = self.c.skill(stage.skill)
        history = [
            *state.history,
            {
                "role": "user",
                "content": self.masked[:MAX_INPUT_CHARS],
                "at": self._stamp,
            },
        ]
        system = self._system(base, stage.key)
        messages = [{"role": "system", "content": system}] + timeline.render(
            history[-self.profile.max_history :], self.now, self.tz
        )
        generation = self._generate(messages, stage_skill, json_mode=True)
        envelope = parse_envelope(generation.text)
        reply = timeline.strip_markers(envelope.reply)
        signals = dict(envelope.signals)

        reply, reason = self._gate(base, reply, system, signals)
        blocked = bool(reason)
        if blocked:  # text we refused to send must not move the conversation along
            keep = self.lang_signal and signals.get("lang")
            signals = {"lang": signals["lang"]} if keep else {}

        styled = self.profile.style(reply)
        history.append(
            {
                "role": "assistant",
                "content": styled if self.profile.store_styled else reply,
                "at": self._stamp,
            }
        )
        new = ConversationState(
            stage=state.stage,
            session=state.session,
            history=history,
            memory=dict(state.memory),
            extra=dict(state.extra),
        )
        signalled = {**base, "reply": reply, "signals": signals}
        outcome_name = None
        if stage_skill is not None and stage_skill.has("on_signals"):
            result = self._call(stage_skill, "on_signals", signalled)
            self._apply(stage_skill, result, new)
            outcome_name = result.output.get("outcome")
        transition = journey.next(stage.key, outcome_name)
        if transition is not None:
            if transition.clear_history:
                new.history = []
            new.stage = transition.to
            if transition.to != CONCLUDED:
                self._enter(new, stage.key)
        detected, conclude = None, False
        for b in self.here:
            if (
                b is stage_skill
                or b.slug in journey.stage_skills
                or not b.has("on_signals")
            ):
                continue
            result = self._call(b, "on_signals", signalled)
            self._apply(b, result, new)
            detected = normalize_locale(result.output.get("locale")) or detected
            conclude = conclude or bool(result.output.get("conclude"))
        if conclude:
            new.stage = CONCLUDED

        return TurnOutcome(
            reply=restore_outbound(vault, styled, redacted=redacted),
            reply_masked=styled,
            state=new,
            vault=vault,
            stage_before=state.stage,
            stage=new.stage,
            signals=signals,
            lang=detected,
            leak_blocked=blocked,
            leak_reason=reason,
            envelope=envelope.shape,
            generation=generation,
            system=system,
            messages=messages,
        )

    def greet(
        self, state: ConversationState, vault: AliasVault, *, first: bool, redacted: str
    ) -> TurnOutcome:
        journey = self.journey
        key = journey.greet_first if first else journey.greet_again
        # A first greeting is in the clone's default language: nothing is known about the person.
        locale = self.c.default_locale if first else self.locale
        greeting = self.c.catalogue.text(key, locale, persona=self.persona.name)
        keep = {
            k: v
            for k, v in state.memory.items()
            if k not in self.c.composed.session_memory
        }
        new = ConversationState(
            stage=journey.start,
            session=state.session + 1,
            history=[{"role": "assistant", "content": greeting, "at": self._stamp}],
            memory={} if first else keep,
        )
        self._enter(new, None)
        reply_masked = self.profile.style(greeting)
        return TurnOutcome(
            reply=restore_outbound(vault, reply_masked, redacted=redacted),
            reply_masked=reply_masked,
            state=new,
            vault=vault,
            stage_before=state.stage,
            stage=new.stage,
            greeted=True,
            new_contact=first,
        )

    def model_opening(
        self, vault: AliasVault, start_stage: str | None, redacted: str
    ) -> TurnOutcome:
        """The model greets first (a call: they have just picked up). No signals are read."""
        start = (
            start_stage if start_stage in self.journey.stages else self.journey.start
        )
        new = ConversationState(stage=start, session=1)
        self._enter(new, None)
        base = self._base(new, opening=True)
        system = self._system(base, start)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": str(self.profile.opening_cue)},
        ]
        stage_skill = self.c.skill(self.journey.stage(start).skill)
        generation = self._generate(messages, stage_skill, json_mode=False)
        envelope = parse_envelope(generation.text)
        reply = timeline.strip_markers(envelope.reply)
        reply, reason = self._gate(base, reply, system, {})
        styled = self.profile.style(reply)
        new.history = [
            {
                "role": "assistant",
                "content": styled if self.profile.store_styled else reply,
                "at": self._stamp,
            }
        ]
        return TurnOutcome(
            reply=restore_outbound(vault, styled, redacted=redacted),
            reply_masked=styled,
            state=new,
            vault=vault,
            stage=start,
            leak_blocked=bool(reason),
            leak_reason=reason,
            # Plain text is asked for here, so only a JSON answer has a shape worth noting.
            envelope=envelope.shape if envelope.parsed else "",
            greeted=True,
            new_contact=True,
            generation=generation,
            system=system,
            messages=messages,
        )

    # ------------------------------------------------------------------------------ helpers
    @property
    def _stamp(self) -> str:
        return timeline.stamp(self.now)

    def _base(self, state: ConversationState, opening: bool = False) -> dict[str, Any]:
        p = self.persona
        return {
            "stage": state.stage,
            "memory": dict(state.memory),
            "locale": self.locale,
            "tz": self.tz,
            "now": self._stamp,
            "channel": self.channel,
            "opening": opening,
            "persona": {
                "key": p.key,
                "name": p.name,
                "ocean": dict(p.ocean),
                "briefing": p.briefing,
            },
            "text": self.masked,
            "assets": self.assets,
        }

    def _system(self, base: Mapping[str, Any], stage_key: str) -> str:
        silent = self.journey.stage_skills - {self.journey.stage(stage_key).skill}
        parts: list[PromptPart] = []
        for b in self.here:
            if b.has("contribute") and b.slug not in silent:
                result = self._call(b, "contribute", base)
                for raw in result.output.get("prompt") or []:
                    parts.append(PromptPart.from_dict(raw, b.ref))
        if self.profile.brief:
            parts.append(
                PromptPart(
                    "channel", fill(self.profile.brief, {"persona": self.persona.name})
                )
            )
        return render_system(parts, self.persona.name)

    def _generate(
        self,
        messages: list[dict[str, Any]],
        stage_skill: BoundSkill | None,
        json_mode: bool,
    ) -> Generation:
        return self.e.gateway.generate(
            messages,
            self.e.cascade,
            json_mode=json_mode,
            budget_s=self.profile.budget_s,
            attempt_timeout_s=self.profile.attempt_timeout_s,
            max_tokens=self.profile.max_tokens,
            require_reply=json_mode and self.profile.require_reply,
            tags=UsageTags(
                tenant=self.c.tenant_id,
                clone=self.c.clone_id,
                clone_version=self.c.version_id,
                skill=stage_skill.ref if stage_skill else None,
                channel=self.channel,
                end_user=self.end_user,
            ),
        )

    def _gate(
        self,
        base: Mapping[str, Any],
        reply: str,
        system: str,
        signals: Mapping[str, Any],
    ) -> tuple[str, str]:
        """(reply to send, why a gate blocked the model's reply or "")."""
        lang = (
            normalize_locale(signals.get("lang")) if self.lang_signal else None
        ) or self.locale
        reason = ""
        for b in self.here:
            if b.has("on_reply"):
                out = self._call(
                    b,
                    "on_reply",
                    {
                        **base,
                        "reply": reply,
                        "system": system,
                        "lang": lang,
                        "markers": list(self.c.composed.markers),
                        "strings": self._strings(lang),
                    },
                ).output
                if out.get("blocked"):
                    reply = str(out.get("reply") or "")
                    reason = reason or str(out["blocked"])
        return reply, reason

    def _strings(self, lang: str) -> dict[str, str]:
        return {
            key: self.c.catalogue.text(
                key, lang, persona=self.persona.name, kind="message"
            )
            for key, spec in SPECS.items()
            if spec.placeholders <= {"persona", "kind"}
        }

    def _halted(
        self,
        state: ConversationState,
        vault: AliasVault,
        halt: dict[str, Any],
        redacted: str,
    ) -> TurnOutcome:
        ack = self.c.catalogue.text(
            "frozen_ack", self.locale, persona=self.persona.name
        )
        reply_masked = self.profile.style(ack)
        return TurnOutcome(
            reply=restore_outbound(vault, reply_masked, redacted=redacted),
            reply_masked=reply_masked,
            state=state,
            vault=vault,
            stage_before=state.stage,
            stage=state.stage,
            halt=halt,
        )

    def _over(self, state: ConversationState, vault: AliasVault) -> TurnOutcome:
        """Nothing more to say: the conversation ended on a channel that does not restart."""
        return TurnOutcome(
            reply="",
            reply_masked="",
            state=state,
            vault=vault,
            stage_before=state.stage,
            stage=CONCLUDED,
        )

    def _enter(self, state: ConversationState, from_stage: str | None) -> None:
        stage = self.journey.stage(str(state.stage))
        bound = self.c.skill(stage.skill)
        if bound is not None and bound.has("on_enter"):
            req = {**self._base(state), "from_stage": from_stage}
            self._apply(bound, self._call(bound, "on_enter", req), state)

    def _call(self, b: BoundSkill, hook: str, data: Mapping[str, Any]) -> SkillResult:
        req = SkillRequest(
            skill=SkillRef.parse(b.ref),
            tenant=self.c.tenant_id,
            channel=self.channel,
            input={
                **data,
                "hook": hook,
                "prompt_overrides": dict(b.resolved.prompt_overrides),
            },
            settings=dict(b.resolved.settings),
            context=self.context,
            clone=self.c.clone_id,
            clone_version=self.c.version_id,
        )
        result = getattr(b.impl, hook)(req)
        if not isinstance(result, SkillResult):
            raise EngineError(f"{b.ref}.{hook} did not return a SkillResult")
        return result

    @staticmethod
    def _apply(b: BoundSkill, result: SkillResult, state: ConversationState) -> None:
        allowed = b.resolved.manifest.memory_writes
        for write in result.memory_writes:
            if write.field not in allowed:
                raise EngineError(
                    f"{b.ref} wrote {write.field!r}, which it does not declare"
                )
            if write.value is None:
                state.memory.pop(write.field, None)
            else:
                state.memory[write.field] = write.value
