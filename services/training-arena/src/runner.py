"""
MEET_RUDI Training Arena — meetrudi-arena-case-runner: runs ONE case, one conversation with Rudi.

Driven by the cases queue; the dispatcher puts one message per case on it. The conversation is a
sequence of steps — Rudi's greeting, a user message (script or tester), Rudi's reply, judging —
and the case state is checkpointed to S3 after every step. So:

  - a step that fails (provider 429 / timeout / outage) is retried on its own, with backoff, and
    the cascades fail over before that;
  - if the Lambda itself dies mid-step, the redelivered message resumes from the checkpoint and
    re-runs only the step that was in flight;
  - with under 90 s of its 15 minutes left, the runner re-queues the case and exits.

Rudi's replies come from meetrudi-engine-turn (the deployed Rudi engine) on the arena's own Rudi
cascade. The tester and judge run on the arena's own cascades (arena_llm, preventive failover).

Budget: before every model call the runner adds that call's expected cost — priced at the
dearest entry the cascade could fail over to — plus the AWS overhead of a turn, and stops the
case as `over_budget` if it would pass the case ceiling.

Logs: IDs, step names, timings and costs. Never message text (CLAUDE.md §5).
"""

import os
import json
import time
import datetime

import arena_costs
import arena_eval
import arena_llm
import arena_results
import arena_tester
from arena_common import iso, log, est_tokens, parse_iso, case_id
from arena_store import ArenaStore

ARENA_BUCKET = os.environ.get("ARENA_BUCKET", "")
ENGINE_FUNCTION = os.environ.get("ENGINE_FUNCTION", "meetrudi-engine-turn")
CASES_QUEUE_URL = os.environ.get("CASES_QUEUE_URL", "")
DISPATCH_QUEUE_URL = os.environ.get("DISPATCH_QUEUE_URL", "")
RELEASE = os.environ.get("RELEASE", "unknown")
MIN_REMAINING_MS = 90_000
RUDI_PROMPT_TOKENS = 3500          # Rudi's system prompt, before history (estimate for budget checks)
BUSY_WAIT_MAX_S = 70               # a free tier's per-minute window: wait it out within the step
BREAKER_STREAK = 3

TERMINAL_ERRORS = ("engine_error", "tester_error", "judge_error", "timeout", "runner_lost")


class EngineTimeout(Exception):
    pass


class StepFailed(Exception):
    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code, self.detail = code, detail


class Deps:
    """Everything the runner touches outside itself — real in Lambda, fakes in tests."""

    def __init__(self, store, engine, notify, requeue, cascade_for, clock=time.time, sleep=time.sleep,
                 remaining_ms=lambda: 900_000):
        self.store, self.engine, self.notify, self.requeue = store, engine, notify, requeue
        self.cascade_for, self.clock, self.sleep, self.remaining_ms = cascade_for, clock, sleep, remaining_ms


def _rudi_engine_cascade(entries):
    return [{"name": e.get("name"), "kind": e.get("kind") or "openai_compatible", "endpoint": e.get("endpoint"),
             "model": e.get("model"), "secret": e.get("key"), "enabled": e.get("on", True)} for e in entries or []]


def _median(xs):
    xs = sorted(xs)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) // 2


class CaseRun:
    def __init__(self, deps, msg):
        self.d, self.msg = deps, msg
        self.bid, self.seq, self.attempt = msg["batch"], int(msg["seq"]), int(msg.get("attempt") or 1)
        self.store = deps.store

    # ------------------------------------------------------------------ entry
    def run(self):
        st = self.store
        lease = st.lease(self.bid, self.seq)
        if not lease or int(lease.get("attempt") or 1) != self.attempt:
            log("case.skip_stale", batch=self.bid, seq=self.seq, attempt=self.attempt)
            return "stale"
        if st.result(self.bid, self.seq):
            self._notify(st.result(self.bid, self.seq)["row"])            # finished earlier; say so again
            return "done"
        self.manifest = st.manifest(self.bid)
        self.spec = st.spec(self.bid, self.seq)
        self.cp = st.checkpoint(self.bid, self.seq) or self._new_checkpoint()
        self.spend = arena_costs.Spend(self.cp["spend"])
        self._build_clients()
        while self.cp["stop"] is None:
            if self.d.remaining_ms() < MIN_REMAINING_MS:
                self._save()
                self.d.requeue({"batch": self.bid, "seq": self.seq, "attempt": self.attempt, "continue": True})
                log("case.continue_later", batch=self.bid, seq=self.seq, turn=self.cp["turn"])
                return "requeued"
            self._heartbeat()
            if self._cancelled():
                self._stop("cancelled")
                break
            self._step()
            self._save()
        self._finish()
        return "finished"

    # ------------------------------------------------------------------ setup
    def _new_checkpoint(self):
        st = self.store
        settings, budgets, cascades = st.engine_settings(), st.budgets(), st.cascades()
        return {"case_id": case_id(self.bid, self.seq, self.manifest_total()), "seq": self.seq,
                "started_at": iso(), "settings": {k: settings[k] for k in ("think_time", "retries", "backoff", "turn_timeout",
                                                                              "tester_temp", "tester_max_tokens", "breaker", "breaker_min")},
                "infra": arena_costs.infra(budgets), "cascades": cascades,
                "opened": False, "turn": 0, "script_idx": 0, "attempts": 0, "pending_user": None,
                "rudi_state": {}, "vault": None, "sim_offset_s": 0, "transcript": [], "checks": {},
                "errors": [], "retries": 0, "latencies": [], "models": {}, "tokens": [0, 0], "spend": {},
                "lang": None, "guard": {"leak_blocked": 0, "pii_redactions": 0}, "engine": {}, "stop": None,
                "rl_streak": 0, "final_state": {}, "cost_models": {}}

    def manifest_total(self):
        return int((self.store.manifest(self.bid) or {}).get("total") or self.seq)

    def _build_clients(self):
        cas = self.cp["cascades"]
        self.tester = self.d.cascade_for("tester", cas.get("tester"))
        judge = self.d.cascade_for("judge", cas.get("judge"))
        prompts_ok = True
        try:
            judge_prompt = self.store.prompt("arena_judge_prompt.md")
        except RuntimeError:
            judge_prompt, prompts_ok = "", False
        self.registry = arena_eval.build(self.store.evaluators(), judge, judge_prompt,
                                         getattr(self.d, "secrets", None), getattr(self.d, "transport", None))
        self.prompts_ok = prompts_ok

    # ------------------------------------------------------------------ helpers
    def _save(self):
        self.cp["spend"] = self.spend.to_dict()
        self.store.put_checkpoint(self.bid, self.seq, self.cp)

    def _heartbeat(self):
        lease = self.store.lease(self.bid, self.seq)
        if lease:
            lease["heartbeat_at"] = self.d.clock()
            self.store.put_lease(lease)

    def _cancelled(self):
        m = self.store.manifest(self.bid) or {}
        return m.get("status") == "cancelled" or self.store.engine_settings().get("state") == "killed"

    def _stop(self, reason, at_turn=None):
        if self.cp["stop"] is None:
            self.cp["stop"] = {"reason": reason, "at_turn": at_turn if at_turn is not None else self.cp["turn"]}

    def _spent_on(self, model, eur):
        """Spend per model, for the dashboard's spend-by-model chart."""
        cm = self.cp.setdefault("cost_models", {})
        cm[model or "unknown"] = round(cm.get(model or "unknown", 0.0) + float(eur or 0.0), 6)

    def _cap(self):
        return float(self.spec["limits"]["max_cost_eur"])

    def _affordable(self, role, tokens_in, tokens_out, extra=0.0):
        exp = arena_costs.expected_cost(self.cp["cascades"].get(role), tokens_in, tokens_out) + extra
        return self.spend.total + exp <= self._cap() + 1e-12

    def _error(self, code, detail, recovered=False, retried_after_ms=None):
        e = {"at_turn": self.cp["turn"], "code": code, "detail": (detail or "")[:200], "recovered": recovered}
        if retried_after_ms is not None:
            e["retried_after_ms"] = retried_after_ms
        self.cp["errors"].append(e)
        return e

    def _mark_recovered(self, turn):
        for e in self.cp["errors"]:
            if e["at_turn"] == turn:
                e["recovered"] = True

    def _sim_now(self):
        off = self.cp["sim_offset_s"]
        if not off:
            return None
        return iso(datetime.datetime.fromtimestamp(self.d.clock() + off, datetime.timezone.utc))

    def _max_turns(self):
        return arena_costs.max_turns(self.spec)

    def _persona_name(self):
        return (self.spec.get("persona") or {}).get("display_name") or "User"

    def _history_chars(self):
        return sum(len(e.get("text") or "") for e in self.cp["transcript"][-20:])

    # ------------------------------------------------------------------ the step machine
    def _step(self):
        cp = self.cp
        if not cp["opened"]:
            return self._open()
        if cp["pending_user"] is None:
            return self._next_user()
        return self._rudi_turn()

    def _open(self):
        if not self.cp.get("infra_case_charged"):
            self.spend.add("infra", self.cp["infra"]["per_case_eur"])
            self._spent_on("AWS (estimate)", self.cp["infra"]["per_case_eur"])
            self.cp["infra_case_charged"] = True
        try:
            out = self._engine({"op": "open", "channel": "chat", "locale": self.spec["locale"],
                                "trace": {"case": self.cp["case_id"], "turn": 0}}, retries=self.cp["settings"]["retries"])
        except StepFailed as f:
            return self._stop(f.code, 0)
        cp = self.cp
        cp["opened"] = True
        cp["rudi_state"], cp["vault"] = out.get("state") or {}, out.get("vault")
        cp["engine"] = out.get("engine") or {}
        cp["transcript"].append({"turn": 0, "from": "rudi", "at": iso(), "text": out.get("reply") or "",
                                 "meta": {"phase": (out.get("info") or {}).get("phase") or "learn", "model": "none",
                                          "note": "canned greeting, no model call"}})

    def _next_user(self):
        cp, spec = self.cp, self.spec
        script = spec.get("script") or []
        if cp["turn"] >= self._max_turns():
            return self._stop("script_end" if spec["mode"] == "scripted" else "max_turns")
        if cp["script_idx"] < len(script):
            e = script[cp["script_idx"]]
            cp["script_idx"] += 1
            cp["pending_user"] = {"text": e["text"], "meta": {"source": "script"}, "gap_s": e.get("gap_s") or 0,
                                  "gap": e.get("gap"), "expect_phase": e.get("expect_phase"), "expect": e.get("expect")}
            return
        if spec["mode"] == "scripted":
            return self._stop("script_end")
        intent = spec.get("intent") or {}
        if cp["turn"] == 0 and not script and intent.get("opening"):
            cp["pending_user"] = {"text": intent["opening"], "meta": {"source": "opening", "pushing": False}}
            return
        s = cp["settings"]
        max_tok = int(s.get("tester_max_tokens") or 400)
        tin = 900 + est_tokens(self._history_chars())
        if not self._affordable("tester", tin, max_tok):
            return self._stop("case_budget")
        try:
            msg, res = self._with_retries("tester_error", lambda: arena_tester.next_message(
                self.tester, self.store.prompt("arena_tester_prompt.md"), spec, cp["transcript"], cp["turn"] + 1,
                self._max_turns(), cp["attempts"], temperature=s.get("tester_temp"), max_tokens=max_tok))
        except StepFailed as f:
            return self._stop(f.code)
        self.spend.add("tester", res.cost)
        self._spent_on(res.model, res.cost)
        cp["tokens"][0] += res.tokens_in
        cp["tokens"][1] += res.tokens_out
        meta = {"source": "tester", "model": res.model, "cost_eur": round(res.cost, 6), "pushing": msg["pushing"]}
        if msg["note"]:
            meta["note"] = msg["note"]
        if res.skipped:
            meta["failover"] = [s_["entry"] for s_ in res.skipped]
        cp["pending_user"] = {"text": msg["text"], "meta": meta}

    def _rudi_turn(self):
        cp, spec = self.cp, self.spec
        pend = cp["pending_user"]
        infra_turn = cp["infra"]["per_turn_eur"]
        if not self._affordable("rudi", RUDI_PROMPT_TOKENS + est_tokens(self._history_chars() + len(pend["text"])), 400, infra_turn):
            return self._stop("case_budget")
        if cp["turn"] > 0 and float(cp["settings"].get("think_time") or 0) > 0:
            self.d.sleep(float(cp["settings"]["think_time"]))
        if pend.get("gap_s"):
            cp["sim_offset_s"] += int(pend["gap_s"])
            pend["meta"]["gap"] = pend.get("gap")
        turn = cp["turn"] + 1
        user_entry = {"turn": turn, "from": "user", "at": iso(), "text": pend["text"], "meta": pend["meta"]}
        sim = self._sim_now()
        if sim:
            user_entry["sim_at"] = sim
        cp["transcript"].append(user_entry)
        cp["turn"] = turn
        if pend["meta"].get("pushing"):
            cp["attempts"] += 1
        payload = {"op": "turn", "channel": "chat", "state": cp["rudi_state"], "vault": cp["vault"], "text": pend["text"],
                   "locale": spec["locale"], "personality": (spec.get("rudi") or {}).get("personality") or "",
                   "now": sim, "cascade": _rudi_engine_cascade(cp["cascades"].get("rudi")),
                   "trace": {"case": cp["case_id"], "turn": turn}}
        started = self.d.clock()
        try:
            out = self._engine(payload, retries=cp["settings"]["retries"])
        except StepFailed as f:
            user_entry["meta"]["unanswered"] = True
            cp["pending_user"] = None
            return self._stop(f.code, turn)
        latency = int((self.d.clock() - started) * 1000)
        self.spend.add("infra", infra_turn)
        self._spent_on("AWS (estimate)", infra_turn)
        self._mark_recovered(turn)
        info, usage = out.get("info") or {}, out.get("usage") or {}
        entry_model = next((e for e in cp["cascades"].get("rudi") or [] if e.get("name") == usage.get("model")), None)
        tin, tout = int(usage.get("tokens_in") or 0), int(usage.get("tokens_out") or 0)
        if usage.get("estimated") or not (tin or tout):
            tin, tout = RUDI_PROMPT_TOKENS + est_tokens(self._history_chars()), est_tokens(out.get("reply") or "")
        cost = arena_costs.call_cost(entry_model, tin, tout) if entry_model else 0.0
        self.spend.add("rudi", cost)
        self._spent_on(usage.get("model") or info.get("model") or "unknown", cost)
        cp["tokens"][0] += tin
        cp["tokens"][1] += tout
        cp["latencies"].append(int(out.get("ms") or latency))
        model = usage.get("model") or info.get("model") or "unknown"
        cp["models"][model] = cp["models"].get(model, 0) + 1
        before, after = info.get("phase_before"), info.get("phase")
        meta = {"latency_ms": int(out.get("ms") or latency), "model": model, "cost_eur": round(cost, 6),
                "tokens": {"in": tin, "out": tout}, "phase": ("%s→%s" % (before, after)) if before and after and before != after else after,
                "lang": info.get("lang")}
        if usage.get("estimated"):
            meta["tokens_estimated"] = True
        if info.get("signals"):
            meta["signals"] = info["signals"]
        guard = {}
        if info.get("leak_blocked"):
            guard["leak_blocked"] = True
            cp["guard"]["leak_blocked"] += 1
        if info.get("pii_redacted"):
            guard["pii_redacted"] = sorted(info["pii_redacted"])
            cp["guard"]["pii_redactions"] += sum(info["pii_redacted"].values())
        if guard:
            meta["guard"] = guard
        retried = [e for e in cp["errors"] if e["at_turn"] == turn]
        if retried:
            meta["retry"] = {"attempts": len(retried) + 1, "first_error": retried[0]["code"]}
        rudi_entry = {"turn": turn, "from": "rudi", "at": iso(), "text": out.get("reply") or "", "meta": meta}
        if sim:
            rudi_entry["sim_at"] = self._sim_now()
        cp["transcript"].append(rudi_entry)
        cp["rudi_state"], cp["vault"] = out.get("state") or cp["rudi_state"], out.get("vault") or cp["vault"]
        cp["engine"] = out.get("engine") or cp["engine"]
        if info.get("lang"):
            cp["lang"] = info["lang"]
        cp["final_state"] = {k: cp["rudi_state"].get(k) for k in ("phase", "goal", "goal_domain", "commitment", "check_in_at")
                             if cp["rudi_state"].get(k) is not None}
        cp["pending_user"] = None
        self._judge_turn(turn, pend, rudi_entry, after)
        self._check_stops(turn, after)

    # ------------------------------------------------------------------ judging
    def _check(self, cid, **fields):
        c = self.cp["checks"].setdefault(cid, {"cost_eur": 0.0})
        c.update({k: v for k, v in fields.items() if k != "cost_eur"})
        c["cost_eur"] = round(c["cost_eur"] + float(fields.get("cost_eur") or 0.0), 6)
        return c

    def _judge_turn(self, turn, pend, rudi_entry, phase):
        stop = self.spec.get("stop") or {}
        if pend.get("expect_phase"):
            ok = pend["expect_phase"] == phase
            self._check("phase-%d" % turn, check="after turn %d — phase is %s" % (turn, pend["expect_phase"]),
                        kind="phase", result="pass" if ok else "fail", at_turn=turn, why="phase is %s" % phase, evaluator="phase")
        criteria = []
        if pend.get("expect"):
            criteria.append({"id": "expect-%d" % turn, "kind": "judged", "text": pend["expect"]["text"],
                             "evaluator": pend["expect"]["evaluator"]})
        succ = self.cp["checks"].get("success_when")
        if stop.get("success_when") and not (succ and succ.get("result") == "pass"):
            criteria.append({"id": "success_when", "kind": "success_when", "text": stop["success_when"]["text"],
                             "evaluator": stop["success_when"]["evaluator"]})
        if stop.get("abort_when"):
            criteria.append({"id": "abort_when", "kind": "abort_when", "text": stop["abort_when"]["text"],
                             "evaluator": stop["abort_when"]["evaluator"]})
        if not criteria:
            return
        verdicts = self._evaluate(criteria)
        if verdicts is None:
            return
        judge_meta = {}
        for c in criteria:
            v = verdicts.get(c["id"]) or arena_eval.verdict(c["id"], None, "no answer", "llm-judge")
            if c["kind"] == "judged":
                self._check(c["id"], check=c["text"], kind="judged", result={True: "pass", False: "fail"}.get(v["met"], "not_run"),
                            at_turn=turn, why=v["why"], evaluator=v["evaluator"], cost_eur=v["cost"], note=v.get("note") or None)
                judge_meta["expect"] = v["met"]
            elif c["kind"] == "success_when":
                self._check("success_when", check=c["text"], kind="success_when",
                            result="pass" if v["met"] else "fail", at_turn=turn if v["met"] else None,
                            why=v["why"], evaluator=v["evaluator"], cost_eur=v["cost"], note=v.get("note") or None)
                judge_meta["success"] = v["met"]
            else:
                triggered = v["met"] is True                   # a triggered abort stops the case at once
                self._check("abort_when", check=c["text"], kind="abort_when",
                            result="triggered" if triggered else "not_triggered",
                            at_turn=turn if triggered else None, why=v["why"],
                            evaluator=v["evaluator"], cost_eur=v["cost"], note=v.get("note") or None)
                judge_meta["abort"] = v["met"]
            if v.get("why"):
                judge_meta["why"] = v["why"]
        rudi_entry["meta"]["judge"] = judge_meta

    def _evaluate(self, criteria):
        """Run the evaluators with step retries and a budget check. None when judging could not
        happen (the case has then been stopped)."""
        tin = 700 + est_tokens(self._history_chars()) + sum(est_tokens(c["text"]) for c in criteria)
        if not self._affordable("judge", tin, 700):
            self._stop("case_budget")
            return None
        context = {"transcript": self.cp["transcript"], "persona_name": self._persona_name()}
        try:
            verdicts, metas = self._with_retries("judge_error", lambda: self.registry.evaluate(criteria, context))
        except StepFailed as f:
            self._stop(f.code)
            return None
        for m in metas:
            self.spend.add("judge", m.get("cost") or 0.0)
            self._spent_on(m.get("model") or m.get("evaluator"), m.get("cost") or 0.0)
        for v in verdicts.values():
            self.cp["tokens"][0] += int((v.get("tokens") or [0, 0])[0])
            self.cp["tokens"][1] += int((v.get("tokens") or [0, 0])[1])
        return verdicts

    def _check_stops(self, turn, phase):
        cp, spec = self.cp, self.spec
        stop = spec.get("stop") or {}
        if cp["stop"] is not None:
            return
        if (cp["checks"].get("abort_when") or {}).get("result") == "triggered":
            return self._stop("abort_criterion", turn)
        if (cp["checks"].get("success_when") or {}).get("result") == "pass":
            return self._stop("success_criterion", turn)
        if phase == "concluded" and spec.get("stop_on_rudi_conclude", True):
            return self._stop("rudi_concluded", turn)
        if stop.get("max_attempts") and cp["attempts"] >= stop["max_attempts"]:
            return self._stop("max_attempts", turn)

    # ------------------------------------------------------------------ calls with retries
    def _call_waiting(self, fn):
        """One call; if every entry is merely at its limit and one frees up soon, wait for it —
        that is pacing, not an error."""
        try:
            return fn()
        except arena_llm.Busy as b:
            wait = b.retry_at - self.d.clock()
            if wait > BUSY_WAIT_MAX_S:
                raise
            self.d.sleep(max(0.0, wait) + 0.5)
            return fn()

    def _with_retries(self, code, fn):
        """Retry a tester / judge step that failed mid-flight, with backoff. The cascade has
        already failed over between entries; this is the step-level second line."""
        s = self.cp["settings"]
        retries, backoff = int(s.get("retries") or 0), float(s.get("backoff") or 4)
        last = None
        for attempt in range(retries + 1):
            try:
                return self._call_waiting(fn)
            except arena_tester.TesterError as te:
                if te.result is not None:
                    self.spend.add("tester", te.result.cost)        # an unusable answer is still paid for
                last = te
            except Exception as err:  # noqa: BLE001 - cascade failures, adapters, network blips alike
                last = err
            final = attempt >= retries
            delay = backoff * (2 ** attempt)
            self._error(code, "%s: %s" % (type(last).__name__, last), retried_after_ms=None if final else int(delay * 1000))
            if not final:
                self.cp["retries"] += 1
                self._save()
                self.d.sleep(delay)
        raise StepFailed(code, str(last))

    def _engine(self, payload, retries):
        backoff = float(self.cp["settings"].get("backoff") or 4)
        timeout = int(self.spec["limits"].get("turn_timeout_s") or self.cp["settings"].get("turn_timeout") or 60)
        for attempt in range(int(retries) + 1):
            try:
                out = self.d.engine(payload, timeout)
            except EngineTimeout:
                out = {"ok": False, "error": "timeout", "detail": "no reply within %d s" % timeout}
            if out.get("ok"):
                self.cp["rl_streak"] = 0
                return out
            code = out.get("error") or "engine_failed"
            if code == "rate_limited":
                self.cp["rl_streak"] += 1
                if self.cp["settings"].get("breaker") and self.cp["rl_streak"] >= BREAKER_STREAK:
                    until = self.d.clock() + 60 * float(self.cp["settings"].get("breaker_min") or 10)
                    self.store.put_runtime("breaker", {"until": until, "by": self.cp["case_id"], "at": iso()})
                    log("breaker.open", case=self.cp["case_id"], minutes=self.cp["settings"].get("breaker_min"))
            final = code in ("bad_cascade", "bad_request") or attempt >= int(retries)
            delay = backoff * (2 ** attempt)
            self._error(code, out.get("detail"), retried_after_ms=None if final else int(delay * 1000))
            if final:
                raise StepFailed("timeout" if code == "timeout" else "engine_error", out.get("detail") or code)
            self.cp["retries"] += 1
            self._save()
            self.d.sleep(delay)
        raise StepFailed("engine_error", "unreachable")

    # ------------------------------------------------------------------ the end
    def _finish(self):
        cp, spec = self.cp, self.spec
        stop = cp["stop"] or {"reason": "script_end", "at_turn": cp["turn"]}
        reason = stop["reason"]
        if spec.get("expect") and reason not in ("cancelled", "case_budget") + TERMINAL_ERRORS:
            criteria = [{"id": "expect-end-%d" % i, "kind": "judged", "text": c["text"], "evaluator": c["evaluator"]}
                        for i, c in enumerate(spec["expect"], start=1)]
            verdicts = self._evaluate(criteria)
            if verdicts is None:
                reason = (cp["stop"] or {}).get("reason", reason)
                for c in criteria:
                    self._check(c["id"], check=c["text"], kind="judged", result="not_run", why="not judged: %s" % reason)
            else:
                for c in criteria:
                    v = verdicts.get(c["id"]) or arena_eval.verdict(c["id"], None, "no answer", "llm-judge")
                    self._check(c["id"], check=c["text"], kind="judged", result={True: "pass", False: "fail"}.get(v["met"], "not_run"),
                                why=v["why"], evaluator=v["evaluator"], cost_eur=v["cost"], note=v.get("note") or None)
        outcome = self._outcome(reason)
        record = self._record(outcome, reason, stop.get("at_turn"))
        text = arena_results.render_case(record)
        record["row"]["size"] = len(text)
        self.store.put_result(self.bid, self.seq, record, text)
        self.store.delete_checkpoint(self.bid, self.seq)
        self._notify(record["row"])
        log("case.finished", case=cp["case_id"], outcome=outcome, stop=reason, turns=cp["turn"],
            eur=round(self.spend.total, 6), retries=cp["retries"])

    def _outcome(self, reason):
        if reason == "cancelled":
            return "cancelled"
        if reason in ("case_budget", "batch_budget"):
            return "over_budget"
        if reason in TERMINAL_ERRORS:
            return "error"
        if reason == "abort_criterion":
            return "violation"
        if reason == "success_criterion":
            return "success"
        checks = [c for k, c in self.cp["checks"].items() if c.get("kind") in ("phase", "judged")]
        failed = any(c.get("result") == "fail" for c in checks)
        stop = self.spec.get("stop") or {}
        criteria_case = self.spec["mode"] != "scripted" and (stop.get("success_when") or stop.get("abort_when") or stop.get("on_limit") == "pass")
        if not criteria_case:
            if not checks:
                return "completed"
            return "failed" if failed or any(c.get("result") == "not_run" for c in checks) else "success"
        if failed:
            return "failed"
        if stop.get("on_limit") == "pass" and (self.cp["checks"].get("abort_when") or {}).get("result") != "triggered":
            self._check("limit", check="on_limit: pass — %s reached without a breach" % reason, kind="limit",
                        result="pass", at_turn=self.cp["turn"])
            return "success"
        return "failed"

    def _verdict(self, outcome, reason, at_turn):
        ch = self.cp["checks"]
        if reason == "success_criterion":
            return "success_when met at turn %s: %s" % (at_turn, (ch.get("success_when") or {}).get("why", ""))
        if reason == "abort_criterion":
            return "abort_when triggered at turn %s: %s" % (at_turn, (ch.get("abort_when") or {}).get("why", ""))
        if outcome == "over_budget":
            return "Stopped at €%.4f of its €%.2f ceiling, before the next model call." % (self.spend.total, self._cap())
        if outcome == "error":
            last = self.cp["errors"][-1] if self.cp["errors"] else {}
            return "Stopped by %s at turn %s after %d retries: %s" % (last.get("code", reason), at_turn, self.cp["retries"], last.get("detail", ""))
        if outcome == "cancelled":
            return "Cancelled at turn %s." % at_turn
        judged = [c for c in ch.values() if c.get("kind") in ("phase", "judged")]
        if outcome == "completed":
            return "%d messages answered; no criteria to judge." % self.cp["turn"]
        if (ch.get("limit") or {}).get("result") == "pass":
            return "Held for %d turns (%d attempts) without a breach." % (self.cp["turn"], self.cp["attempts"])
        if judged:
            passed = sum(1 for c in judged if c.get("result") == "pass")
            first_bad = next((c for c in judged if c.get("result") != "pass"), None)
            return "%d of %d checks passed.%s" % (passed, len(judged), (" First not passed: %s" % first_bad["check"]) if first_bad else "")
        return "success_when not met within %d turns (stopped by %s)." % (self.cp["turn"], reason)

    def _record(self, outcome, reason, at_turn):
        cp, spec = self.cp, self.spec
        persona = spec.get("persona") or {}
        finished = iso()
        dur = 0
        a, b = parse_iso(cp["started_at"]), parse_iso(finished)
        if a and b:
            dur = int((b - a).total_seconds())
        checks = []
        for c in cp["checks"].values():
            item = {"check": c.get("check"), "kind": c.get("kind"), "result": c.get("result")}
            for k in ("at_turn", "why", "evaluator", "note"):
                if c.get(k) not in (None, ""):
                    item[k] = c[k]
            if c.get("cost_eur"):
                item["cost_eur"] = c["cost_eur"]
            checks.append(item)
        lat = cp["latencies"]
        user_turns = sum(1 for e in cp["transcript"] if e["from"] == "user")
        rudi_turns = sum(1 for e in cp["transcript"] if e["from"] == "rudi")
        spend = self.spend.to_dict()
        total = round(self.spend.total, 6)
        cost = {"eur": total, "cap_eur": self._cap(), "by_role": spend, "tokens": {"in": cp["tokens"][0], "out": cp["tokens"][1]}}
        err_codes = {}
        for e in cp["errors"]:
            err_codes[e["code"]] = err_codes.get(e["code"], 0) + 1
        case = {"id": cp["case_id"], "key": spec["key"], "title": spec.get("title") or "", "mode": spec["mode"],
                "channel": "chat", "locale": {"requested": spec["locale"], "detected": cp["lang"]},
                "persona": {"id": persona.get("id"), "display_name": persona.get("display_name")} if persona else {},
                "rudi": {"engine": "live", "personality": (spec.get("rudi") or {}).get("personality") or "default"},
                "tags": spec.get("tags") or []}
        result = {"outcome": outcome, "stop_reason": reason, "verdict": self._verdict(outcome, reason, at_turn),
                  "checks": checks,
                  "counts": {"user_turns": user_turns, "rudi_turns": rudi_turns, "attempts": cp["attempts"], "retries": cp["retries"]},
                  "cost": cost,
                  "timing": {"started_at": cp["started_at"], "finished_at": finished, "duration_s": dur,
                             "rudi_latency_ms": {"p50": _median(lat), "max": max(lat) if lat else None}},
                  "final_state": cp["final_state"], "errors": cp["errors"]}
        row = {"seq": self.seq, "id": cp["case_id"], "key": spec["key"], "title": spec.get("title") or "", "mode": spec["mode"],
               "loc": spec["locale"], "persona": persona.get("display_name") or "", "outcome": outcome, "stop": reason,
               "turns": user_turns, "dur": dur, "errs": len(cp["errors"]), "cost": total, "cap": self._cap(), "roles": spend,
               "lat": lat, "models": cp["models"], "err_codes": err_codes, "lang": cp["lang"], "guard": cp["guard"],
               "tokens": cp["tokens"], "tags": spec.get("tags") or [], "finished_at": finished,
               "cost_models": cp.get("cost_models") or {},
               "engine": {"release": (cp["engine"] or {}).get("release"), "assets": (cp["engine"] or {}).get("assets") or {}}}
        return {"case": case, "result": result, "transcript": cp["transcript"], "row": row}

    def _notify(self, row):
        self.d.notify({"type": "finished", "batch": self.bid, "seq": self.seq, "attempt": self.attempt, "row": row})


# --------------------------------------------------------------------------- Lambda wiring
def _lambda_deps(context):
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ReadTimeoutError, ConnectTimeoutError

    s3 = boto3.client("s3")
    sqs = boto3.client("sqs")
    secrets = arena_llm.Secrets(boto3.client("secretsmanager"))
    store = ArenaStore(s3, ARENA_BUCKET)
    tracker = arena_llm.LimitTracker(store.b)
    clients = {}

    def engine(payload, timeout):
        client = clients.get(timeout)
        if client is None:
            client = clients[timeout] = boto3.client("lambda", config=Config(
                read_timeout=timeout + 5, connect_timeout=5, retries={"max_attempts": 0}))
        try:
            resp = client.invoke(FunctionName=ENGINE_FUNCTION, InvocationType="RequestResponse",
                                 Payload=json.dumps(payload).encode("utf-8"))
        except (ReadTimeoutError, ConnectTimeoutError):
            raise EngineTimeout()
        body = resp["Payload"].read()
        if resp.get("FunctionError"):
            return {"ok": False, "error": "engine_failed", "detail": "function error"}
        return json.loads(body or b"{}")

    def notify(msg):
        key = "fin-%s-%s-%s" % (msg["batch"], msg["seq"], msg["attempt"])
        sqs.send_message(QueueUrl=DISPATCH_QUEUE_URL, MessageBody=json.dumps(msg), MessageGroupId="dispatch",
                         MessageDeduplicationId=key)

    def requeue(msg):
        sqs.send_message(QueueUrl=CASES_QUEUE_URL, MessageBody=json.dumps(msg))

    def cascade_for(role, entries):
        return arena_llm.Cascade(role, entries, tracker, secrets)

    deps = Deps(store, engine, notify, requeue, cascade_for,
                remaining_ms=(context.get_remaining_time_in_millis if context else (lambda: 900_000)))
    deps.secrets = secrets
    return deps


def handler(event, context=None):
    deps = _lambda_deps(context)
    for record in event.get("Records", []):
        msg = json.loads(record["body"])
        CaseRun(deps, msg).run()
    return {"ok": True}
