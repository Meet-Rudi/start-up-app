"""
MEET_RUDI — AI gateway for meetrudi-rudi-chat.

Provider cascade (S3 config) + Groq fallback + JSON mode + Secrets Manager keys. Pure stdlib +
boto3 (runtime-provided), so the Lambda ships with zero pip dependencies.

Evolved from services/ask-ai (adds JSON-mode and a single `generate()` entry point). Kept
self-contained per service for now; a shared Lambda Layer is a sensible future refactor.
"""

import os
import json
import urllib.request
import urllib.error

import boto3

_secrets = boto3.client("secretsmanager")
_s3 = boto3.client("s3")
_secret_cache = {}

DATA_BUCKET = os.environ["DATA_BUCKET"]
ENDPOINTS_KEY = os.environ.get("ENDPOINTS_CONFIG_KEY", "config/ai_endpoints.json")
GROQ_FALLBACK = {
    "name": "groq-fallback",
    "kind": "groq",
    "endpoint": os.environ.get("GROQ_ENDPOINT", "https://api.groq.com/openai/v1/chat/completions"),
    "model": os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant"),
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
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        resp = self._post_json(cfg["endpoint"], headers, payload, timeout)
        return resp["choices"][0]["message"]["content"]

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


def generate(messages, json_mode=False):
    """Run the provider cascade (config endpoints, then Groq fallback). Returns
    {"text": ..., "model": ...}. Raises AllRateLimited if every attempt was 429,
    or AIError if all failed for other reasons."""
    cascade = _load_endpoints()
    cascade.append(GROQ_FALLBACK)

    errors = []
    attempts = 0
    rate_limited = 0
    for ep in cascade:
        attempts += 1
        try:
            text = _registry.call(ep, messages, json_mode=json_mode)
            return {"text": text, "model": ep.get("name")}
        except RateLimitError as e:
            rate_limited += 1
            errors.append("%s: %s" % (ep.get("name"), e))
        except Exception as e:  # noqa: BLE001 - try next provider
            errors.append("%s: %s" % (ep.get("name"), e))

    if attempts > 0 and rate_limited == attempts:
        raise AllRateLimited("All models rate-limited -> " + " | ".join(errors))
    raise AIError("All endpoints failed -> " + " | ".join(errors))
