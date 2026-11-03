"""
The model gateway (CLAUDE.md §2 `ModelGateway`): endpoints and policy (endpoints.py), HTTP
(transport.py), keys (secrets.py), the reply envelope (envelope.py) and the cascade (core.py).
"""

from .core import Generation, ModelGateway, UsageRecord, UsageSink, UsageTags
from .endpoints import (
    ModelEndpoint,
    ModelPolicy,
    PolicyError,
    load_endpoints,
    resolve_cascade,
)
from .envelope import Envelope, parse_envelope
from .secrets import SecretsManagerResolver, StaticSecrets
from .transport import AIError, AllRateLimited, RateLimitError, UrllibTransport

__all__ = [
    "AIError",
    "AllRateLimited",
    "Envelope",
    "Generation",
    "ModelEndpoint",
    "ModelGateway",
    "ModelPolicy",
    "PolicyError",
    "RateLimitError",
    "SecretsManagerResolver",
    "StaticSecrets",
    "UrllibTransport",
    "UsageRecord",
    "UsageSink",
    "UsageTags",
    "load_endpoints",
    "parse_envelope",
    "resolve_cascade",
]
