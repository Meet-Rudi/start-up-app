"""
MEET_RUDI — AI gateway for meetrudi-call.

Provider cascade (S3 config) + Groq fallback + JSON mode + Secrets Manager keys. Pure stdlib +
boto3 (runtime-provided), so the Lambda ships with zero pip dependencies.

Evolved from services/ask-ai (adds JSON-mode and a single `generate()` entry point). Kept
self-contained per service for now; a shared Lambda Layer is a sensible future refactor.
"""

import os
import json
import time
import urllib.request
import urllib.error

import boto3

_secrets = boto3.client("secretsmanager")
_s3 = boto3.client("s3")
_secret_cache = {}

DATA_BUCKET = os.environ["DATA_BUCKET"]
ENDPOINTS_KEY = os.environ.get("ENDPOINTS_CONFIG_KEY", "config/ai_endpoints.json")

# A spoken turn runs against a wall clock. The Lambda is killed at 25s, and a turn that has not
# produced words by then is dead air on a live call: the patient hears nothing, then asks whether
# anyone is there. So the cascade gets a BUDGET rather than an open-ended wait — each attempt is
# capped, the cascade as a whole is capped, and the remainder is Rudi's room to say something
# human instead of failing silently.
#
# 7s per attempt comes from measurement, not taste: across 345 recorded turns the slowest real
# generation was 2.7s and the 99th percentile 2.3s, so 7s is about three times anything genuine.
# Past that it is a stall, and waiting longer only lengthens the silence.
TURN_BUDGET_S = float(os.environ.get("AI_TURN_BUDGET_S", "18"))
ATTEMPT_TIMEOUT_S = float(os.environ.get("AI_ATTEMPT_TIMEOUT_S", "7"))
MIN_ATTEMPT_S = float(os.environ.get("AI_MIN_ATTEMPT_S", "2"))
# Replies are one or two spoken sentences; the longest ever recorded was 35 words. 250 tokens is
# several times that, so it cannot clip real speech — it is there to stop a runaway generation
# from eating the whole budget.
MAX_OUTPUT_TOKENS = int(os.environ.get("AI_MAX_OUTPUT_TOKENS", "250"))

GROQ_FALLBACK = {
    "name": "groq-fallback",
    "kind": "groq",
    "endpoint": os.environ.get("GROQ_ENDPOINT", "https://api.groq.com/openai/v1/chat/completions"),
    "model": os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b"),
    "secret": os.environ.get("GROQ_SECRET", "meetrudi-groq-firstkey"),
    "enabled": True,
}

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36")


class AIError(Exception):
    """A provider call could not produce a reply."""


class RateLimitError(AIError):
    """Provider returned HTTP 429 (quota / rate limit)."""


class AllRateLimited(AIError):
    """Every attempted provider was rate-limited (whole cascade depleted)."""


def get_secret(secret_id):
    if secret_id in _secret_cache:
        return _secret_cache[secret_id]
    raw = _secrets.get_secret_value(SecretId=secret_id).get("SecretString", "") or ""
    key = raw
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            lowered = {str(k).lower(): v for k, v in obj.items()}
            for cand in ("api_key", "apikey", "key", "token", "secret"):
                if lowered.get(cand):
                    key = lowered[cand]
                    break
            else:
                if len(obj) == 1:
                    key = next(iter(obj.values()))
    except (ValueError, TypeError):
        key = raw
    _secret_cache[secret_id] = key
    return key


class ProviderRegistry:
    def call(self, cfg, messages, timeout=25, json_mode=False):
        kind = cfg.get("kind", "openai_compatible")
        method = getattr(self, "_call_" + kind, None)
        if method is None:
            raise AIError("Unsupported provider kind: %s" % kind)
        return method(cfg, messages, timeout, json_mode)

    def _post_json(self, url, headers, payload, timeout):
        data = json.dumps(payload).encode("utf-8")
        merged = dict(headers)
        merged.setdefault("User-Agent", _UA)  # Groq/Cloudflare blocks the default urllib UA
        merged.setdefault("Accept", "application/json")
        req = urllib.request.Request(url, data=data, headers=merged, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            msg = "HTTP %s from %s: %s" % (e.code, url, detail)
            if e.code == 429:
                raise RateLimitError(msg)
            raise AIError(msg)
        except urllib.error.URLError as e:
            raise AIError("Network error calling %s: %s" % (url, e.reason))

    def _call_openai_compatible(self, cfg, messages, timeout, json_mode):
        key = get_secret(cfg["secret"])
        headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
        payload = {
            "model": cfg["model"],
            "messages": messages,
            "temperature": cfg.get("temperature", 0.5),
            "max_tokens": int(cfg.get("max_tokens") or MAX_OUTPUT_TOKENS),
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        resp = self._post_json(cfg["endpoint"], headers, payload, timeout)
        choice = (resp.get("choices") or [{}])[0]
        content = ((choice.get("message") or {}).get("content") or "").strip()
        finish = choice.get("finish_reason")

        # Two answers that look like success and are not. Both have to fail over rather than be
        # passed on, because nothing above this line can tell them from a real reply.
        #
        # Empty content: a reasoning model can spend its whole completion budget thinking and
        # return nothing at all. An empty string is spoken as silence — precisely the failure
        # this change exists to remove.
        if not content:
            raise AIError("%s returned no content (finish_reason=%s)" % (cfg.get("name"), finish))
        # Truncated JSON: the envelope parser treats unparseable text as the reply itself, so
        # half an object would be read aloud to the patient, braces and all.
        if finish == "length" and json_mode:
            raise AIError("%s hit the token cap mid-JSON" % cfg.get("name"))
        return content

    _call_groq = _call_openai_compatible


_registry = ProviderRegistry()


def _load_endpoints():
    try:
        obj = _s3.get_object(Bucket=DATA_BUCKET, Key=ENDPOINTS_KEY)
        data = json.loads(obj["Body"].read().decode("utf-8"))
        eps = data.get("endpoints", []) if isinstance(data, dict) else []
        return [e for e in eps if e.get("enabled", True)]
    except Exception as e:  # noqa: BLE001 - missing/broken config => fallback only
        print("INFO: endpoints config unavailable (%s); fallback only" % e)
        return []


def generate(messages, json_mode=False, budget_s=None):
    """Run the provider cascade (config endpoints, then Groq fallback) inside a time budget.

    Returns {"text": ..., "model": ...}. Raises AllRateLimited if every attempt was 429, or
    AIError if they all failed, ran out of budget, or answered with something unusable.

    The budget is what stops a stall becoming dead air: no attempt may outlast its timeout, and
    the cascade gives up once too little time remains to be worth another try — leaving the
    caller enough of the Lambda's 25s to speak instead of being killed mid-wait.
    """
    cascade = _load_endpoints()
    cascade.append(GROQ_FALLBACK)
    # `is None`, not `or`: a caller passing 0 means "no time left, do not start", and `or` would
    # read that as "unset" and hand it the full budget — the opposite instruction.
    deadline = time.monotonic() + (TURN_BUDGET_S if budget_s is None else float(budget_s))

    errors = []
    attempts = 0
    rate_limited = 0
    for ep in cascade:
        left = deadline - time.monotonic()
        if left < MIN_ATTEMPT_S:
            errors.append("out of turn budget after %d attempt(s)" % attempts)
            break
        # Per-endpoint override exists because the providers are not equally quick, and one slow
        # entry should not be able to spend everyone else's share of the budget.
        timeout = min(float(ep.get("timeout") or ATTEMPT_TIMEOUT_S), left)
        attempts += 1
        try:
            text = _registry.call(ep, messages, timeout=timeout, json_mode=json_mode)
            return {"text": text, "model": ep.get("name")}
        except RateLimitError as e:
            rate_limited += 1
            errors.append("%s: %s" % (ep.get("name"), e))
        except Exception as e:  # noqa: BLE001 - try next provider
            errors.append("%s: %s" % (ep.get("name"), e))

    if attempts > 0 and rate_limited == attempts:
        raise AllRateLimited("All models rate-limited -> " + " | ".join(errors))
    raise AIError("All endpoints failed -> " + " | ".join(errors))


def has_headroom():
    """Can we hold a real conversation right now? One cheap generation answers it honestly.

    There is no quota API to ask. The binding limit is Groq's tokens-per-minute, which is a
    moving target that depends on what every other call just spent — so the only truthful test
    is to actually ask the model something and see whether it answers.

    Used before placing a proactive call: a five-minute conversation that dies on turn one is
    worse than a one-line spoken message, so the caller falls back to speak-only on False.
    """
    try:
        generate([{"role": "system", "content": "Reply with the single word: ok"},
                  {"role": "user", "content": "ok"}])
        return True
    except AIError as e:
        print("INFO: no AI headroom for a conversational call (%s)" % type(e).__name__)
        return False
