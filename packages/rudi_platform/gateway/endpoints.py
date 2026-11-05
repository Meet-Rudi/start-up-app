"""
MEET_RUDI platform — model endpoints and the policy that picks among them.

An endpoint is one model at one provider, tagged with what a tenant's policy needs to judge it:
the region the provider processes in, whether a DPA covers it, the skill tiers it is fit for, and
its price. A policy (per tenant, optionally narrowed per clone) filters the configured endpoints
into the cascade for one call (blueprint §10). Nothing is appended behind the caller's back: the
legacy services always add a Groq fallback in code; here the cascade is exactly what the
configuration and the policy allow, and an empty result fails closed with PolicyError.

`ModelEndpoint.from_config` reads today's config/ai_endpoints.json entries unchanged. Entries
without tags count as region "other" with no DPA, so a strict policy refuses them until tagged.
`params` are extra request fields for that model (e.g. {"reasoning_effort": "low"}), the same key
the Training Arena's cascades use.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

# Request fields `params` may never override: the gateway owns them.
RESERVED_PARAMS = frozenset({"model", "messages", "response_format", "max_tokens"})

REGIONS = ("eu", "us", "other")
TIERS = ("classify", "coach", "long")  # cheap classification, conversation, long-form
KINDS = ("openai_compatible", "groq")


class PolicyError(Exception):
    """No configured endpoint is allowed for this call: the policy left the cascade empty."""


@dataclass(frozen=True)
class ModelEndpoint:
    name: str  # stable slug; appears in usage rows and logs
    url: str
    model: str
    secret: str  # Secrets Manager name holding the API key
    kind: str = "openai_compatible"
    region: str = "other"
    dpa: bool = False
    tiers: frozenset[str] = field(default_factory=lambda: frozenset(TIERS))
    temperature: float = 0.5
    timeout_s: float | None = None
    max_tokens: int | None = None
    price_in_eur_per_mtok: float = 0.0
    price_out_eur_per_mtok: float = 0.0
    enabled: bool = True
    params: Mapping[str, Any] = field(default_factory=dict, hash=False)

    def __post_init__(self) -> None:
        if not self.name or not self.url or not self.model or not self.secret:
            raise ValueError("an endpoint needs name, url, model and secret")
        if self.kind not in KINDS:
            raise ValueError(f"unknown endpoint kind {self.kind!r}")
        if self.region not in REGIONS:
            raise ValueError(
                f"region must be one of {', '.join(REGIONS)}, got {self.region!r}"
            )
        unknown = set(self.tiers) - set(TIERS)
        if unknown or not self.tiers:
            raise ValueError(
                f"tiers must be a non-empty subset of {TIERS}, got {sorted(self.tiers)}"
            )
        if not isinstance(self.params, Mapping):
            raise ValueError("params must be an object")  # noqa: TRY004 - a bad config, like the rest
        clash = RESERVED_PARAMS & set(self.params)
        if clash:
            raise ValueError(f"params may not set {sorted(clash)}")

    @classmethod
    def from_config(cls, d: Mapping[str, object]) -> ModelEndpoint:
        """One entry of an endpoints config. Accepts the legacy keys (`endpoint`, `timeout`)."""

        def opt_float(key: str) -> float | None:
            v = d.get(key)
            return float(v) if v is not None else None  # type: ignore[arg-type]

        tiers = d.get("tiers")
        max_tokens = d.get("max_tokens")
        return cls(
            name=str(d.get("name") or ""),
            url=str(d.get("url") or d.get("endpoint") or ""),
            model=str(d.get("model") or ""),
            secret=str(d.get("secret") or ""),
            kind=str(d.get("kind") or "openai_compatible"),
            region=str(d.get("region") or "other"),
            dpa=bool(d.get("dpa", False)),
            tiers=frozenset(tiers) if tiers else frozenset(TIERS),  # type: ignore[arg-type]
            temperature=float(d.get("temperature", 0.5)),  # type: ignore[arg-type]
            timeout_s=opt_float("timeout_s")
            if "timeout_s" in d
            else opt_float("timeout"),
            max_tokens=int(max_tokens) if max_tokens is not None else None,  # type: ignore[call-overload]
            price_in_eur_per_mtok=float(d.get("price_in_eur_per_mtok", 0.0)),  # type: ignore[arg-type]
            price_out_eur_per_mtok=float(d.get("price_out_eur_per_mtok", 0.0)),  # type: ignore[arg-type]
            enabled=bool(d.get("enabled", True)),
            params=dict(d.get("params") or {}),  # type: ignore[call-overload]
        )

    def cost_eur(self, tokens_in: int, tokens_out: int) -> float:
        return (
            tokens_in * self.price_in_eur_per_mtok
            + tokens_out * self.price_out_eur_per_mtok
        ) / 1_000_000


def load_endpoints(config: Mapping[str, object]) -> list[ModelEndpoint]:
    """All entries of an endpoints config ({"endpoints": [...]}) in order, disabled ones included."""
    entries = config.get("endpoints") or []
    return [ModelEndpoint.from_config(e) for e in entries]  # type: ignore[union-attr]


@dataclass(frozen=True)
class ModelPolicy:
    """What a tenant (or clone) allows. None means "no restriction" for that dimension."""

    regions: frozenset[str] | None = None
    require_dpa: bool = False
    endpoints: frozenset[str] | None = None  # allow-list by endpoint name

    def allows(self, ep: ModelEndpoint) -> bool:
        if self.regions is not None and ep.region not in self.regions:
            return False
        if self.require_dpa and not ep.dpa:
            return False
        return self.endpoints is None or ep.name in self.endpoints

    @classmethod
    def eu_only(cls) -> ModelPolicy:
        """The strict default a tenant can opt into: EU processing under a DPA."""
        return cls(regions=frozenset({"eu"}), require_dpa=True)


def resolve_cascade(
    endpoints: Iterable[ModelEndpoint], policy: ModelPolicy, tier: str = "coach"
) -> list[ModelEndpoint]:
    """The enabled endpoints that fit `tier` and that `policy` allows, in configured order."""
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r}")
    cascade = [
        ep for ep in endpoints if ep.enabled and tier in ep.tiers and policy.allows(ep)
    ]
    if not cascade:
        raise PolicyError(f"no endpoint is allowed for tier {tier!r} under this policy")
    return cascade
