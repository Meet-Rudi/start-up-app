"""
MEET_RUDI Training Arena — evaluators: who decides whether a criterion was met.

Every criterion (`expect`, `success_when`, `abort_when`) is routed to an evaluator:

  llm-judge       an LLM reads the transcript and the criteria and answers {met, why} for each.
                  Runs on the arena's judge cascade (arena_llm), with preventive failover.
  decision models non-generative models behind an HTTP API. Two families:
                  - System One decision models (TypeSafe Jev, eu/jev, Cloudflare Clef): the
                    transcript is the `state`, each criterion a yes/no (`noul`) question, and the
                    answer a probability. Several criteria go in one request. Probabilities near
                    0.5 are escalated to the LLM judge (`escalate_below`).
                  - classifiers (moderation APIs, guard models, text classification): a label
                    and score about one text, turned into met / not met by a `rule`.
                  Cheaper, faster and more repeatable than an LLM judge where the criterion fits;
                  weaker where the verdict has to be reasoned out.
  phase           deterministic: Rudi's conversation phase after a reply (expect_phase).

Routing, in order: the evaluator a criterion names (`{text, evaluator: name}` in the input), then
the first `routes` pattern in config/evaluators.json that matches the criterion text, then the
default (llm-judge). A named or routed evaluator that is disabled falls back to the default, and
the verdict says so.

Decision-model providers are not chosen yet: the adapters below are ready and tested against
fakes, and config ships with none enabled.
"""

import re
import json
import time

import arena_costs
import arena_llm

DEFAULT = "llm-judge"
MAX_TRANSCRIPT_MESSAGES = 40


def verdict(cid, met, why, evaluator, cost=0.0, tokens=(0, 0), note=""):
    return {"id": cid, "met": met, "why": (why or "")[:300], "evaluator": evaluator,
            "cost": float(cost or 0.0), "tokens": list(tokens), "note": note}


def transcript_text(transcript, persona_name="User"):
    lines = []
    for e in transcript[-MAX_TRANSCRIPT_MESSAGES:]:
        who = "Rudi" if e.get("from") == "rudi" else persona_name
        lines.append("[turn %s] %s: %s" % (e.get("turn"), who, e.get("text") or ""))
    return "\n".join(lines)


class LLMJudge:
    kind = "llm_judge"

    def __init__(self, name, cascade, system_prompt, max_tokens=700):
        self.name, self.cascade, self.system, self.max_tokens = name, cascade, system_prompt, max_tokens

    def evaluate(self, criteria, context):
        """criteria: [{"id", "kind", "text"}]; context: {"transcript", "persona_name"}."""
        payload = {"criteria": [{"id": c["id"], "type": c["kind"], "criterion": c["text"]} for c in criteria],
                   "transcript": transcript_text(context["transcript"], context.get("persona_name") or "User")}
        messages = [{"role": "system", "content": self.system},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
        res = self.cascade.chat(messages, json_mode=True, max_tokens=self.max_tokens, temperature=0)
        obj = arena_llm.parse_json_reply(res.text)
        answers = {str(a.get("id")): a for a in obj.get("results") or [] if isinstance(a, dict)}
        share = res.cost / max(1, len(criteria))
        out = []
        for c in criteria:
            a = answers.get(c["id"])
            met = a.get("met") if a and isinstance(a.get("met"), bool) else None
            out.append(verdict(c["id"], met, (a or {}).get("why") or ("" if a else "the judge gave no answer"),
                               self.name, share, (res.tokens_in // max(1, len(criteria)), res.tokens_out // max(1, len(criteria)))))
        return out, {"model": res.model, "cost": res.cost, "attempts": res.attempts, "skipped": res.skipped}


class DecisionModel:
    """A decision model or classifier behind HTTP. Config (config/evaluators.json):

        {"name": "eu-jev", "kind": "decision_model", "api": "system_one", "endpoint": "...",
         "model": "...", "key": "meetrudi/arena/...", "on": false, "eu": true,
         "threshold": 0.5, "escalate_below": 0.6, "in": 0.037, "out": 0.0}

        {"name": "guard-self-harm", "kind": "decision_model",
         "api": "moderation" | "text_classification" | "chat_classifier", "endpoint": "...", "model": "...",
         "key": "...", "on": false, "eu": true, "input": "rudi_reply" | "user_message" | "transcript",
         "rule": {"label": "self-harm", "min_score": 0.5, "met_when": "present" | "absent"},
         "per_call_eur": 0.0}

    `eu: false` marks a provider outside the EU; such an entry stays off unless it also carries
    `signed_off: true` (CLAUDE.md §0.3: every third-party service EU-region and under a DPA).
    """
    MAX_QUESTIONS = 8              # eu/jev's per-request ceiling; TypeSafe allows more
    MAX_INSTRUCTION = 1000
    MAX_STATE = 200_000
    kind = "decision_model"

    def __init__(self, cfg, secrets, transport=None, clock=time.time):
        self.cfg, self.name = cfg, cfg["name"]
        self.secrets = secrets
        self.transport = transport or arena_llm.HttpTransport()
        self.clock = clock

    def _text(self, context):
        which = self.cfg.get("input") or "rudi_reply"
        t = context["transcript"]
        if which == "transcript":
            return transcript_text(t, context.get("persona_name") or "User")
        want = "rudi" if which == "rudi_reply" else "user"
        for e in reversed(t):
            if e.get("from") == want:
                return e.get("text") or ""
        return ""

    def _labels(self, body, reply_text=""):
        """{label: score} from the three API shapes."""
        api = self.cfg.get("api")
        if api == "moderation":
            r = (body.get("results") or [{}])[0]
            scores = {k.lower(): float(v) for k, v in (r.get("category_scores") or {}).items()}
            for k, v in (r.get("categories") or {}).items():
                scores.setdefault(k.lower(), 1.0 if v else 0.0)
            return scores
        if api == "text_classification":
            items = body[0] if isinstance(body, list) and body and isinstance(body[0], list) else body
            return {str(i.get("label", "")).lower(): float(i.get("score", 0.0)) for i in items or [] if isinstance(i, dict)}
        if api == "chat_classifier":
            # Guard-style answers ("unsafe\nS11") or JSON policy verdicts: every word becomes a label.
            return {w.lower(): 1.0 for w in re.findall(r"[A-Za-z0-9_-]+", reply_text or "")}
        raise ValueError("unknown decision-model api %r" % api)

    def _system_one(self, criteria, context):
        """State = the transcript; one yes/no (`noul`) question per criterion, 8 to a request."""
        cfg = self.cfg
        state = transcript_text(context["transcript"], context.get("persona_name") or "User")[-self.MAX_STATE:]
        threshold = float(cfg.get("threshold", 0.5))
        out, total, tin, tout = [], 0.0, 0, 0
        for i in range(0, len(criteria), self.MAX_QUESTIONS):
            chunk = criteria[i:i + self.MAX_QUESTIONS]
            questions = {}
            for c in chunk:
                questions[c["id"]] = {"type": "noul", "instructions": (
                    "In this conversation between a user and Rudi, an AI coach: " + c["text"])[:self.MAX_INSTRUCTION]}
            payload = {"state": state, "questions": questions}
            if cfg.get("model"):
                payload["model"] = cfg["model"]
            body, _headers = self.transport.post(cfg["endpoint"], self.secrets.get(cfg["key"]), payload, 30)
            usage = body.get("usage") or {}
            ci, co = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
            cost = float((body.get("meta") or {}).get("cost_eur") or 0.0) or arena_costs.call_cost(cfg, ci, co)
            total, tin, tout = total + cost, tin + ci, tout + co
            answers = body.get("answers") or {}
            for c in chunk:
                p = (answers.get(c["id"]) or {}).get("noul")
                if not isinstance(p, (int, float)):
                    out.append(verdict(c["id"], None, "no answer from %s" % self.name, self.name))
                    continue
                v = verdict(c["id"], p >= threshold, "p(yes) = %.2f" % p, self.name)
                v["confidence"] = round(abs(float(p) - 0.5) * 2, 3)     # no confidence is returned for yes/no
                out.append(v)
        for v in out:
            v["cost"] = total / max(1, len(out))
        return out, {"model": cfg.get("model") or self.name, "cost": total, "tokens": [tin, tout]}

    def evaluate(self, criteria, context):
        if self.cfg.get("api") == "system_one":
            return self._system_one(criteria, context)
        text = self._text(context)
        cfg = self.cfg
        if cfg.get("api") == "chat_classifier":
            payload = {"model": cfg["model"], "messages": [{"role": "user", "content": text}]}
        elif cfg.get("api") == "moderation":
            payload = {"model": cfg["model"], "input": text}
        else:
            payload = {"inputs": text}
        body, _headers = self.transport.post(cfg["endpoint"], self.secrets.get(cfg["key"]), payload, 30)
        reply = arena_llm._content(body) if cfg.get("api") == "chat_classifier" else ""
        labels = self._labels(body, reply)
        rule = cfg.get("rule") or {}
        score = labels.get(str(rule.get("label", "")).lower(), 0.0)
        present = score >= float(rule.get("min_score", 0.5))
        met = present if rule.get("met_when", "present") == "present" else not present
        cost = float(cfg.get("per_call_eur") or 0.0) or arena_costs.call_cost(
            cfg, arena_llm.est_tokens(text), arena_llm.est_tokens(reply))
        share = cost / max(1, len(criteria))
        why = "%s %s (score %.2f)" % (rule.get("label"), "present" if present else "absent", score)
        return [verdict(c["id"], met, why, self.name, share) for c in criteria], {"model": cfg.get("model"), "cost": cost}


class Phase:
    """expect_phase: compares Rudi's phase after the reply. Free and exact."""
    name = kind = "phase"

    @staticmethod
    def check(expected, actual):
        return verdict("phase", expected == actual, "phase is %s" % actual, "phase")


class Registry:
    def __init__(self, config, default):
        self.config = config or {}
        self.default = default
        self.models = {}
        self.routes = []
        for r in self.config.get("routes") or []:
            try:
                self.routes.append((re.compile(r["match"], re.I), r["evaluator"]))
            except (KeyError, re.error):
                continue

    def add(self, evaluator, enabled=True):
        self.models[evaluator.name] = (evaluator, enabled)

    def names(self):
        return {DEFAULT} | {e.get("name") for e in self.config.get("evaluators") or [] if e.get("name")}

    def route(self, crit):
        """(evaluator, note). A disabled or unknown choice falls back to the default, noted."""
        wanted = crit.get("evaluator") or "auto"
        if wanted == "auto":
            for rx, name in self.routes:
                if rx.search(crit.get("text") or ""):
                    wanted = name
                    break
        if wanted in ("auto", DEFAULT):
            return self.default, ""
        ev = self.models.get(wanted)
        if ev and ev[1]:
            return ev[0], ""
        return self.default, "%s is not enabled; judged by %s" % (wanted, DEFAULT)

    def evaluate(self, criteria, context):
        """Group criteria by evaluator, one call each. A decision model's answer that is too close
        to a coin toss (`escalate_below`) goes to the default judge instead. Returns
        ({id: verdict}, [call metas])."""
        groups, notes = {}, {}
        for c in criteria:
            ev, note = self.route(c)
            groups.setdefault(ev.name, (ev, []))[1].append(c)
            if note:
                notes[c["id"]] = note
        out, metas, escalate = {}, [], []
        for _name, (ev, items) in groups.items():
            verdicts, meta = ev.evaluate(items, context)
            metas.append(dict(meta, evaluator=ev.name))
            floor = float(getattr(ev, "cfg", {}).get("escalate_below") or 0)
            for v in verdicts:
                if notes.get(v["id"]):
                    v["note"] = notes[v["id"]]
                out[v["id"]] = v
                if ev is not self.default and (v["met"] is None or ("confidence" in v and v["confidence"] < floor)):
                    escalate.append((next(c for c in items if c["id"] == v["id"]), v))
        if escalate:
            verdicts, meta = self.default.evaluate([c for c, _ in escalate], context)
            metas.append(dict(meta, evaluator=self.default.name))
            first = {c["id"]: v for c, v in escalate}
            for v in verdicts:
                v["note"] = "escalated from %s (%s)" % (first[v["id"]]["evaluator"], first[v["id"]]["why"])
                v["cost"] += first[v["id"]]["cost"]
                out[v["id"]] = v
        return out, metas


def build(config, judge_cascade, judge_prompt, secrets, transport=None):
    """The registry from config/evaluators.json and the judge cascade."""
    judge = LLMJudge(DEFAULT, judge_cascade, judge_prompt,
                     max_tokens=int(((config or {}).get("llm_judge") or {}).get("max_tokens") or 700))
    reg = Registry(config, judge)
    for e in (config or {}).get("evaluators") or []:
        if e.get("kind") == "decision_model" and e.get("name"):
            allowed = e.get("eu", True) is not False or e.get("signed_off") is True
            reg.add(DecisionModel(e, secrets, transport), enabled=bool(e.get("on")) and allowed)
    return reg
