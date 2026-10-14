"""
MEET_RUDI — meetrudi-engine-turn: one chat turn of the deployed Rudi engine, for the Training Arena.

Invoked directly (lambda:InvokeFunction) by the arena's case runner — there is no Function URL.
It is stateless: the caller hands in the conversation state and alias vault it got back from the
previous turn, and stores what comes back. Rudi's prompts are read from the live data bucket
exactly as for every other Rudi function; nothing is written anywhere.

Operations:
    open   Rudi's canned greeting for a fresh conversation (no model call)
    turn   one user message → Rudi's reply, through webchat.chat_turn (de-identify → responder →
           restore) — the same function the tester console's chat uses
    scan   run the engine's own identifier detector over texts (arena input validation)
    info   the release and the Rudi personalities a case may name (arena input validation)

The arena chooses only WHOSE KEYS a turn spends. A `turn` must carry the arena's own Rudi cascade,
and it is refused unless every entry points at a provider live Rudi already uses and every key
lives under meetrudi/arena/rudi-. Inside gateway.using() the in-code live Groq fallback is not
appended, so an arena turn can never spend live Rudi's quota.

Logs carry opaque IDs, operations and timings only — never message text (CLAUDE.md §5).
"""

import os
import time
import datetime
import urllib.parse

import deid
import gateway
import personality
import responder
import webchat

RELEASE = os.environ.get("RELEASE", "") or "unknown"
ARENA_SECRET_PREFIX = os.environ.get("ARENA_SECRET_PREFIX", "meetrudi/arena/rudi-")
KINDS = ("openai_compatible", "groq")
MAX_TEXT = 4000
MAX_SCAN = 1000


class BadCascade(ValueError):
    """The supplied cascade would touch something other than the arena's own Rudi keys."""


def _host(url):
    p = urllib.parse.urlparse(str(url or ""))
    return p.hostname if p.scheme == "https" and p.hostname else None


def _allowed_hosts():
    """Hosts of the providers live Rudi is cleared for: its enabled S3 cascade plus the in-code
    fallback. The list of EU/DPA-cleared providers therefore stays in one place."""
    hosts = {_host(e.get("endpoint")) for e in gateway._load_endpoints()}
    hosts.add(_host(gateway.GROQ_FALLBACK["endpoint"]))
    hosts.discard(None)
    return hosts


def check_cascade(cascade, allowed=None):
    """Return the cascade normalised for gateway.using(), or raise BadCascade."""
    if not isinstance(cascade, list) or not cascade:
        raise BadCascade("an arena turn needs a non-empty cascade")
    allowed = _allowed_hosts() if allowed is None else allowed
    out = []
    for i, e in enumerate(cascade):
        if not isinstance(e, dict):
            raise BadCascade("entry %d is not an object" % i)
        host, secret, kind = _host(e.get("endpoint")), str(e.get("secret") or ""), e.get("kind") or "openai_compatible"
        if host is None or host not in allowed:
            raise BadCascade("entry %d: host %r is not one live Rudi uses" % (i, host))
        if not secret.startswith(ARENA_SECRET_PREFIX):
            raise BadCascade("entry %d: key must live under %s" % (i, ARENA_SECRET_PREFIX))
        if kind not in KINDS or not e.get("model") or not e.get("name"):
            raise BadCascade("entry %d: needs name, model and a supported kind" % i)
        out.append({"name": str(e["name"]), "kind": kind, "endpoint": str(e["endpoint"]),
                    "model": str(e["model"]), "secret": secret, "enabled": e.get("enabled", True) is not False})
    if not any(x["enabled"] for x in out):
        raise BadCascade("every entry is disabled")
    return out


def _now(value):
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)


def _engine():
    return {"release": RELEASE, "assets": responder.asset_versions()}


def _usage(info):
    u = info.get("usage") or {}
    pt, ct = u.get("prompt_tokens"), u.get("completion_tokens")
    return {"model": info.get("model"), "tokens_in": pt, "tokens_out": ct,
            "estimated": not (isinstance(pt, int) and isinstance(ct, int))}


def _open(event):
    reply, state, info = responder.respond({}, "", locale=event.get("locale") or "en")
    return {"ok": True, "reply": reply, "state": state, "vault": deid.AliasVault().to_dict(),
            "info": {"phase_before": None, "phase": info.get("phase"), "signals": {}, "model": "none",
                     "lang": None, "leak_blocked": False, "pii_redacted": {}},
            "usage": {"model": "none", "tokens_in": 0, "tokens_out": 0, "estimated": False},
            "engine": _engine(), "ms": 0}


def _turn(event):
    text = str(event.get("text") or "")[:MAX_TEXT]
    if not text.strip():
        return {"ok": False, "error": "bad_request", "detail": "empty text"}
    try:
        cascade = check_cascade(event.get("cascade"))
    except BadCascade as e:
        return {"ok": False, "error": "bad_cascade", "detail": str(e)}
    vault = deid.AliasVault.from_dict(event.get("vault") or None)
    started = time.time()
    try:
        with gateway.using(cascade):
            t = webchat.chat_turn(vault, event.get("state") or {}, text, event.get("locale") or "en",
                                  event.get("personality") or "", tz=event.get("tz") or "",
                                  now=_now(event.get("now")), redacted="[%s removed]")
    except gateway.AllRateLimited as e:
        return {"ok": False, "error": "rate_limited", "detail": str(e)[:300]}
    except gateway.AIError as e:
        return {"ok": False, "error": "engine_failed", "detail": str(e)[:300]}
    ms = int((time.time() - started) * 1000)
    info = t["info"]
    return {"ok": True, "reply": t["reply"], "state": t["state"], "vault": t["vault"],
            "info": {"phase_before": info.get("phase_before"), "phase": info.get("phase"),
                     "signals": info.get("signals") or {}, "model": info.get("model"),
                     "lang": info.get("lang"), "leak_blocked": bool(info.get("leak_blocked")),
                     "pii_redacted": t["found"] or {}},
            "usage": _usage(info), "engine": _engine(), "ms": ms}


def _scan(event):
    texts = event.get("texts") or []
    if not isinstance(texts, list) or len(texts) > MAX_SCAN:
        return {"ok": False, "error": "bad_request", "detail": "texts must be a list of at most %d" % MAX_SCAN}
    return {"ok": True, "found": [deid.redact(str(t or ""))[1] for t in texts]}


def _info(event):
    return {"ok": True, "release": RELEASE, "default_personality": personality.DEFAULT_SLUG,
            "personalities": [p["slug"] for p in personality.list_available()]}


def handler(event, context=None):
    event = event if isinstance(event, dict) else {}
    op = event.get("op")
    trace = event.get("trace") or {}
    if event.get("channel", "chat") != "chat" and op not in ("scan", "info"):
        return {"ok": False, "error": "bad_request", "detail": "only the chat channel exists in v1"}
    try:
        if op == "open":
            out = _open(event)
        elif op == "turn":
            out = _turn(event)
        elif op == "scan":
            out = _scan(event)
        elif op == "info":
            out = _info(event)
        else:
            out = {"ok": False, "error": "bad_request", "detail": "unknown op"}
    except Exception as e:  # noqa: BLE001 - the arena must get an answer, never a stack trace
        print("ERROR engine-turn op=%s case=%s: %s" % (op, trace.get("case"), type(e).__name__))
        out = {"ok": False, "error": "engine_failed", "detail": type(e).__name__}
    print("ENGINE-TURN op=%s case=%s turn=%s ok=%s err=%s ms=%s"
          % (op, trace.get("case"), trace.get("turn"), out.get("ok"), out.get("error"), out.get("ms")))
    return out
