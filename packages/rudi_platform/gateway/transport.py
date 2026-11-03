"""
MEET_RUDI platform — HTTP to model providers, and the errors the cascade reacts to.

The transport only moves JSON and classifies failures: RateLimitError for HTTP 429 (the cascade
retries the same provider with backoff), AIError for everything else (the cascade fails over).
Injected into the gateway, so tests never open a socket.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, Protocol


class AIError(Exception):
    """A provider call could not produce a usable reply."""


class RateLimitError(AIError):
    """The provider returned HTTP 429."""


class AllRateLimited(AIError):
    """Every provider attempted was rate-limited."""


class Transport(Protocol):
    def post_json(
        self, url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float
    ) -> dict[str, Any]: ...


# Groq sits behind Cloudflare, which blocks urllib's default user agent.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)


class UrllibTransport:
    """Standard-library HTTPS. `opener` is urllib.request.urlopen unless a test swaps it."""

    def __init__(self, opener: Callable[..., Any] | None = None) -> None:
        self._open = opener or urllib.request.urlopen

    def post_json(
        self, url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        merged = {"User-Agent": USER_AGENT, "Accept": "application/json", **headers}
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), headers=merged, method="POST"
        )
        try:
            with self._open(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            msg = f"HTTP {e.code} from {url}: {detail}"
            if e.code == 429:
                raise RateLimitError(msg) from None
            raise AIError(msg) from None
        except urllib.error.URLError as e:
            raise AIError(f"network error calling {url}: {e.reason}") from None
        except (TimeoutError, ValueError) as e:
            raise AIError(
                f"bad or late response from {url}: {type(e).__name__}"
            ) from None
        if not isinstance(body, dict):
            raise AIError(f"non-object JSON from {url}")
        return body
