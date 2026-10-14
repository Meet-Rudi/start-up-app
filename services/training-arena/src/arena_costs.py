"""
MEET_RUDI Training Arena — prices, cost estimates and budget arithmetic.

Spend has four roles: `rudi`, `tester`, `judge` (model calls, priced from the token counts the
providers report and the price on each cascade entry) and `infra` — an assumed unit price for
the AWS work around every turn and every case (Lambda time, SQS, S3). Infrastructure is part of
the total and of every budget check; its unit prices are settings, not measurements.

All amounts are euro.
"""

ROLES = ("rudi", "tester", "judge", "infra")

# Assumed AWS overhead (Lambda GB-seconds for the runner and engine-turn, SQS, S3 requests).
# Order-of-magnitude figures by design — a super-admin can change them in Engine control.
DEFAULT_INFRA = {"per_turn_eur": 0.00015, "per_case_eur": 0.0002}

# Typical token counts, for estimates and pre-call checks when nothing better is known.
TYPICAL = {"rudi": (3500, 300), "tester": (1400, 90), "judge": (1300, 250)}


def price(entry):
    """(€ per million input tokens, € per million output tokens) of a cascade entry."""
    return float(entry.get("in") or 0.0), float(entry.get("out") or 0.0)


def call_cost(entry, tokens_in, tokens_out):
    pin, pout = price(entry)
    return (max(0, int(tokens_in or 0)) * pin + max(0, int(tokens_out or 0)) * pout) / 1e6


def enabled(entries):
    return [e for e in entries or [] if e.get("on", True)]


def is_free(entry):
    """A free-tier entry: nothing to pay per token (free slow mode waits for these)."""
    return float(entry.get("in") or 0) == 0 and float(entry.get("out") or 0) == 0


def expected_cost(entries, tokens_in, tokens_out):
    """What the next call may cost: priced at the dearest enabled entry, because a failover can
    land anywhere in the cascade. Used for budget checks, so it errs on the high side."""
    live = enabled(entries)
    return max((call_cost(e, tokens_in, tokens_out) for e in live), default=0.0)


def infra(budgets):
    i = dict(DEFAULT_INFRA)
    i.update({k: float(v) for k, v in ((budgets or {}).get("infra") or {}).items() if v is not None})
    return i


def estimate_model(cascades, budgets):
    """Per-unit prices for the validator's budget checks: the DEAREST enabled entry, as if every
    call went to a paid model — the safe side. The launch estimate (arena_estimate) is the
    realistic one, mode by mode."""
    out = {}
    for role in ("rudi", "tester", "judge"):
        tin, tout = TYPICAL[role]
        out[role] = expected_cost((cascades or {}).get(role), tin, tout)
    i = infra(budgets)
    out["infra_turn"], out["infra_case"] = i["per_turn_eur"], i["per_case_eur"]
    return out


def max_turns(spec):
    return int((spec.get("stop") or {}).get("max_turns") or (spec.get("limits") or {}).get("max_turns") or 12)


def estimate_case(spec, model):
    """{"turns", "tester_calls", "judge_calls", "calls", "eur"} for one resolved case — its WORST
    case: a goal-based case runs every turn up to max_turns (the runner's hard stop)."""
    limit = max_turns(spec)
    script = spec.get("script") or []
    scripted = min(len(script), limit)
    improvised = 0
    if spec.get("mode") in ("intent", "hybrid"):
        improvised = max(0, limit - scripted)
    turns = scripted + improvised
    stop = spec.get("stop") or {}
    if stop.get("success_when") or stop.get("abort_when"):
        judge_calls = turns
    else:
        judge_calls = sum(1 for e in script[:scripted] if e.get("expect")) + (1 if spec.get("expect") else 0)
    eur = (turns * (model["rudi"] + model["infra_turn"]) + improvised * model["tester"]
           + judge_calls * model["judge"] + model["infra_case"])
    return {"turns": turns, "tester_calls": improvised, "judge_calls": judge_calls,
            "calls": turns + improvised + judge_calls, "eur": round(eur, 6)}


class Spend:
    """Running spend of one case (or batch), by role."""

    def __init__(self, data=None):
        self.by = {r: float((data or {}).get(r) or 0.0) for r in ROLES}

    def add(self, role, eur):
        self.by[role] = self.by.get(role, 0.0) + float(eur or 0.0)

    @property
    def total(self):
        return sum(self.by.values())

    @property
    def model(self):
        return sum(v for k, v in self.by.items() if k != "infra")

    def room(self, cap):
        return float(cap) - self.total

    def to_dict(self):
        return {r: round(self.by.get(r, 0.0), 6) for r in ROLES}
