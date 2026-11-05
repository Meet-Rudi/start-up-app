"""
MEET_RUDI platform — the Training Arena's door into the platform engine.

The same contract as today's `meetrudi-engine-turn` (services/whatsapp/src/engine_turn.py), so
the arena's case runner can switch doors without changing, plus one field: `target`, the clone
version a batch runs against. Stateless: the caller hands in the state and vault it got back from
the previous turn and stores what comes back; nothing is written anywhere.

Event:
    op           open | turn | scan | info
    target       "<tenant>/<clone>@live" or "<tenant>/<clone>@<version number>"
                 (default: the door's default target, CM's live version)
    channel      a channel the clone serves (default "whatsapp", which is what the legacy door's
                 "chat" ran: the WhatsApp prompts). "voice" runs a call: `open` is the model's
                 greeting as the person picks up, and both ops take
    call         the call config as the dispatcher stores it (user_name, topic, notes,
                 language, timezone, call_goal, start_phase, max_minutes) and
    elapsed_s    seconds since the call was answered
    text, state (flat form), vault, locale, tz, now (ISO), personality (persona key), trace
    cascade      the arena's own model cascade: every entry must point at a provider the platform
                 already uses and keep its key under the arena's secret prefix, so an arena turn
                 can never spend live quota (the legacy door's rule, unchanged)

Response (as the legacy door): ok, reply, state, vault, info {phase_before, phase, signals, model,
lang, leak_blocked, pii_redacted, ended}, usage {model, tokens_in, tokens_out, estimated}, engine
{release, clone_version, version, pins}, ms. A turn stopped by an inbound gate answers ok with
`info.halted` = {kind, evidence, source}. Errors: bad_request, bad_target, bad_cascade,
rate_limited, engine_failed — never a stack trace.

Logs carry opaque ids, operations and timings only, never message text (CLAUDE.md §5).
"""

from __future__ import annotations

import datetime
import logging
import re
import time
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..gateway.core import ModelGateway
from ..gateway.endpoints import KINDS, ModelEndpoint
from ..gateway.transport import AIError, AllRateLimited
from ..memory.state import from_flat, to_flat
from ..privacy.deid import AliasVault, redact
from ..registry.models import PLATFORM
from ..registry.store import NotFound, RegistryStore
from . import voice
from .clone import AssetStore, CloneLoadError, LoadedClone, load_clone
from .skill import SkillRuntime
from .turn import TurnEngine, TurnOutcome

log = logging.getLogger(__name__)

MAX_TEXT = 4000
MAX_SCAN = 1000
TARGET_RE = re.compile(r"^(?P<name>[a-z0-9-]+/[a-z0-9-]+)@(?P<which>live|\d+)$")


class BadCascade(ValueError):
    pass


class BadTarget(ValueError):
    pass


@dataclass
class DoorDeps:
    registry: RegistryStore
    runtime: SkillRuntime
    assets: AssetStore
    gateway: ModelGateway
    clones: Mapping[str, str]  # "tenant/clone" → cln_ id
    allowed_hosts: frozenset[str] = field(default_factory=frozenset)
    secret_prefix: str = "meetrudi/arena/rudi-"
    release: str = "unknown"
    default_target: str = "cm/rudi@live"


def _host(url: object) -> str | None:
    p = urllib.parse.urlparse(str(url or ""))
    return p.hostname if p.scheme == "https" and p.hostname else None


def check_cascade(
    cascade: object, allowed: frozenset[str], prefix: str
) -> list[ModelEndpoint]:
    """The arena's cascade as endpoints, or BadCascade."""
    if not isinstance(cascade, list) or not cascade:
        raise BadCascade("an arena turn needs a non-empty cascade")
    out: list[ModelEndpoint] = []
    for i, e in enumerate(cascade):
        if not isinstance(e, Mapping):
            raise BadCascade(f"entry {i} is not an object")
        host = _host(e.get("endpoint") or e.get("url"))
        if host is None or host not in allowed:
            raise BadCascade(f"entry {i}: host {host!r} is not one the platform uses")
        if not str(e.get("secret") or "").startswith(prefix):
            raise BadCascade(f"entry {i}: key must live under {prefix}")
        if (
            (e.get("kind") or "openai_compatible") not in KINDS
            or not e.get("model")
            or not e.get("name")
        ):
            raise BadCascade(f"entry {i}: needs name, model and a supported kind")
        try:
            endpoint = ModelEndpoint.from_config(e)
        except (TypeError, ValueError) as err:
            raise BadCascade(f"entry {i}: {err}") from None
        if endpoint.enabled:
            out.append(endpoint)
    if not out:
        raise BadCascade("every entry is disabled")
    return out


def _now(value: object) -> datetime.datetime | None:
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(value))  # 3.11+ reads a trailing "Z"
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.UTC)


class Door:
    def __init__(self, deps: DoorDeps) -> None:
        self.deps = deps
        self._loaded: dict[
            str, LoadedClone
        ] = {}  # versions are immutable: cache forever

    # ------------------------------------------------------------------------------ routing
    def clone(self, target: str) -> LoadedClone:
        m = TARGET_RE.match(target or "")
        if not m or m.group("name") not in self.deps.clones:
            raise BadTarget(f"unknown target {target!r}")
        clone_id = self.deps.clones[m.group("name")]
        reg = self.deps.registry
        try:
            if m.group("which") == "live":
                version_id = reg.get_clone(PLATFORM, clone_id).live_version_id
            else:
                number = int(m.group("which"))
                version_id = next(
                    (
                        v.id
                        for v in reg.list_versions(PLATFORM, clone_id)
                        if v.number == number
                    ),
                    None,
                )
        except NotFound:
            version_id = None
        if not version_id:
            raise BadTarget(f"{target} has no such version")
        if version_id not in self._loaded:
            try:
                self._loaded[version_id] = load_clone(
                    reg, PLATFORM, version_id, self.deps.runtime, self.deps.assets
                )
            except CloneLoadError as e:
                raise BadTarget(
                    f"{target} cannot run: {'; '.join(e.problems)}"
                ) from None
        return self._loaded[version_id]

    # ---------------------------------------------------------------------------- the door
    def handle(self, event: object) -> dict[str, Any]:
        event = event if isinstance(event, dict) else {}
        op = event.get("op")
        trace = event.get("trace") or {}
        started = time.monotonic()
        try:
            if op in ("open", "turn", "info"):
                clone = self.clone(str(event.get("target") or self.deps.default_target))
                channel = str(event.get("channel") or "whatsapp")
                if op != "info" and channel not in clone.composed.per_channel:
                    out = _bad("bad_request", f"the clone does not serve {channel!r}")
                elif op == "open":
                    out = self._open(clone, channel, event)
                elif op == "turn":
                    out = self._turn(clone, channel, event)
                else:
                    out = self._info(clone)
            elif op == "scan":
                out = self._scan(event)
            else:
                out = _bad("bad_request", "unknown op")
        except BadTarget as e:
            out = _bad("bad_target", str(e))
        except Exception as e:  # noqa: BLE001 - the arena must get an answer, never a stack trace
            log.error(
                "engine door op=%s case=%s failed: %s",
                op,
                trace.get("case"),
                type(e).__name__,
            )
            out = _bad("engine_failed", type(e).__name__)
        out.setdefault("ms", int((time.monotonic() - started) * 1000))
        log.info(
            "ENGINE-DOOR op=%s case=%s turn=%s ok=%s err=%s ms=%s",
            op, trace.get("case"), trace.get("turn"), out.get("ok"), out.get("error"), out["ms"],
        )  # fmt: skip
        return out

    def _engine_info(self, clone: LoadedClone) -> dict[str, Any]:
        return {
            "release": self.deps.release,
            "clone_version": clone.version_id,
            "version": clone.version_number,
            "pins": clone.composed.pins(),
        }

    def _response(
        self, clone: LoadedClone, channel: str, o: TurnOutcome
    ) -> dict[str, Any]:
        gen = o.generation
        info: dict[str, Any] = {
            "phase_before": o.stage_before,
            "phase": o.stage,
            "signals": o.signals,
            "model": gen.model if gen else "none",
            "lang": o.lang,
            "leak_blocked": o.leak_blocked,
            "pii_redacted": o.found,
            "ended": o.ended,
        }
        if o.halt:
            info["halted"] = o.halt
        return {
            "ok": True,
            "reply": o.reply,
            "state": to_flat(o.state, clone.memory_schema(channel)),
            "vault": o.vault.to_dict(),
            "info": info,
            "usage": {
                "model": gen.model if gen else "none",
                "tokens_in": gen.usage.tokens_in if gen else 0,
                "tokens_out": gen.usage.tokens_out if gen else 0,
                "estimated": False,
            },
            "engine": self._engine_info(clone),
        }

    def _engine(self, clone: LoadedClone, event: dict) -> TurnEngine:
        cascade = check_cascade(
            event.get("cascade"), self.deps.allowed_hosts, self.deps.secret_prefix
        )
        return TurnEngine(clone, self.deps.gateway, cascade)

    def _open(self, clone: LoadedClone, channel: str, event: dict) -> dict[str, Any]:
        started = time.monotonic()
        persona = str(event.get("personality") or "")
        try:
            if (
                channel == voice.CHANNEL
            ):  # the model greets: a call has just been answered
                engine = self._engine(clone, event)
                o = voice.open_call(
                    engine,
                    _call(event),
                    AliasVault.from_dict(event.get("vault") or None),
                    now=_now(event.get("now")),
                    persona_key=persona,
                )
            else:
                o = TurnEngine(clone, self.deps.gateway, []).open(
                    channel=channel, now=_now(event.get("now")), persona_key=persona
                )
        except BadCascade as e:
            return _bad("bad_cascade", str(e))
        except AllRateLimited as e:
            return _bad("rate_limited", str(e)[:300])
        except AIError as e:
            return _bad("engine_failed", str(e)[:300])
        return {
            **self._response(clone, channel, o),
            "ms": int((time.monotonic() - started) * 1000),
        }

    def _turn(self, clone: LoadedClone, channel: str, event: dict) -> dict[str, Any]:
        text = str(event.get("text") or "")[:MAX_TEXT]
        if not text.strip():
            return _bad("bad_request", "empty text")
        state = from_flat(event.get("state") or {}, clone.memory_schema(channel))
        vault = AliasVault.from_dict(event.get("vault") or None)
        persona = str(event.get("personality") or "")
        started = time.monotonic()
        try:
            engine = self._engine(clone, event)
            if channel == voice.CHANNEL:
                o = voice.call_turn(
                    engine,
                    state,
                    text,
                    _call(event),
                    vault,
                    elapsed_s=_int(event.get("elapsed_s")),
                    now=_now(event.get("now")),
                    persona_key=persona,
                )
            else:
                o = engine.turn(
                    state,
                    text,
                    vault=vault,
                    channel=channel,
                    locale=str(event.get("locale") or "en"),
                    tz=str(event.get("tz") or ""),
                    now=_now(event.get("now")),
                    persona_key=persona,
                    redacted="[{} removed]",
                )
        except BadCascade as e:
            return _bad("bad_cascade", str(e))
        except AllRateLimited as e:
            return _bad("rate_limited", str(e)[:300])
        except AIError as e:
            return _bad("engine_failed", str(e)[:300])
        return {
            **self._response(clone, channel, o),
            "ms": int((time.monotonic() - started) * 1000),
        }

    def _scan(self, event: dict) -> dict[str, Any]:
        texts = event.get("texts") or []
        if not isinstance(texts, list) or len(texts) > MAX_SCAN:
            return _bad("bad_request", f"texts must be a list of at most {MAX_SCAN}")
        return {"ok": True, "found": [redact(str(t or ""))[1] for t in texts]}

    def _info(self, clone: LoadedClone) -> dict[str, Any]:
        return {
            "ok": True,
            "release": self.deps.release,
            "default_personality": clone.default_persona,
            "personalities": sorted(clone.personas),
            "engine": self._engine_info(clone),
        }


def _bad(error: str, detail: str) -> dict[str, Any]:
    return {"ok": False, "error": error, "detail": detail}


def _call(event: Mapping[str, Any]) -> dict[str, Any]:
    call = event.get("call")
    return dict(call) if isinstance(call, Mapping) else {}


def _int(value: object) -> int:
    try:
        return max(0, int(value))  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0
