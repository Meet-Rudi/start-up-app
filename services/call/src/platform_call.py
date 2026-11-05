"""
MEET_RUDI — meetrudi-call on the platform Turn Engine (decision #37).

ws.py runs every call on one of two engines, chosen when the call is answered and kept until it
ends (manifest["engine"]), so a deploy or a flipped switch never changes engine mid-call:

    legacy    brain.py, as it has run since August
    platform  CM's clone on the platform Turn Engine (packages/rudi_platform, rudi_skills,
              rudi_tenants), shipped in the meetrudi-tenant-prod-pool-engine layer

Which one: the call config's `engine` when it names one (a test call can opt in), else
CALL_ENGINE (default legacy). If the platform cannot load, the call runs on legacy instead: the
switch must never be the reason a call fails.

This module only turns (state, what they said, call config) into a reply, as brain.open_call and
brain.turn do. Telephony stays in ws.py: voicemail, hang-ups, the call ledger, the lost-thread and
operational-pause lines.

The platform is imported lazily, so a legacy call never depends on the layer. Logs carry ids,
endpoint names, token counts and timings, never what was said (CLAUDE.md §5).
"""

import json
import logging
import os
import time

import boto3
import gateway

LEGACY, PLATFORM = "legacy", "platform"
ENGINES = (LEGACY, PLATFORM)
DATA_BUCKET = os.environ["DATA_BUCKET"]
ENDPOINTS_KEY = os.environ.get("ENDPOINTS_CONFIG_KEY", "config/ai_endpoints.json")

# How hard gpt-oss may think on a call ("low", "medium", "high"; "" = the provider's default,
# as brain.py gets). Thinking counts against the reply cap and Groq's tokens per minute, so "low"
# is faster — but measured on twelve simulated calls (2026-11-04) it also invented names for the
# caller and parroted its instructions into the leak guard far more often. Off until a tier with
# headroom or an eval says otherwise. An endpoint whose config sets its own `params` keeps them.
REASONING_EFFORT = os.environ.get("CALL_REASONING_EFFORT", "").strip()

# Endpoints too slow for a call's 7 s attempt, tried only after every other one. Measured on
# simulated calls, 2026-11-04: OVH gpt-oss-120b and Llama 3.3 70B time out on a full turn, while
# OVH gpt-oss-20b answers in about 2 s. In the shared config order the 120b sits second, so every
# Groq rate limit cost 7 s of silence before the call moved on.
SLOW_FOR_CALLS = frozenset(
    n.strip() for n in os.environ.get("CALL_SLOW_MODELS", "").split(",") if n.strip())

# Which model answered, and why the others did not ("endpoint=… failed: rate-limited"): ids and
# labels only, never content.
logging.getLogger("rudi_platform.gateway").setLevel(logging.INFO)

# The live call gateway appends this after the configured endpoints on every turn. The platform
# gateway never adds a hidden fallback, so the adapter names it explicitly: same cascade, in the
# same order, as legacy calls get.
FALLBACK = {
    "name": "groq-fallback",
    "kind": "groq",
    "endpoint": os.environ.get("GROQ_ENDPOINT", "https://api.groq.com/openai/v1/chat/completions"),
    "model": os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b"),
    "secret": os.environ.get("GROQ_SECRET", "meetrudi-groq-firstkey"),
}

_s3 = boto3.client("s3")
_loaded = None          # (clone, model gateway), built once per warm container


def default_engine():
    value = os.environ.get("CALL_ENGINE", LEGACY).strip().lower()
    return value if value in ENGINES else LEGACY


def choose(config):
    """The engine a new call runs on: the config's own choice, else the service default."""
    asked = str((config or {}).get("engine") or "").strip().lower()
    return asked if asked in ENGINES else default_engine()


def _load():
    """CM's live clone version, its skills and its knowledge assets, plus the gateway.

    The registry is in memory and seeded from the code in the layer (rudi_tenants.cm), exactly
    as the parity tests build it: there is no PlanetScale registry yet. Assets come from the same
    bucket keys brain.py reads.
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
        # No 429 retries: on a live call a back-off is dead air. Fail over at once, as brain.py's
        # gateway does.
        models = ModelGateway(UrllibTransport(),
                              SecretsManagerResolver(boto3.client("secretsmanager")),
                              max_retries=0)
        _loaded = (clone, models)
        ms = int((time.time() - started) * 1000)
        print(f"ENGINE platform loaded clone_version={clone.version_id} "
              f"skills={len(clone.skills)} ms={ms}")
    return _loaded


def ready():
    """Load the platform now. False (and the reason logged) when it cannot run a call."""
    try:
        _load()
        return True
    except Exception as e:  # noqa: BLE001 - any load failure means "run this call on legacy"
        print(f"ERROR: platform engine unavailable, call runs on legacy: "
              f"{type(e).__name__}: {e}")
        return False


def _for_calls(entry):
    """One config entry as a call uses it: gpt-oss is asked to think briefly."""
    if REASONING_EFFORT and "gpt-oss" in str(entry.get("model") or "") and not entry.get("params"):
        return {**entry, "params": {"reasoning_effort": REASONING_EFFORT}}
    return entry


def _cascade(config=None):
    """config/ai_endpoints.json's enabled entries, then the fallback — read every turn, as
    brain.py's gateway does, so an edit to the config reaches the next turn. Endpoints named in
    CALL_SLOW_MODELS move to the end, in their configured order.

    A test call may pin named endpoints (config `models`), to time one provider on a call;
    a pin that names nothing configured is ignored rather than leaving the call without a model.
    """
    from rudi_platform.gateway import ModelEndpoint

    entries = []
    try:
        obj = _s3.get_object(Bucket=DATA_BUCKET, Key=ENDPOINTS_KEY)
        data = json.loads(obj["Body"].read().decode("utf-8"))
        entries = data.get("endpoints", []) if isinstance(data, dict) else []
    except Exception as e:  # noqa: BLE001 - missing/broken config => fallback only
        print(f"INFO: endpoints config unavailable ({type(e).__name__}); fallback only")
    cascade = []
    for entry in [*entries, FALLBACK]:
        if not isinstance(entry, dict) or not entry.get("enabled", True):
            continue
        try:
            cascade.append(ModelEndpoint.from_config(_for_calls(entry)))
        except (TypeError, ValueError) as e:
            print(f"WARN: endpoint {entry.get('name')!r} skipped: {e}")
    cascade = ([ep for ep in cascade if ep.name not in SLOW_FOR_CALLS]
               + [ep for ep in cascade if ep.name in SLOW_FOR_CALLS])
    pinned = [str(n) for n in ((config or {}).get("models") or []) if n]
    if pinned:
        chosen = [ep for ep in cascade if ep.name in pinned]
        if chosen:
            return chosen
        print(f"WARN: pinned models {pinned} are not configured; using the full cascade")
    return cascade


def _engine(config=None):
    from rudi_platform.engine import TurnEngine

    clone, models = _load()
    return TurnEngine(clone, models, _cascade(config))


def _vault(manifest):
    from rudi_platform.privacy.deid import AliasVault

    return AliasVault.from_dict(manifest.get("_vault") or None)


def _flat(state):
    from rudi_platform.engine import voice
    from rudi_platform.memory import to_flat

    clone, _ = _load()
    return to_flat(state, clone.memory_schema(voice.CHANNEL))


def _info(outcome, started):
    gen = outcome.generation
    info = {
        "phase": outcome.stage,
        "signals": dict(outcome.signals or {}),
        "model": gen.endpoint if gen else None,
        "ended": outcome.ended,
        "leak_blocked": outcome.leak_blocked,
        "engine": PLATFORM,
    }
    tokens = f"{gen.usage.tokens_in}/{gen.usage.tokens_out}" if gen else "0/0"
    ms = int((time.time() - started) * 1000)
    print(f"ENGINE platform stage={outcome.stage_before}->{outcome.stage} "
          f"endpoint={info['model']} tokens={tokens} attempts={gen.attempts if gen else 0} "
          f"ms={ms} ended={outcome.ended}")
    return info


def _translate(error):
    """Platform model errors as the call service's own, so ws.py handles both engines alike."""
    from rudi_platform.gateway import AllRateLimited

    if isinstance(error, AllRateLimited):
        return gateway.AllRateLimited(str(error))
    return gateway.AIError(str(error))


def scrub(manifest, text, language):
    """What they said, de-identified with the call's vault before it is stored or sent.
    Returns (masked, found); the vault goes back onto the manifest."""
    from rudi_platform.privacy.deid import HeuristicDetector, scrub_inbound

    clone, _ = _load()
    vault = _vault(manifest)
    masked, found = scrub_inbound(vault, text or "", HeuristicDetector(keep=clone.persona_names),
                                  language or "en")
    manifest["_vault"] = vault.to_dict()
    return masked, found


def open_call(manifest, config):
    """The person has just picked up. Returns (spoken, stored, state, info).

    `spoken` has names restored for Twilio; `stored` and the state's history stay
    de-identified, as on legacy calls.
    """
    from rudi_platform.engine import voice
    from rudi_platform.gateway import AIError

    started = time.time()
    vault = _vault(manifest)
    try:
        outcome = voice.open_call(_engine(config), config, vault)
    except AIError as e:
        raise _translate(e) from None
    manifest["_vault"] = vault.to_dict()
    return outcome.reply, outcome.reply_masked, _flat(outcome.state), _info(outcome, started)


def turn(manifest, state, masked, config, elapsed_s=0):
    """One finished utterance, already scrubbed by `scrub`. Returns (spoken, stored, state, info);
    info["ended"] means: speak the reply, then hang up."""
    from rudi_platform.engine import voice
    from rudi_platform.gateway import AIError
    from rudi_platform.memory import from_flat

    started = time.time()
    clone, _ = _load()
    vault = _vault(manifest)
    try:
        outcome = voice.call_turn(_engine(config),
                                  from_flat(state, clone.memory_schema(voice.CHANNEL)),
                                  masked, config, vault, elapsed_s=elapsed_s)
    except AIError as e:
        raise _translate(e) from None
    manifest["_vault"] = vault.to_dict()
    return outcome.reply, outcome.reply_masked, _flat(outcome.state), _info(outcome, started)
