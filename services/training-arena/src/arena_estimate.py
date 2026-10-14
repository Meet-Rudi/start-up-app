"""
MEET_RUDI Training Arena — launch-time estimates: how long a batch will take, what it will cost
and when it will end, in normal mode and in free slow mode, launched now or held for quiet hours.

Goal-based (intent / hybrid) cases are counted at their worst case: every turn up to `max_turns`
(the runner's hard stop; 12 when the file sets none, never above the arena's cap), each with a
tester message and, when the case has criteria, a judge call. Scripted cases are their script.

Normal mode: cases run side by side (the parallel ceiling); each role's free entry serves what its
per-minute window holds and the rest spills to the role's paid entry — that share is what the cost
is made of. Free slow mode: one case at a time, every reply waits for its free minute, and when a
free entry's day is used up the batch waits for the daily reset (00:00 UTC). Quiet hours: work
only progresses between 21:30 and 06:30 Europe/Brussels.

Timing constants are calibrated on the 2026-10-14 runs (≈ 100 Rudi replies); everything is an
estimate and says so.
"""

import datetime
import math

import arena_costs
from arena_common import TZ

RUDI_TOKENS = (4200, 400)          # per reply, measured 2026-10-14
TESTER_TOKENS = (1000, 90)
JUDGE_TOKENS = (700, 170)
RUDI_S = {"free": 2.0, "paid": 12.0}   # Groq free tier vs OVH, seconds per reply
TESTER_S, JUDGE_S = 3.0, 4.0
STEP_S = 1.0                       # checkpoint and Lambda hand-over per step
CASE_S = 4.0                       # a case's start: lease, spec, Rudi's greeting
SAFETY = 0.85                      # the LimitTracker plans to 85% of a free allowance
QUIET_START, QUIET_END = (21, 30), (6, 30)
HORIZON_S = 60 * 86400             # stop simulating after 60 days


def _first(entries, free):
    return next((e for e in arena_costs.enabled(entries) if arena_costs.is_free(e) == free), None)


def _tokens(role):
    return {"rudi": RUDI_TOKENS, "tester": TESTER_TOKENS, "judge": JUDGE_TOKENS}[role]


def _day_used(tracker, entry, now):
    """Tokens and requests the arena already used today on a free entry (0 without a tracker)."""
    if tracker is None or entry is None:
        return 0, 0
    _m, d = tracker._windows(tracker._load(entry), now)
    return int(d.get("tokens") or 0), int(d.get("requests") or 0)


# --------------------------------------------------------------------------- clock helpers
def _local(t):
    return datetime.datetime.fromtimestamp(t, TZ)


def _at(t, hm, days=0):
    d = _local(t).date() + datetime.timedelta(days=days)
    return datetime.datetime(d.year, d.month, d.day, hm[0], hm[1], tzinfo=TZ).timestamp()


def in_quiet(t):
    hm = (_local(t).hour, _local(t).minute)
    return hm >= QUIET_START or hm < QUIET_END


def quiet_start(t):
    """The moment work may run: t itself inside quiet hours, else the next 21:30."""
    return t if in_quiet(t) else _at(t, QUIET_START)


def _quiet_end(t):
    hm = (_local(t).hour, _local(t).minute)
    return _at(t, QUIET_END, 1 if hm >= QUIET_START else 0)


def _next_utc_midnight(t):
    d = datetime.datetime.fromtimestamp(t, datetime.timezone.utc).date() + datetime.timedelta(days=1)
    return datetime.datetime(d.year, d.month, d.day, tzinfo=datetime.timezone.utc).timestamp()


def run_until(start, units, unit_s, quiet=False, per_day=None, today_left=None):
    """When `units` pieces of work of `unit_s` seconds each are done, starting at `start`: only
    inside quiet hours when `quiet`, and at most `per_day` units per UTC day (`today_left` today)."""
    t, left = start, (per_day if today_left is None else today_left)
    while units > 0:
        if t - start > HORIZON_S:
            return None
        if quiet and not in_quiet(t):
            t = quiet_start(t)
            continue
        if per_day is not None and left <= 0:
            t, left = _next_utc_midnight(t), per_day
            continue
        bound = _next_utc_midnight(t) if per_day is not None else t + units * unit_s
        if quiet:
            bound = min(bound, _quiet_end(t))
        n = min(units, max(1, int((bound - t) // unit_s)))
        if per_day is not None:
            n = min(n, left)
            left -= n
        t += n * unit_s
        units -= n
    return t


# --------------------------------------------------------------------------- the estimate
def _ceiling(settings, cases):
    s = settings
    hard = max(1, (int(s.get("account_limit") or 10) - int(s.get("live_reserve") or 6)) // max(1, int(s.get("execs_per_case") or 2)))
    return max(1, min(int(s.get("max_parallel") or 1), int(s.get("per_batch_parallel") or 1), hard, max(1, cases)))


def _paid_cost(cascades, role, calls):
    paid = _first(cascades.get(role), False)
    return calls * arena_costs.call_cost(paid, *_tokens(role)) if paid else 0.0


def estimate(stats, cascades, settings, budgets, now, tracker=None):
    """Four plans — normal / free slow × now / quiet hours — plus the assumptions behind them."""
    n = int(stats.get("cases") or 0)
    rudi = int(stats.get("turns") or 0)                        # Rudi's model replies (the greeting is free)
    tester = int(stats.get("tester_calls") or 0)
    judge = int(stats.get("judge_calls") or 0)
    think = float(settings.get("think_time") or 0)
    infra = arena_costs.infra(budgets)
    infra_eur = n * infra["per_case_eur"] + rudi * infra["per_turn_eur"]
    calls = {"rudi": rudi, "tester": tester, "judge": judge}
    per_turn = {"tester": tester / rudi if rudi else 0.0, "judge": judge / rudi if rudi else 0.0}
    free = {r: _first(cascades.get(r), True) for r in calls}

    def day_left(role, safety):
        e = free[role]
        lim = (e or {}).get("limits") or {}
        if not e or not lim.get("tpd"):
            return None, None
        per_call = sum(_tokens(role))
        per_day = int(lim["tpd"] * safety // per_call)
        if lim.get("rpd"):
            per_day = min(per_day, int(lim["rpd"] * safety))
        used_tok, used_req = _day_used(tracker, e, now)
        today = max(0, int((lim["tpd"] * safety - used_tok) // per_call))
        if lim.get("rpd"):
            today = min(today, max(0, int(lim["rpd"] * safety) - used_req))
        return per_day, today

    # ---- normal: free entries serve what their minute holds, the rest goes to the paid entry
    p = _ceiling(settings, n)
    share = {r: (1.0 if free[r] else 0.0) for r in calls}
    turn_s = 0.0
    for _ in range(6):
        rudi_s = share["rudi"] * RUDI_S["free"] + (1 - share["rudi"]) * RUDI_S["paid"]
        turn_s = think + rudi_s + per_turn["tester"] * TESTER_S + per_turn["judge"] * JUDGE_S + 3 * STEP_S
        for role in calls:
            e = free[role]
            tpm = ((e or {}).get("limits") or {}).get("tpm")
            if not e or not calls[role]:
                continue
            demand = p * (60.0 / turn_s) * (1.0 if role == "rudi" else per_turn[role]) * sum(_tokens(role))
            share[role] = min(1.0, tpm / demand) if tpm and demand else 1.0
    for role in calls:                                         # today's free allowance caps the share too
        _per_day, today = day_left(role, 1.0)
        if today is not None and calls[role]:
            share[role] = min(share[role], today / calls[role])
    by_role = {r: round(_paid_cost(cascades, r, calls[r] * (1 - share[r])), 6) for r in calls}
    case_s = CASE_S + (rudi / n if n else 0) * turn_s
    rate = float(settings.get("launch_rate") or 6)
    normal_s = max(n * case_s / p, (n - 1) * 60.0 / rate + case_s) if n else 0.0

    # ---- free slow: one case at a time, each reply waits for its free minute and free day
    pace = []
    unit_s = (think + RUDI_S["free" if free["rudi"] else "paid"] + per_turn["tester"] * TESTER_S
              + per_turn["judge"] * JUDGE_S + 3 * STEP_S)
    per_day, today, feasible = None, None, True
    for role in calls:
        e = free[role]
        if not e or not calls[role]:
            continue
        lim = e.get("limits") or {}
        weight = 1.0 if role == "rudi" else per_turn[role]
        if lim.get("tpm"):
            fit = lim["tpm"] * SAFETY / sum(_tokens(role))      # calls a free minute holds
            if fit < 1:
                feasible = False
            elif weight:
                unit_s = max(unit_s, 60.0 * weight / fit)
        pd, td = day_left(role, SAFETY)
        if pd is not None and weight:
            pd_units, td_units = int(pd / weight), int(td / weight)
            per_day = pd_units if per_day is None else min(per_day, pd_units)
            today = td_units if today is None else min(today, td_units)
    slow_by_role = {r: (0.0 if free[r] else round(_paid_cost(cascades, r, calls[r]), 6)) for r in calls}
    overhead = n * (CASE_S + 2 * STEP_S)                       # starting each case, one after the other
    if free["rudi"]:
        pace.append("about %.1f Rudi replies a minute" % (60.0 / unit_s))
    if per_day:
        pace.append("about %d a day on the free tier" % per_day)

    plans = []
    for slow in (False, True):
        for quiet in (False, True):
            start = quiet_start(now) if quiet else now
            if slow:
                end = run_until(start, rudi, unit_s, quiet, per_day, today) if feasible else None
                end = end + overhead if end is not None else None
                roles = slow_by_role
            else:
                end = run_until(start, int(math.ceil(normal_s)), 1.0, quiet) if normal_s else start
                roles = by_role
            eur = round(sum(roles.values()) + infra_eur, 6)
            plans.append({"free_slow": slow, "quiet": quiet, "eur": eur,
                          "by_role": dict(roles, infra=round(infra_eur, 6)),
                          "start": _local(start).strftime("%Y-%m-%d %H:%M"),
                          "end": _local(end).strftime("%Y-%m-%d %H:%M") if end else None,
                          "duration_s": int(end - now) if end else None,
                          "work_s": int(normal_s if not slow else (rudi * unit_s + overhead)),
                          "parallel": 1 if slow else p,
                          "free_share": {r: round(1.0 if slow and free[r] else share[r], 2) for r in calls}})
    assumptions = [
        "Goal-based cases counted at their worst case: every turn up to max_turns, with a tester message and a judge call each.",
        "Normal mode: %d cases side by side; free tiers serve what their minute holds (Rudi ≈ %d%% of his replies), the rest goes to the paid entry." % (p, round(100 * share["rudi"])),
        "Free slow mode: one case at a time, %s; when a free day is used up it continues after 00:00 UTC." % (", ".join(pace) or "no free entry to wait for"),
        "Free slow costs leave out tester or judge turns a free model refuses and a paid one answers (a few on crisis topics).",
        "Quiet hours: work only progresses between 21:30 and 06:30 Europe/Brussels.",
        "Times are Europe/Brussels; the AWS overhead is included in every cost.",
    ]
    if not feasible:
        assumptions.append("A free entry's minute cannot hold even one call of its role — free slow mode would never finish.")
    return {"now": _local(now).strftime("%Y-%m-%d %H:%M"), "plans": plans, "assumptions": assumptions,
            "counts": {"cases": n, "rudi_replies": rudi, "tester_calls": tester, "judge_calls": judge}}
