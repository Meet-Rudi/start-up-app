"""
MEET_RUDI Training Arena — meetrudi-arena-ingest: validate a batch input, then lay out its cases.

Invoked asynchronously by the API.

    {"batch": id, "mode": "validate"}   stream the input, check every rule, estimate the run, write
                                        validation.json; the batch becomes ready or invalid
    {"batch": id, "mode": "launch"}     stream it again, write one spec per case and the batch
                                        index, then tell the dispatcher; resumable (from_seq) so
                                        an input of any size fits in Lambda's 15 minutes

Identifiers are checked by the deployed engine's own detector (meetrudi-engine-turn, op scan),
and personality names against the engine's list (op info) — the arena keeps no copy of either.
Until the batch is launched, ingest is the only writer of its manifest.
"""

import os
import json
import hashlib

import arena_format
from arena_common import iso, log, case_id
from arena_store import ArenaStore, BLOCK

ARENA_BUCKET = os.environ.get("ARENA_BUCKET", "")
ENGINE_FUNCTION = os.environ.get("ENGINE_FUNCTION", "meetrudi-engine-turn")
DISPATCH_QUEUE_URL = os.environ.get("DISPATCH_QUEUE_URL", "")
SELF_FUNCTION = os.environ.get("INGEST_FUNCTION", "meetrudi-arena-ingest")
MIN_REMAINING_MS = 60_000


class _Hashing:
    """Pass-through reader that hashes what it reads, so the SHA-256 costs no second pass."""

    def __init__(self, raw):
        self.raw, self.h = raw, hashlib.sha256()

    def read(self, n=-1):
        data = self.raw.read(n) if n is not None and n >= 0 else self.raw.read()
        self.h.update(data if isinstance(data, bytes) else data.encode("utf-8"))
        return data


class Ingest:
    def __init__(self, store, open_input, engine, notify, reinvoke, remaining_ms=lambda: 900_000):
        self.store, self.open_input, self.engine = store, open_input, engine
        self.notify, self.reinvoke, self.remaining_ms = notify, reinvoke, remaining_ms

    def _scan(self, texts):
        out = self.engine({"op": "scan", "texts": texts})
        if not out.get("ok"):
            raise RuntimeError(out.get("error") or "scan failed")
        return out["found"]

    def _validator(self, m):
        st = self.store
        info = self.engine({"op": "info"})
        personalities = info.get("personalities") if info.get("ok") else None
        return arena_format.Validator(st.engine_settings(), st.budgets(), st.cascades(), personalities=personalities,
                                      evaluators=_evaluator_names(st.evaluators()), scan=self._scan,
                                      only_keys=m.get("only_keys"))

    def run(self, event):
        bid, mode = event["batch"], event.get("mode") or "validate"
        m = self.store.manifest(bid)
        if not m:
            return {"ok": False, "error": "unknown batch"}
        return self._validate(m) if mode == "validate" else self._launch(m, int(event.get("from_seq") or 1))

    def _validate(self, m):
        st = self.store
        m["status"] = "validating"
        st.put_manifest(m)
        stream = _Hashing(self.open_input(m["input_key"]))
        report = self._validator(m).run(arena_format.documents(stream, m["input_file"]))
        stream.read()                                   # hash whatever the parser did not need
        m["input_sha256"] = stream.h.hexdigest()
        report["checked_at"] = iso()
        st.put_validation(m["id"], report)
        b = report["budget"]
        m.update({"status": "ready" if report["ok"] else "invalid", "total": report["cases"],
                  "validation": {k: report[k] for k in ("ok", "errors", "warnings", "cases", "cost_eur", "duration_s", "rudi_replies")},
                  "modes": report["by_mode"], "locales": report["by_locale"], "cap": b["batch"], "caseCap": b["case"],
                  "budget_src": "%s / %s" % (b["batch_src"], b["case_src"]),
                  "c": {k: 0 for k in ("success", "failed", "violation", "error", "completed", "cancelled", "over_budget", "running")}})
        m["c"]["queued"] = report["cases"]
        if not m.get("name_from_console"):
            from arena_common import slug, file_stem
            m["name"] = slug(report["batch"]["name"]) if report["batch"]["name"] else m.get("name") or ""
            m["stem"] = file_stem(m["id"], m["name"])
        m["desc"] = m.get("desc") or report["batch"]["description"]
        m["tags"] = m.get("tags") or report["batch"]["tags"]
        st.put_manifest(m)
        log("ingest.validated", batch=m["id"], ok=report["ok"], cases=report["cases"], errors=report["errors"],
            warnings=report["warnings"], est_eur=report["cost_eur"])
        return {"ok": report["ok"]}

    def _launch(self, m, from_seq):
        st = self.store
        if m.get("status") not in ("ready", "launching"):
            return {"ok": False, "error": "batch is %s, not ready" % m.get("status")}
        m["status"] = "launching"
        st.put_manifest(m)
        total = int(m["total"])
        blocks, state = {}, {"seq": 0, "stopped": False}

        def flush():
            for block, rows in blocks.items():
                current = st.index_block(m["id"], block)
                current.update(rows)
                st.put_index_block(m["id"], block, current)
            blocks.clear()

        def on_case(spec):
            seq = spec["seq"]
            state["seq"] = seq
            if seq < from_seq or state["stopped"]:
                return
            if self.remaining_ms() < MIN_REMAINING_MS:
                state["stopped"] = True
                return
            spec["id"] = case_id(m["id"], seq, total)
            st.put_spec(m["id"], seq, spec)
            persona = spec.get("persona") or {}
            blocks.setdefault((seq - 1) // BLOCK, {})[str(seq)] = {
                "seq": seq, "id": spec["id"], "key": spec["key"], "title": spec.get("title") or "", "mode": spec["mode"],
                "loc": spec["locale"], "persona": persona.get("display_name") or "", "outcome": "queued", "stop": "—",
                "turns": 0, "dur": 0, "errs": 0, "cost": 0.0, "cap": spec["limits"]["max_cost_eur"], "tags": spec.get("tags") or []}
            if sum(len(v) for v in blocks.values()) >= 200:
                flush()

        report = self._validator(m).run(arena_format.documents(self.open_input(m["input_key"]), m["input_file"]), on_case=on_case)
        flush()
        if not report["ok"]:
            m["status"] = "invalid"
            st.put_manifest(m)
            return {"ok": False, "error": "the input no longer validates"}
        if state["stopped"]:
            resume = state["seq"] if state["seq"] >= from_seq else from_seq
            self.reinvoke({"batch": m["id"], "mode": "launch", "from_seq": resume})
            log("ingest.continue_later", batch=m["id"], next_seq=resume)
            return {"ok": True, "continued": True}
        settings, budgets, cascades = st.engine_settings(), st.budgets(), st.cascades()
        m.update({"status": "queued", "cursor": 0, "spent": 0.0, "reserved": 0.0,
                  "roles": {"rudi": 0.0, "tester": 0.0, "judge": 0.0, "infra": 0.0},
                  "settings": settings, "budget_snapshot": budgets, "cascades": cascades,
                  "evaluators": sorted(_evaluator_names(st.evaluators(), enabled_only=True)),
                  "launched_at": iso(), "engine": {}})
        st.put_manifest(m)
        self.notify({"type": "launch", "batch": m["id"]})
        log("ingest.launched", batch=m["id"], cases=total)
        return {"ok": True}


def _evaluator_names(cfg, enabled_only=False):
    names = {"llm-judge"}
    for e in (cfg or {}).get("evaluators") or []:
        if e.get("name") and (e.get("on") or not enabled_only):
            names.add(e["name"])
    return names


def handler(event, context=None):
    import boto3
    s3 = boto3.client("s3")
    sqs = boto3.client("sqs")
    lam = boto3.client("lambda")
    store = ArenaStore(s3, ARENA_BUCKET)

    def open_input(key):
        return s3.get_object(Bucket=ARENA_BUCKET, Key=key)["Body"]

    def engine(payload):
        # Unreachable engine (not deployed yet, no permission): the scan reports it as a validation
        # error and the batch becomes invalid with a clear message, instead of hanging.
        try:
            resp = lam.invoke(FunctionName=ENGINE_FUNCTION, InvocationType="RequestResponse",
                              Payload=json.dumps(payload).encode("utf-8"))
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": "meetrudi-engine-turn unreachable (%s)" % type(e).__name__}
        if resp.get("FunctionError"):
            return {"ok": False, "error": "engine function error"}
        return json.loads(resp["Payload"].read() or b"{}")

    def notify(msg):
        sqs.send_message(QueueUrl=DISPATCH_QUEUE_URL, MessageBody=json.dumps(msg), MessageGroupId="dispatch",
                         MessageDeduplicationId="%s-%s" % (msg["type"], msg["batch"]))

    def reinvoke(payload):
        lam.invoke(FunctionName=SELF_FUNCTION, InvocationType="Event", Payload=json.dumps(payload).encode("utf-8"))

    remaining = context.get_remaining_time_in_millis if context else (lambda: 900_000)
    return Ingest(store, open_input, engine, notify, reinvoke, remaining).run(event)
