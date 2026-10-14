"""
MEET_RUDI Training Arena — the arena's own model client for the TESTER and the JUDGE.

A cascade is an ordered list of entries (provider, model, arena key, price, known limits).
Before every call the client asks the LimitTracker whether an entry can take the call without
running out of its allowance part-way; an entry that cannot is skipped *before* anything is sent
(preventive failover). An entry that fails anyway — 429, timeout, 5xx, network, an empty reply —
is noted and the call moves to the next entry at once.

Allowances come from two places and the stricter wins:
  - the provider's own rate-limit headers, when it sends them. Groq: x-ratelimit-remaining-tokens
    (tokens per MINUTE) and x-ratelimit-remaining-requests (requests per DAY), each with a reset
    duration such as "2m59.56s"; OVH (Kong): x-ratelimit-remaining-minute and ratelimit-reset.
  - the entry's configured `limits` (rpm, rpd, tpm, tpd), counted by the arena itself — needed
    because no provider reports every window in its headers.
Tracker state is shared through S3 (runtime/limits/), so parallel runners see each other's use.
It is approximate by nature, which is why every check keeps a safety margin.

Rudi's own turns do NOT go through this client: they run in the deployed engine (gateway.py),
which fails over reactively on 429.
"""

import re
import json
import time
import hashlib
import datetime
import urllib.error
import urllib.request

import arena_costs
from arena_common import est_tokens

SAFETY = 0.85                      # never plan to use more than 85% of a known allowance
DEFAULT_TIMEOUT = 40
_UA = "meetrudi-training-arena/1"
_DUR = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")


class LLMError(Exception):
    """Base for client failures."""


class Busy(LLMError):
    """Every entry was skipped or rate-limited; `retry_at` is the earliest moment one frees up."""

    def __init__(self, retry_at, attempts, skipped):
        super().__init__("cascade busy until %s" % retry_at)
        self.retry_at, self.attempts, self.skipped = retry_at, attempts, skipped


class CascadeFailed(LLMError):
    """Every entry was tried and failed for reasons other than limits. `cost` is what the
    unusable replies among them were billed."""

    def __init__(self, attempts, skipped, cost=0.0):
        super().__init__("; ".join("%s: %s" % (a["entry"], a.get("detail") or a["outcome"]) for a in attempts) or "no entry")
        self.attempts, self.skipped, self.cost = attempts, skipped, cost


class _RateLimited(Exception):
    def __init__(self, headers):
        super().__init__("429")
        self.headers = headers or {}


def parse_duration(value):
    """"2m59.56s" → 179.56; "7.66s" → 7.66; "500ms" → 0.5; "10" → 10.0; None if unreadable."""
    if value is None:
        return None
    s = str(value).strip()
    try:
        return float(s)
    except ValueError:
        pass
    total, matched = 0.0, False
    for num, unit in _DUR.findall(s):
        matched = True
        total += float(num) * {"h": 3600, "m": 60, "s": 1, "ms": 0.001}[unit]
    return total if matched else None


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def header_windows(headers, now):
    """Rate-limit windows a response reported: {name: {"remaining", "reset_at"}}."""
    h = {str(k).lower(): v for k, v in (headers or {}).items()}
    out = {}
    pairs = (("tokens", "x-ratelimit-remaining-tokens", "x-ratelimit-reset-tokens"),
             ("requests", "x-ratelimit-remaining-requests", "x-ratelimit-reset-requests"),
             ("requests_minute", "x-ratelimit-remaining-minute", "ratelimit-reset"))
    for name, rem_h, reset_h in pairs:
        remaining = _int(h.get(rem_h))
        if remaining is None:
            continue
        reset = parse_duration(h.get(reset_h))
        out[name] = {"remaining": remaining, "reset_at": now + (reset if reset is not None else 60.0)}
    return out


class LimitTracker:
    """Shared, approximate view of each entry's remaining allowance."""

    def __init__(self, bucket=None, clock=time.time, safety=SAFETY):
        self.bucket, self.clock, self.safety = bucket, clock, safety
        self._mem = {}

    @staticmethod
    def key(entry):
        ident = "%s|%s" % (entry.get("key") or "", entry.get("model") or "")
        return "runtime/limits/%s.json" % hashlib.sha256(ident.encode()).hexdigest()[:16]

    def _load(self, entry):
        k = self.key(entry)
        state = self.bucket.get_json(k, None) if self.bucket else None
        if state is None:
            state = self._mem.get(k) or {}
        self._mem[k] = state
        return state

    def _save(self, entry, state):
        k = self.key(entry)
        self._mem[k] = state
        if self.bucket:
            self.bucket.put_json(k, state)

    def _windows(self, state, now):
        minute = int(now // 60)
        day = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m-%d")
        m = state.get("minute") or {}
        d = state.get("day") or {}
        if m.get("t") != minute:
            m = {"t": minute, "requests": 0, "tokens": 0}
        if d.get("d") != day:
            d = {"d": day, "requests": 0, "tokens": 0}
        return m, d

    def check(self, entry, tokens):
        """(ok, reason, retry_at). `tokens` = prompt estimate + the reply ceiling. When several
        windows are full, retry_at is the LAST of their resets — the moment the call would fit."""
        now = self.clock()
        state = self._load(entry)
        fails = []
        if (state.get("blocked_until") or 0) > now:
            fails.append(("cooling down after a 429", state["blocked_until"]))
        m, d = self._windows(state, now)
        lim = entry.get("limits") or {}
        s = self.safety
        checks = (("rpm", m["requests"] + 1, (int(now // 60) + 1) * 60),
                  ("tpm", m["tokens"] + tokens, (int(now // 60) + 1) * 60),
                  ("rpd", d["requests"] + 1, _next_utc_midnight(now)),
                  ("tpd", d["tokens"] + tokens, _next_utc_midnight(now)))
        for name, would, reset in checks:
            cap = lim.get(name)
            if cap and would > cap * s:
                fails.append(("would pass its %s limit (%d of %d)" % (name, would, cap), reset))
        for name, w in (state.get("headers") or {}).items():
            if (w.get("reset_at") or 0) <= now:
                continue
            need = tokens if name == "tokens" else 1
            if w["remaining"] < need or (name == "tokens" and need > w["remaining"] * s):
                fails.append(("provider reports %s remaining %s" % (name, w["remaining"]), w["reset_at"]))
        if fails:
            reason, at = max(fails, key=lambda f: f[1])
            return False, reason, at
        return True, None, None

    def record(self, entry, tokens, headers=None):
        now = self.clock()
        state = self._load(entry)
        m, d = self._windows(state, now)
        m["requests"] += 1
        m["tokens"] += int(tokens or 0)
        d["requests"] += 1
        d["tokens"] += int(tokens or 0)
        state.update({"minute": m, "day": d, "updated": now, "strikes": 0})
        hw = header_windows(headers, now)
        if hw:
            state["headers"] = dict(state.get("headers") or {}, **hw)
        self._save(entry, state)

    def block(self, entry, headers=None):
        """After a 429: keep the entry out until the provider says it is free again."""
        now = self.clock()
        state = self._load(entry)
        h = {str(k).lower(): v for k, v in (headers or {}).items()}
        wait = parse_duration(h.get("retry-after"))
        if wait is None:
            resets = [w["reset_at"] - now for w in header_windows(headers, now).values() if w["remaining"] <= 0]
            wait = min(resets) if resets else 30.0
        state["blocked_until"] = now + max(1.0, wait)
        state["strikes"] = int(state.get("strikes") or 0) + 1         # 429s in a row, for backing off
        hw = header_windows(headers, now)
        if hw:
            state["headers"] = dict(state.get("headers") or {}, **hw)
        self._save(entry, state)


def _next_utc_midnight(now):
    d = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).date() + datetime.timedelta(days=1)
    return datetime.datetime(d.year, d.month, d.day, tzinfo=datetime.timezone.utc).timestamp()


def _error_hint(err):
    """The provider's error code and the start of its message, e.g. " json_validate_failed: Failed
    to generate JSON…". Never the rest of the body: Groq echoes the generation in failed_generation."""
    try:
        body = json.loads(err.read().decode("utf-8", "replace") or "{}")
    except Exception:  # noqa: BLE001 - no readable body: the status code alone will do
        return ""
    e = body.get("error") if isinstance(body, dict) else None
    if not isinstance(e, dict):
        return ""
    code = str(e.get("code") or e.get("type") or "")[:40]
    msg = " ".join(str(e.get("message") or "").split())[:120]
    return " " + ": ".join(x for x in (code, msg) if x) if (code or msg) else ""


class HttpTransport:
    """OpenAI-compatible chat completions over urllib. Returns (json, headers)."""

    def post(self, url, key, payload, timeout):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Authorization": "Bearer " + key, "Content-Type": "application/json",
            "Accept": "application/json", "User-Agent": _UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8")), dict(r.headers.items())
        except urllib.error.HTTPError as e:
            headers = dict(e.headers.items()) if e.headers else {}
            if e.code == 429:
                raise _RateLimited(headers)
            raise LLMError("HTTP %s%s" % (e.code, _error_hint(e)))
        except urllib.error.URLError as e:
            raise LLMError("network: %s" % getattr(e, "reason", e))
        except TimeoutError:
            raise LLMError("timeout")


class Secrets:
    """Secrets Manager keys, cached per container. Accepts a plain string or a JSON object."""

    def __init__(self, client):
        self.client, self._cache = client, {}

    def get(self, name):
        if name not in self._cache:
            raw = self.client.get_secret_value(SecretId=name).get("SecretString", "") or ""
            key = raw
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    low = {str(k).lower(): v for k, v in obj.items()}
                    key = next((low[c] for c in ("api_key", "apikey", "key", "token") if low.get(c)),
                               next(iter(obj.values())) if len(obj) == 1 else raw)
            except ValueError:
                pass
            self._cache[name] = key
        return self._cache[name]


class Result:
    def __init__(self, text, entry, tokens_in, tokens_out, estimated, cost, attempts, skipped, ms):
        self.text, self.entry = text, entry
        self.tokens_in, self.tokens_out, self.estimated = tokens_in, tokens_out, estimated
        self.cost, self.attempts, self.skipped, self.ms = cost, attempts, skipped, ms

    @property
    def model(self):
        return self.entry.get("name")


class Cascade:
    """One role's ordered entries, with preventive failover."""

    def __init__(self, role, entries, tracker, secrets, transport=None, clock=time.time):
        self.role = role
        self.entries = arena_costs.enabled(entries)
        self.tracker, self.secrets = tracker, secrets
        self.transport = transport or HttpTransport()
        self.clock = clock
        self.prefer_free = False           # free slow mode: a free entry at its limit is waited for

    def chat(self, messages, json_mode=True, max_tokens=500, temperature=None, timeout=DEFAULT_TIMEOUT, accept=None):
        """`accept(text)` returns None for a usable reply, or a short reason — naming the reply's
        shape, never its content — and the call moves on to the next entry, as on an error.

        With `prefer_free`, a free entry held back by its limits is not replaced by a paid one: the
        call raises Busy for the moment it frees up. A free entry that fails for any other reason
        (an error, an unusable reply) still hands over to the paid entries."""
        prompt = sum(len(m.get("content") or "") for m in messages)
        need = est_tokens(prompt) + int(max_tokens or 0)
        attempts, skipped, retry_at, discarded, free_waiting = [], [], [], 0.0, []
        for e in self.entries:
            free = arena_costs.is_free(e)
            if self.prefer_free and free_waiting and not free:
                skipped.append({"entry": e.get("name"), "reason": "free slow mode: waiting for the free tier"})
                continue
            ok, reason, free_at = self.tracker.check(e, need)
            if not ok:
                skipped.append({"entry": e.get("name"), "reason": reason})
                if free_at:
                    retry_at.append(free_at)
                    if free:
                        free_waiting.append(free_at)
                continue
            payload = {"model": e["model"], "messages": messages}
            if temperature is not None:
                payload["temperature"] = temperature
            if max_tokens:
                payload[e.get("max_tokens_param") or "max_tokens"] = int(max_tokens)
            if json_mode:
                payload["response_format"] = {"type": "json_object"}
            payload.update(e.get("params") or {})
            started = self.clock()
            try:
                key = self.secrets.get(e["key"])
            except Exception as err:  # noqa: BLE001 - a missing / unreadable key fails this entry over, not the step
                attempts.append({"entry": e.get("name"), "outcome": "no_key", "detail": "key unavailable (%s)" % type(err).__name__})
                continue
            try:
                body, headers = self._post(e, key, payload, timeout)
            except _RateLimited as rl:
                self.tracker.block(e, rl.headers)
                attempts.append({"entry": e.get("name"), "outcome": "rate_limited"})
                state_until = self.tracker._load(e).get("blocked_until")
                if state_until:
                    retry_at.append(state_until)
                    if free:
                        free_waiting.append(state_until)
                continue
            except LLMError as err:
                attempts.append({"entry": e.get("name"), "outcome": "error", "detail": str(err)[:160]})
                continue
            text = _content(body)
            usage = body.get("usage") if isinstance(body, dict) else None
            tin = _int((usage or {}).get("prompt_tokens"))
            tout = _int((usage or {}).get("completion_tokens"))
            estimated = tin is None or tout is None
            if estimated:
                tin, tout = est_tokens(prompt), est_tokens(text or "")
            self.tracker.record(e, tin + tout, headers)
            if not text:
                attempts.append({"entry": e.get("name"), "outcome": "error", "detail": "empty reply"})
                continue
            why = accept(text) if accept else None
            if why:
                discarded += arena_costs.call_cost(e, tin, tout)
                attempts.append({"entry": e.get("name"), "outcome": "error", "detail": "unusable reply: " + why})
                continue
            return Result(text, e, tin, tout, estimated, arena_costs.call_cost(e, tin, tout) + discarded,
                          attempts, skipped, int((self.clock() - started) * 1000))
        if self.prefer_free and free_waiting:
            raise Busy(min(free_waiting), attempts, skipped)
        # Only limits (and entries whose key is not configured yet) stood in the way: that is a
        # matter of waiting for a window, not a failure. A missing key on its own is a failure.
        if not any(a["outcome"] == "error" for a in attempts) and (retry_at or not attempts):
            raise Busy(min(retry_at) if retry_at else self.clock() + 60, attempts, skipped)
        raise CascadeFailed(attempts, skipped, discarded)

    def _post(self, e, key, payload, timeout):
        try:
            return self.transport.post(e["endpoint"], key, payload, timeout)
        except LLMError as err:
            if "json_validate_failed" not in str(err) or "response_format" not in payload:
                raise
        # The provider refused its own malformed JSON (Groq: json_validate_failed). Ask the same
        # entry once more without JSON mode; parse_json_reply digs the object out of plain text.
        return self.transport.post(e["endpoint"], key, {k: v for k, v in payload.items() if k != "response_format"}, timeout)


_THINK = re.compile(r"<think>.*?(?:</think>|$)", re.S)


def _content(body):
    try:
        c = body["choices"][0]["message"].get("content")
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""
    return _THINK.sub("", c).strip() if isinstance(c, str) else ""


def parse_json_reply(text):
    """Defensive JSON parse of a model reply; {} when nothing usable is in it."""
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        a, b = (text or "").find("{"), (text or "").rfind("}")
        try:
            obj = json.loads(text[a:b + 1]) if a != -1 and b > a else {}
        except ValueError:
            obj = {}
    return obj if isinstance(obj, dict) else {}
