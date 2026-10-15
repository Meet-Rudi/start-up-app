"""
MEET_RUDI Training Arena — meetrudi-arena-extractor: runs one IMPROVEMENT analysis in AWS, for
whoever has no local Claude (arena_improve.py has the logic; the console's local path runs the
same code in the local generator on the user's own Claude subscription).

Invoked by the API with {"analysis": "<IM-id>"}. Reads the record (analyses/records/<id>.json),
the results file(s) it names and the skills catalogue (skills/rudi-skills.yaml, seeded on deploy),
asks the arena's own JUDGE cascade — Groq's free tier when the prompt fits its minute, otherwise the
arena's paid EU entry (OVH) — and writes the report next to the source:
analyses/<batch id | uploads>/<results stem>__improvements__<id>.{html,json,md}.

Budget: a ceiling per analysis (estimated before the call, at the dearest entry) and the arena's
month budget, counting what analyses already spent this month (stats/analyses/<yyyy-mm>.json —
this function's own file; the dispatcher's stats are not touched).
Logs: ids, sizes, timings, cost. Never the content.
"""

import datetime
import os
import time

import arena_costs
import arena_improve
import arena_llm
from arena_common import iso, log, est_tokens
from arena_store import ArenaStore

ARENA_BUCKET = os.environ.get("ARENA_BUCKET", "")
ANALYSIS_CEILING_EUR = float(os.environ.get("ANALYSIS_CEILING_EUR", "0.25"))
MAX_OUT = 8000
SYSTEM = "You review Training Arena results for a coaching product's engineering team. Answer with JSON only."


def record_key(aid):
    return "analyses/records/%s.json" % aid


def report_prefix(batch_id):
    return "analyses/%s/" % (batch_id or "uploads")


class Run:
    def __init__(self, store, judge, clock=time.time, catalogue_text=None):
        self.store, self.judge, self.clock = store, judge, clock
        self.catalogue_text = catalogue_text

    def _save(self, rec):
        rec["updated_at"] = iso()
        self.store.b.put_json(record_key(rec["id"]), rec)

    def _month_spent(self):
        month = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")
        total = 0.0
        for k in self.store.b.list_keys("stats/daily/%s" % month):
            day = self.store.b.get_json(k, {}) or {}
            total += sum(float(v or 0) for v in (day.get("roles") or {}).values())
        own = self.store.b.get_json("stats/analyses/%s.json" % month, {}) or {}
        return total + float(own.get("eur") or 0), month

    def _charge(self, month, eur):
        k = "stats/analyses/%s.json" % month
        own = self.store.b.get_json(k, {}) or {}
        own["eur"] = round(float(own.get("eur") or 0) + eur, 6)
        own["count"] = int(own.get("count") or 0) + 1
        self.store.b.put_json(k, own)

    def run(self, aid):
        rec = self.store.b.get_json(record_key(aid))
        if not rec:
            log("extract.missing", analysis=aid)
            return None
        if rec.get("status") in ("done", "failed"):
            return rec
        rec.update(status="running", started_at=iso(), steps=[])
        self._save(rec)
        spent = {"eur": 0.0, "model": None}

        def progress(text, state="run", t=""):
            steps = rec["steps"]
            if steps and steps[-1]["text"] == text:
                steps[-1].update(state=state, t=t)
            else:
                if steps and steps[-1]["state"] == "run":
                    steps[-1]["state"] = "done"
                steps.append({"text": text, "state": state, "t": t})
            self._save(rec)

        try:
            texts = []
            for key in rec["source"].get("keys") or []:
                t = self.store.b.get_text(key)
                if t is None:
                    raise arena_improve.ImproveError("The results file %s is no longer in the arena bucket." % key.rsplit("/", 1)[-1])
                texts.append(t)
            text = "\n".join(texts)
            cat = arena_improve.load_catalogue(self.catalogue_text if self.catalogue_text is not None
                                               else self.store.b.get_text("skills/rudi-skills.yaml"))
            budgets = self.store.budgets()
            month_spent, month = self._month_spent()

            def ask(prompt):
                tin = est_tokens(len(prompt) + len(SYSTEM))
                worst = arena_costs.expected_cost(self.judge.entries, tin, MAX_OUT)
                if worst > ANALYSIS_CEILING_EUR:
                    raise arena_improve.ImproveError("This run is too large to analyse within €%.2f (≈ €%.2f at worst). Analyse one results part at a time."
                                                     % (ANALYSIS_CEILING_EUR, worst))
                if month_spent + worst > float(budgets.get("month_budget") or 0) + 1e-9:
                    raise arena_improve.ImproveError("The arena's month budget is spent; analyses wait until next month or a super-admin raises it.")
                started = self.clock()
                for attempt in range(2):
                    try:
                        res = self.judge.chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                                              json_mode=True, max_tokens=MAX_OUT, temperature=0, timeout=280)
                        break
                    except arena_llm.Busy as b:
                        wait = b.retry_at - self.clock()
                        if attempt or wait > 90:
                            raise arena_improve.ImproveError("The arena's models are busy; try again in a few minutes.")
                        time.sleep(max(1.0, wait))
                spent["eur"] += res.cost
                spent["model"] = res.model
                log("extract.model", analysis=aid, model=res.model, tokens_in=res.tokens_in, tokens_out=res.tokens_out,
                    eur=round(res.cost, 6), ms=int((self.clock() - started) * 1000))
                return res.text

            out = arena_improve.analyze(text, rec["source"], ask, "The arena's model", aid, catalogue=cat, progress=progress)
            batch_id = out["meta"].get("batch_id")
            base = report_prefix(batch_id) + out["file"][:-len(".html")]
            out["meta"]["engine"] = "The arena's model (%s)" % (spent["model"] or "judge cascade")
            html = arena_improve.render_html(out["report"], out["meta"], out["cases"])
            self.store.b.put_text(base + ".html", html, "text/html; charset=utf-8")
            self.store.b.put_json(base + ".json", {"format": arena_improve.REPORT_FORMAT, "meta": out["meta"], "report": out["report"], "cases": out["cases"]})
            self.store.b.put_text(base + ".md", arena_improve.markdown(out["report"], out["meta"]), "text/markdown; charset=utf-8")
            if spent["eur"]:
                self._charge(month, spent["eur"])
            rec.update(status="done", finished_at=iso(), batch_id=batch_id, report_key=base + ".html", file=out["file"],
                       model=spent["model"], cost_eur=round(spent["eur"], 6), engine_label=out["meta"]["engine"],
                       counts={"errors": len(out["report"]["app_errors"]), "weaknesses": len(out["report"]["deficiencies"]),
                               "requests": len(out["report"]["feature_requests"])})
            log("extract.done", analysis=aid, batch=batch_id, eur=rec["cost_eur"], **rec["counts"])
        except (arena_improve.ImproveError, arena_llm.LLMError) as e:
            rec.update(status="failed", finished_at=iso(), error=str(e)[:400], cost_eur=round(spent["eur"], 6))
            log("extract.failed", analysis=aid, error=type(e).__name__)
        for s in rec.get("steps") or []:
            if s["state"] == "run":
                s["state"] = "done" if rec["status"] == "done" else "todo"
        self._save(rec)
        return rec


def _deps():
    import boto3
    store = ArenaStore(boto3.client("s3"), ARENA_BUCKET)
    secrets = arena_llm.Secrets(boto3.client("secretsmanager"))
    tracker = arena_llm.LimitTracker(store.b)
    judge = arena_llm.Cascade("judge", store.cascades().get("judge"), tracker, secrets)
    return store, judge


def handler(event, context=None):
    store, judge = _deps()
    return {"ok": True, "status": (Run(store, judge).run(event["analysis"]) or {}).get("status")}
