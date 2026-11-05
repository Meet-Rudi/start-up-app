"""
MEET_RUDI — which engine answers a WhatsApp or tester-chat message (decision #40).

Two engines produce Rudi's chat replies, behind one function:

    legacy    responder.respond, as it has run since August
    platform  CM's clone on the platform Turn Engine (packages/rudi_platform, rudi_skills,
              rudi_tenants), shipped in the meetrudi-tenant-prod-pool-engine-whatsapp layer

CHAT_ENGINE picks one for the whole service (default legacy). Both read and write the same
conversation state (`meta.ai_state`, CM's flat form; test_engine_parity.py holds the two to the
same prompts, replies and state turn by turn), so the engine may differ from one message to the
next and a rollback is one setting and a redeploy.

The switch never costs a reply: if the platform cannot load, or breaks on a turn for any reason
other than the models themselves, that turn runs on legacy. Model failures (every provider
rate-limited or down) are reported as the legacy gateway's own errors, so callers say what they
always say ("tired", "error") whichever engine was asked.

This module only turns (state, de-identified text) into a reply, as responder.respond does.
Everything around it stays with the caller: de-identification on the way in, storage, the
objection gate, delivery, scheduling, profile summaries and proactive reach-out (those are
background work and stay legacy until the scheduler exists, Phase 2).

The platform is imported lazily, so the legacy path never depends on the layer. Logs carry
opaque ids, endpoint names, token counts and timings, never message content (CLAUDE.md §5).
"""

import json
import logging
import os
import time

import boto3
import gateway
import personality
import responder

LEGACY, PLATFORM = "legacy", "platform"
ENGINES = (LEGACY, PLATFORM)
CHANNEL = "whatsapp"
DATA_BUCKET = os.environ["DATA_BUCKET"]
ENDPOINTS_KEY = os.environ.get("ENDPOINTS_CONFIG_KEY", "config/ai_endpoints.json")

# The live chat gateway appends this after the configured endpoints on every turn (gateway.py
# GROQ_FALLBACK). The platform gateway never adds a hidden fallback, so it is named here: same
# cascade, in the same order, as legacy replies get.
FALLBACK = dict(gateway.GROQ_FALLBACK)

# Which model answered, and why the others did not: ids and labels only, never content.
logging.getLogger("rudi_platform.gateway").setLevel(logging.INFO)

_s3 = boto3.client("s3")
_loaded = None          # (clone, model gateway), built once per warm container


def default_engine():
    value = os.environ.get("CHAT_ENGINE", LEGACY).strip().lower()
    return value if value in ENGINES else LEGACY


def _load():
    """CM's live clone version, its skills and knowledge assets, plus the model gateway.

    The registry is in memory and seeded from the code in the layer (rudi_tenants.cm), as the
    parity tests build it: there is no PlanetScale registry yet. Assets come from the same bucket
    keys responder.py reads.
    """
    global _loaded
    if _loaded is None:
        started = time.time()
        import rudi_skills
        from rudi_platform.engine import S3AssetStore, load_clone
        from rudi_platform.gateway import (
            ModelGateway,
            SecretsManagerResolver,
            UrllibTransport,
        )
        from rudi_platform.registry import PLATFORM as CTX
        from rudi_platform.registry import InMemoryRegistryStore
        from rudi_tenants import cm

        store = InMemoryRegistryStore()
        seeded = cm.seed(store, rudi_skills.manifests())
        clone = load_clone(store, CTX, seeded.version_id, rudi_skills.runtime(),
                           S3AssetStore(_s3, DATA_BUCKET))
        # Retries on 429 as the legacy chat gateway does (2, backing off from 0.5 s): a chat
        # reply can wait a few seconds; a call cannot, which is why the call adapter sets 0.
        models = ModelGateway(UrllibTransport(),
                              SecretsManagerResolver(boto3.client("secretsmanager")))
        _loaded = (clone, models)
        ms = int((time.time() - started) * 1000)
        print(f"ENGINE platform loaded clone_version={clone.version_id} "
              f"skills={len(clone.skills)} ms={ms}")
    return _loaded


def ready():
    """Load the platform now. False (and the reason logged) when it cannot answer."""
    try:
        _load()
        return True
    except Exception as e:  # noqa: BLE001 - any load failure means "answer on legacy"
        print(f"ERROR: platform engine unavailable, chat runs on legacy: "
              f"{type(e).__name__}: {e}")
        return False


def _cascade(given=None):
    """config/ai_endpoints.json's enabled entries, then the fallback — read every turn, as the
    legacy gateway does, so an edit to the config reaches the next message.

    `given` replaces all of it, fallback included: the Training Arena's own cascade (already
    checked by engine_turn.check_cascade), so an arena turn never spends live Rudi's quota —
    the rule gateway.using() enforces for legacy turns.
    """
    from rudi_platform.gateway import ModelEndpoint

    entries = []
    if given is not None:
        entries = list(given)
    else:
        try:
            obj = _s3.get_object(Bucket=DATA_BUCKET, Key=ENDPOINTS_KEY)
            data = json.loads(obj["Body"].read().decode("utf-8"))
            entries = data.get("endpoints", []) if isinstance(data, dict) else []
        except Exception as e:  # noqa: BLE001 - missing/broken config => fallback only
            print(f"INFO: endpoints config unavailable ({type(e).__name__}); fallback only")
        entries.append(FALLBACK)
    cascade = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("enabled", True):
            continue
        try:
            cascade.append(ModelEndpoint.from_config(entry))
        except (TypeError, ValueError) as e:
            print(f"WARN: endpoint {entry.get('name')!r} skipped: {e}")
    return cascade


def _translate(error):
    """Platform model errors as the chat gateway's own, so callers handle both engines alike."""
    from rudi_platform.gateway import AllRateLimited

    if isinstance(error, AllRateLimited):
        return gateway.AllRateLimited(str(error))
    return gateway.AIError(str(error))


def _sync_vault(vault, data):
    """Aliases the platform added this turn, back into the caller's vault, so the caller's own
    restore step can put every name back. The two vaults share one dict format."""
    if vault is not None and isinstance(data, dict):
        vault.mapping.update(data.get("mapping") or {})
        vault.roles.update(data.get("roles") or {})


def _on_platform(ai_state, masked, locale, persona, tz, now, vault, cascade=None):
    from rudi_platform.engine import TurnEngine
    from rudi_platform.gateway import AIError
    from rudi_platform.memory import from_flat, to_flat
    from rudi_platform.privacy.deid import AliasVault

    started = time.time()
    clone, models = _load()
    schema = clone.memory_schema(CHANNEL)
    pvault = AliasVault.from_dict(vault.to_dict() if vault is not None else None)
    try:
        o = TurnEngine(clone, models, _cascade(cascade)).turn(
            from_flat(ai_state or {}, schema), masked or "", vault=pvault, channel=CHANNEL,
            locale=locale, tz=tz or "", now=now, persona_key=persona or "")
    except AIError as e:
        raise _translate(e) from None
    _sync_vault(vault, o.vault.to_dict())
    gen = o.generation
    info = {
        "phase": o.stage,
        "phase_before": o.stage_before,
        "signals": dict(o.signals or {}),
        "model": gen.endpoint if gen else None,
        "lang": o.lang,
        "leak_blocked": o.leak_blocked,
        "engine": PLATFORM,
    }
    if gen:
        info["usage"] = {"prompt_tokens": gen.usage.tokens_in,
                         "completion_tokens": gen.usage.tokens_out}
    if o.greeted:
        info["greeted"] = True
        if o.new_contact:
            info["new_contact"] = True
    if o.halt:
        info["halted"] = dict(o.halt)
    guard = {k: v for k, v in (("leak", o.leak_reason), ("envelope", o.envelope))
             if v and v != "json"}
    if guard:
        info["guard"] = guard
    tokens = f"{gen.usage.tokens_in}/{gen.usage.tokens_out}" if gen else "0/0"
    print(f"ENGINE platform stage={o.stage_before}->{o.stage} endpoint={info['model']} "
          f"tokens={tokens} attempts={gen.attempts if gen else 0} "
          f"ms={int((time.time() - started) * 1000)}"
          + (" halted" if o.halt else "")
          + "".join(f" {k}={v}" for k, v in guard.items()))
    return o.reply_masked, to_flat(o.state, schema), info


def respond(ai_state, masked, *, locale, persona="", tz="", now=None, vault=None,
            personality_block=None, engine=None, legacy=None, personas=None, cascade=None):
    """One message, already de-identified with `vault`. Returns (reply, new_state, info).

    `reply` is de-identified, as stored; the caller restores names with `vault` before sending.
    `info` is responder.respond's (phase, signals, model, lang, phase_before, leak_blocked,
    usage), plus "engine", and from the platform possibly "halted" (an inbound gate stopped the
    turn: the reply is the stop acknowledgement and nothing moved) and "guard" (why a reply was
    blocked, labels only).

    `engine` overrides CHAT_ENGINE (tests, the arena door). `legacy`, `personas` and
    `personality_block` let a caller pass the references its tests stub. `cascade` replaces the
    live models on the platform path (the arena's own keys); a legacy turn takes its cascade from
    gateway.using(), which the arena door sets around the whole call.
    """
    legacy = legacy or responder
    if (engine or default_engine()) == PLATFORM and ready():
        try:
            return _on_platform(ai_state, masked, locale, persona, tz, now, vault, cascade)
        except gateway.AIError:
            raise
        except Exception as e:  # noqa: BLE001 - never lose a reply to the switch
            print(f"ERROR: platform turn failed, this message runs on legacy: "
                  f"{type(e).__name__}")
    if personality_block is None:
        personality_block = (personas or personality).resolve_block(persona)
    kwargs = {"locale": locale, "personality_block": personality_block}
    if tz:
        kwargs["tz"] = tz
    if now is not None:
        kwargs["now"] = now
    reply, state, info = legacy.respond(ai_state, masked, **kwargs)
    info = dict(info or {})
    info["engine"] = LEGACY
    return reply, state, info


class Responder:
    """responder-shaped: `respond(state, text, locale=…, personality_block=…, tz=…, now=…)`,
    routed through `respond` above. For webchat.chat_turn, which takes an engine object, so the
    tester console's chat answers on the same engine as WhatsApp."""

    def __init__(self, persona="", vault=None, legacy=None, personas=None, engine=None,
                 cascade=None):
        self.persona, self.vault = persona or "", vault
        self.legacy, self.personas, self.engine = legacy, personas, engine
        self.cascade = cascade
        self.answered = None      # which engine answered the last turn

    def respond(self, state, text, locale="en", personality_block=None, tz="", now=None):
        reply, new_state, info = respond(
            state, text, locale=locale, persona=self.persona, tz=tz, now=now, vault=self.vault,
            personality_block=personality_block, engine=self.engine, legacy=self.legacy,
            personas=self.personas, cascade=self.cascade)
        self.answered = info.get("engine")
        return reply, new_state, info
