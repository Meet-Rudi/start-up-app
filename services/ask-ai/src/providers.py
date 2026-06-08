"""
MEET_RUDI — AI provider registry (the "how to call each model" helper).

Imported by the meetrudi-ask-ai Lambda at launch. Holds, per provider *kind*, the way to
build the request (URL, headers, payload) and parse the reply. Pure stdlib + boto3 (boto3 is
provided by the Lambda runtime), so the function ships with zero pip dependencies.

To support a new provider, add a `_call_<kind>` method and reference that `kind` in the
S3 endpoints config. This is the single swap-point for API <-> self-hosted later.
"""

import json
import urllib.request
import urllib.error

import boto3

_secrets = boto3.client("secretsmanager")
_secret_cache = {}


class AIError(Exception):
    """Raised when a provider call cannot produce a reply."""


class RateLimitError(AIError):
    """Raised specifically when a provider returns HTTP 429 (quota / rate limit exhausted)."""


def get_secret(secret_id):
    """
    Fetch an API key from Secrets Manager. The secret may be stored either as a raw key
    string or as JSON like {"api_key": "..."}. Cached per warm container.
    """
    if secret_id in _secret_cache:
        return _secret_cache[secret_id]
    resp = _secrets.get_secret_value(SecretId=secret_id)
    raw = resp.get("SecretString", "") or ""
    key = raw
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            # Match common field names case-insensitively (api_key, API_key, apiKey, key, ...)
            lowered = {str(k).lower(): v for k, v in obj.items()}
            for cand in ("api_key", "apikey", "key", "token", "secret"):
                if lowered.get(cand):
                    key = lowered[cand]
                    break
            else:
                # Single-field secret: take its only value.
                if len(obj) == 1:
                    key = next(iter(obj.values()))
    except (ValueError, TypeError):
        key = raw
    _secret_cache[secret_id] = key
    return key


class ProviderRegistry:
    """Dispatches a chat request to the correct provider implementation by `kind`."""

    def call(self, cfg, messages, timeout=25):
        kind = cfg.get("kind", "openai_compatible")
        method = getattr(self, "_call_" + kind, None)
        if method is None:
            raise AIError("Unsupported provider kind: %s" % kind)
        return method(cfg, messages, timeout)

    # --- transport ---------------------------------------------------------
    def _post_json(self, url, headers, payload, timeout):
        data = json.dumps(payload).encode("utf-8")
        # Some providers (e.g. Groq) sit behind Cloudflare, which blocks the default
        # "Python-urllib/x.y" User-Agent with HTTP 403 / error 1010. Send a normal UA.
        merged = dict(headers)
        merged.setdefault(
            "User-Agent",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        )
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

    # --- provider kinds ----------------------------------------------------
    def _call_openai_compatible(self, cfg, messages, timeout):
        """OpenAI-style /chat/completions (Mistral, Groq, Together, Fireworks, vLLM, ...)."""
        key = get_secret(cfg["secret"])
        headers = {
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
        }
        payload = {
            "model": cfg["model"],
            "messages": messages,
            "temperature": cfg.get("temperature", 0.4),
        }
        resp = self._post_json(cfg["endpoint"], headers, payload, timeout)
        return resp["choices"][0]["message"]["content"]

    # Groq speaks the OpenAI wire format.
    _call_groq = _call_openai_compatible

    def _call_anthropic(self, cfg, messages, timeout):
        """Anthropic Messages API (/v1/messages)."""
        key = get_secret(cfg["secret"])
        system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
        user_msgs = [m for m in messages if m.get("role") != "system"]
        headers = {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        payload = {
            "model": cfg["model"],
            "max_tokens": cfg.get("max_tokens", 1024),
            "system": system,
            "messages": user_msgs,
        }
        resp = self._post_json(cfg["endpoint"], headers, payload, timeout)
        return resp["content"][0]["text"]
