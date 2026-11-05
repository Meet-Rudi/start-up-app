"""
MEET_RUDI platform — the model gateway: the only way any skill reaches a model (CLAUDE.md §2).

One implementation of what four service copies of gateway.py each did part of:

  - **Cascade with fail-over** (all services): try each endpoint in order; the first usable answer
    wins. Non-429 failures fail over at once.
  - **429 retry with backoff** (WhatsApp copy): a rate-limited endpoint is retried a few times
    before failing over; AllRateLimited when every endpoint tried ended rate-limited.
  - **Time budget** (call copy): with `budget_s`, no attempt outlasts the time left, and the
    cascade stops once less than MIN_ATTEMPT_S remains, leaving the caller time to say something.
  - **Unusable answers fail over** (call copy): empty content, JSON cut off by the token cap, and
    with `require_reply` an answer without the {reply, signals} envelope.
  - **De-identification sandwich** (memory blueprint part C): with a `vault`, every message is
    masked before it leaves and the answer is restored before anyone parses it, so signals come
    back clean. It lives here so no caller can forget it.
  - **Metering** (decision #25, blueprint §10): every call ends in one UsageRecord — tokens, cost,
    latency, outcome — tagged with tenant, clone, skill, channel and end-user pseudonym.

The cascade is always explicit. Policy resolution (endpoints.resolve_cascade) decides it; there is
no hidden fallback, so an arena run can never spend live quota and a tenant's "EU only" holds.
Logs carry endpoint names and outcomes, never message content.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ..ids import Cat, new_id
from ..privacy.deid import AliasVault, PersonDetector
from ..skills.contract import Usage
from .endpoints import ModelEndpoint
from .envelope import parse_envelope
from .secrets import SecretResolver
from .transport import AIError, AllRateLimited, RateLimitError, Transport

log = logging.getLogger(__name__)

DEFAULT_MAX_RETRIES = 2
DEFAULT_BACKOFF_S = 0.5
DEFAULT_ATTEMPT_TIMEOUT_S = 25.0
MIN_ATTEMPT_S = 2.0

_HTTP_STATUS = re.compile(r"\bHTTP (\d{3})\b")


def failure_kind(error: Exception | None) -> str:
    """Why one attempt failed, as a short label for logs. Never the error text: a provider's error
    body can quote what it was sent."""
    if isinstance(error, RateLimitError):
        return "rate-limited"
    text = str(error or "")
    if "timed out" in text or "TimeoutError" in text:
        return "timeout"
    if "returned no content" in text:
        return "no-content"
    if "token cap" in text:
        return "truncated"
    if "without the reply envelope" in text:
        return "no-envelope"
    status = _HTTP_STATUS.search(text)
    if status:
        return f"http-{status.group(1)}"
    return type(error).__name__ if error is not None else "unknown"


@dataclass(frozen=True)
class UsageTags:
    """Who a model call was for. Ids only (conventions §1), never names or contact data."""

    tenant: str
    clone: str | None = None
    clone_version: str | None = None
    skill: str | None = None  # "slug@1.2.3"
    channel: str | None = None
    end_user: str | None = None
    turn: str | None = None


@dataclass(frozen=True)
class UsageRecord:
    id: str  # use_…
    tags: UsageTags | None
    outcome: str  # ok | rate_limited | failed | out_of_budget
    endpoint: str | None
    model: str | None
    region: str | None
    attempts: int
    tokens_in: int
    tokens_out: int
    cost_eur: float
    latency_ms: int


class UsageSink(Protocol):
    def record(self, rec: UsageRecord) -> None: ...


@dataclass(frozen=True)
class Generation:
    text: str  # restored when a vault was given
    endpoint: str
    model: str
    usage: Usage
    attempts: int
    latency_ms: int
    masked: bool


class ModelGateway:
    def __init__(
        self,
        transport: Transport,
        secrets: SecretResolver,
        *,
        sink: UsageSink | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_s: float = DEFAULT_BACKOFF_S,
    ) -> None:
        self._transport = transport
        self._secrets = secrets
        self._sink = sink
        self._sleep = sleep
        self._clock = clock
        self._max_retries = max_retries
        self._backoff_s = backoff_s

    def generate(
        self,
        messages: Sequence[Mapping[str, Any]],
        cascade: Sequence[ModelEndpoint],
        *,
        json_mode: bool = False,
        budget_s: float | None = None,
        attempt_timeout_s: float | None = None,
        max_tokens: int | None = None,
        vault: AliasVault | None = None,
        detector: PersonDetector | None = None,
        locale: str = "en",
        restore_fallback: str = "them",
        tags: UsageTags | None = None,
        require_reply: bool = False,
    ) -> Generation:
        """Run `cascade` until one endpoint gives a usable answer.

        With a `vault`, a `detector` is required: it must know the clone's persona names
        (HeuristicDetector(keep=...)), or the persona's own name in the prompt would be masked.
        `attempt_timeout_s` caps every single attempt (a live call: a stall past it is dead
        air, so the cascade moves on). `require_reply`: an answer without the {reply, signals}
        envelope (envelope.parse_envelope) is unusable and fails over. Raises AllRateLimited,
        AIError, or ValueError for a malformed call.
        """
        if not cascade:
            raise ValueError(
                "empty cascade: resolve one with endpoints.resolve_cascade"
            )
        if vault is not None and detector is None:
            raise ValueError("a vault needs a detector that knows the persona names")
        start = self._clock()
        deadline = None if budget_s is None else start + float(budget_s)
        wire = self._mask(messages, vault, detector, locale)

        errors: list[str] = []
        attempts = tried = rate_limited = 0
        out_of_budget = False
        for ep in cascade:
            if deadline is not None and deadline - self._clock() < MIN_ATTEMPT_S:
                out_of_budget = True
                errors.append(f"out of budget after {attempts} attempt(s)")
                break
            tried += 1
            last: Exception | None = None
            for attempt in range(self._max_retries + 1):
                timeout = ep.timeout_s or DEFAULT_ATTEMPT_TIMEOUT_S
                if attempt_timeout_s is not None:
                    timeout = min(timeout, float(attempt_timeout_s))
                if deadline is not None:
                    timeout = min(timeout, max(deadline - self._clock(), 0.0))
                attempts += 1
                try:
                    raw, tokens_in, tokens_out = self._call(
                        ep, wire, timeout, json_mode, max_tokens
                    )
                    if require_reply and not parse_envelope(raw).has_reply:
                        raise AIError(f"{ep.name} answered without the reply envelope")
                except RateLimitError as e:
                    last = e
                    delay = self._backoff_s * 2**attempt
                    room = (
                        deadline is None
                        or deadline - self._clock() - delay >= MIN_ATTEMPT_S
                    )
                    if attempt < self._max_retries and room:
                        log.info(
                            "endpoint=%s rate-limited; retrying in %.1fs",
                            ep.name,
                            delay,
                        )
                        self._sleep(delay)
                        continue
                    break
                except Exception as e:  # noqa: BLE001 - any other failure: fail over, don't retry
                    last = (
                        e if isinstance(e, AIError) else AIError(f"{type(e).__name__}")
                    )
                    break
                text = (
                    vault.unmask(raw, fallback=restore_fallback)
                    if vault is not None
                    else raw
                )
                cost = ep.cost_eur(tokens_in, tokens_out)
                latency_ms = int((self._clock() - start) * 1000)
                self._record(
                    tags, "ok", ep, attempts, tokens_in, tokens_out, cost, latency_ms
                )
                return Generation(
                    text=text,
                    endpoint=ep.name,
                    model=ep.model,
                    usage=Usage(ep.model, tokens_in, tokens_out, cost),
                    attempts=attempts,
                    latency_ms=latency_ms,
                    masked=vault is not None,
                )
            if isinstance(last, RateLimitError):
                rate_limited += 1
            log.info("endpoint=%s failed: %s", ep.name, failure_kind(last))
            errors.append(f"{ep.name}: {last}")

        latency_ms = int((self._clock() - start) * 1000)
        if tried and rate_limited == tried:
            self._record(tags, "rate_limited", None, attempts, 0, 0, 0.0, latency_ms)
            raise AllRateLimited("all endpoints rate-limited -> " + " | ".join(errors))
        outcome = "out_of_budget" if out_of_budget else "failed"
        self._record(tags, outcome, None, attempts, 0, 0, 0.0, latency_ms)
        raise AIError("all endpoints failed -> " + " | ".join(errors))

    def has_headroom(
        self, cascade: Sequence[ModelEndpoint], tags: UsageTags | None = None
    ) -> bool:
        """Can a real conversation be held right now? One cheap generation answers it honestly:
        there is no quota API, and the binding limit moves with every other call."""
        try:
            self.generate(
                [
                    {"role": "system", "content": "Reply with the single word: ok"},
                    {"role": "user", "content": "ok"},
                ],
                cascade,
                max_tokens=5,
                tags=tags,
            )
        except AIError:
            return False
        return True

    # ------------------------------------------------------------------------------- internals
    @staticmethod
    def _mask(
        messages: Sequence[Mapping[str, Any]],
        vault: AliasVault | None,
        detector: PersonDetector | None,
        locale: str,
    ) -> list[dict[str, Any]]:
        wire = []
        for m in messages:
            content = str(m.get("content") or "")
            if vault is not None and detector is not None:
                content = vault.mask(content, detector, locale)
            wire.append({"role": m.get("role", "user"), "content": content})
        return wire

    def _call(
        self,
        ep: ModelEndpoint,
        wire: list[dict[str, Any]],
        timeout: float,
        json_mode: bool,
        max_tokens: int | None,
    ) -> tuple[str, int, int]:
        headers = {
            "Authorization": "Bearer " + self._secrets.api_key(ep.secret),
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": ep.model,
            "messages": wire,
            "temperature": ep.temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        limit = max_tokens or ep.max_tokens
        if limit:
            payload["max_tokens"] = int(limit)
        payload.update(
            ep.params
        )  # checked at construction: never model/messages/format/cap
        resp = self._transport.post_json(ep.url, headers, payload, timeout)
        choices = resp.get("choices") if isinstance(resp.get("choices"), list) else []
        choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        message = (
            choice.get("message") if isinstance(choice.get("message"), dict) else {}
        )
        content = str(message.get("content") or "").strip()
        finish = choice.get("finish_reason")
        # Both look like success and are not; nothing above this line could tell them apart.
        if not content:  # a reasoning model can spend its whole budget thinking
            raise AIError(f"{ep.name} returned no content (finish_reason={finish})")
        if (
            finish == "length" and json_mode
        ):  # half an object would be read out as the reply
            raise AIError(f"{ep.name} hit the token cap mid-JSON")
        usage = resp.get("usage") if isinstance(resp.get("usage"), dict) else {}
        return (
            content,
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0),
        )

    def _record(
        self,
        tags: UsageTags | None,
        outcome: str,
        ep: ModelEndpoint | None,
        attempts: int,
        tokens_in: int,
        tokens_out: int,
        cost: float,
        latency_ms: int,
    ) -> None:
        if self._sink is None:
            return
        rec = UsageRecord(
            id=new_id(Cat.USAGE),
            tags=tags,
            outcome=outcome,
            endpoint=ep.name if ep else None,
            model=ep.model if ep else None,
            region=ep.region if ep else None,
            attempts=attempts,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_eur=cost,
            latency_ms=latency_ms,
        )
        try:
            self._sink.record(rec)
        except Exception:  # noqa: BLE001 - metering must never cost a person their reply
            log.warning("usage sink failed outcome=%s", outcome)
