"""
MEET_RUDI platform — API keys for model endpoints.

Endpoints name a Secrets Manager secret; the resolver turns it into the key. The secret may hold
the bare key or a JSON object (api_key / apikey / key / token / secret, or a single value), as the
existing meetrudi-* secrets do. Keys are cached per warm container and never logged.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Protocol

_KEY_FIELDS = ("api_key", "apikey", "key", "token", "secret")


class SecretResolver(Protocol):
    def api_key(self, secret_name: str) -> str: ...


def extract_key(raw: str) -> str:
    """The API key inside a secret string, whatever its shape."""
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return raw
    if not isinstance(obj, dict):
        return raw
    lowered = {str(k).lower(): v for k, v in obj.items()}
    for cand in _KEY_FIELDS:
        if lowered.get(cand):
            return str(lowered[cand])
    if len(obj) == 1:
        return str(next(iter(obj.values())))
    return raw


class SecretsManagerResolver:
    """Reads keys through an injected boto3 Secrets Manager client, cached per container."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self._cache: dict[str, str] = {}

    def api_key(self, secret_name: str) -> str:
        if secret_name not in self._cache:
            raw = (
                self._client.get_secret_value(SecretId=secret_name).get("SecretString")
                or ""
            )
            self._cache[secret_name] = extract_key(raw)
        return self._cache[secret_name]


class StaticSecrets:
    """Keys from a mapping. For tests and local runs only."""

    def __init__(self, keys: Mapping[str, str]) -> None:
        self._keys = dict(keys)

    def api_key(self, secret_name: str) -> str:
        return self._keys[secret_name]
