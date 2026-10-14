"""
MEET_RUDI Training Arena — meetrudi-arena-assembler: builds the results files of a batch.

Invoked by the dispatcher when a batch has no case left (or by the API for a snapshot of a running
batch). It never holds the results in memory:

  1. PLAN from the batch index alone — each finished case recorded the size of its rendered
     document — so the number of parts is final before anything is written;
  2. STREAM each part: header, then the case documents read from S3 one by one, written through a
     multipart upload in 8 MB chunks. Memory stays at one case plus one chunk;
  3. write the batch index CSV, then tell the dispatcher (which owns the manifest).

Parts are independent. With little time left the assembler re-invokes itself for the next part.
"""

import os
import json
import time

import arena_results
from arena_common import log, est_tokens, file_stem
from arena_store import ArenaStore

ARENA_BUCKET = os.environ.get("ARENA_BUCKET", "")
DISPATCH_QUEUE_URL = os.environ.get("DISPATCH_QUEUE_URL", "")
SELF_FUNCTION = os.environ.get("ASSEMBLER_FUNCTION", "meetrudi-arena-assembler")
ARENA_RELEASE = os.environ.get("RELEASE", "unknown")
CHUNK = 8 * 1024 * 1024
OUTCOME_LINE_CHARS = 140           # the case's line in its part header's `outcomes`
MIN_REMAINING_MS = 60_000


class S3Writer:
    """put_object for small files, multipart for big ones — never the whole file in memory."""

    def __init__(self, s3, bucket, key, content_type="application/yaml; charset=utf-8"):
        self.s3, self.bucket, self.key, self.ct = s3, bucket, key, content_type
        self.buf, self.parts, self.upload_id = bytearray(), [], None

    def write(self, text):
        self.buf += text.encode("utf-8")
        if len(self.buf) >= CHUNK:
            self._flush()

    def _flush(self):
        if self.upload_id is None:
            self.upload_id = self.s3.create_multipart_upload(Bucket=self.bucket, Key=self.key, ContentType=self.ct)["UploadId"]
        n = len(self.parts) + 1
        r = self.s3.upload_part(Bucket=self.bucket, Key=self.key, UploadId=self.upload_id, PartNumber=n, Body=bytes(self.buf))
        self.parts.append({"PartNumber": n, "ETag": r["ETag"]})
        self.buf = bytearray()

    def close(self):
        if self.upload_id is None:
            self.s3.put_object(Bucket=self.bucket, Key=self.key, Body=bytes(self.buf), ContentType=self.ct)
            return
        if self.buf:
            self._flush()
        self.s3.complete_multipart_upload(Bucket=self.bucket, Key=self.key, UploadId=self.upload_id,
                                          MultipartUpload={"Parts": self.parts})


def summary(manifest, rows):
    """The whole batch in figures, repeated in every part header."""
    cases = {k: 0 for k in ("success", "completed", "failed", "violation", "error", "over_budget", "cancelled")}
    lat, models, langs, codes = [], {}, {}, {}
    guard = {"leak_blocked": 0, "pii_redactions": 0}
    roles = {"rudi": 0.0, "tester": 0.0, "judge": 0.0, "infra": 0.0}
    by_model, tokens = {}, [0, 0]
    user_turns = rudi_turns = 0
    costs = []
    for r in rows:
        if r.get("outcome") in cases:
            cases[r["outcome"]] += 1
        lat += list(r.get("lat") or [])
        for k, v in (r.get("models") or {}).items():
            models[k] = models.get(k, 0) + v
        if r.get("lang"):
            langs[r["lang"]] = langs.get(r["lang"], 0) + 1
        for k, v in (r.get("err_codes") or {}).items():
            codes[k] = codes.get(k, 0) + v
        for k in guard:
            guard[k] += int((r.get("guard") or {}).get(k) or 0)
        for k, v in (r.get("roles") or {}).items():
            roles[k] = roles.get(k, 0.0) + float(v or 0)
        for k, v in (r.get("cost_models") or {}).items():
            by_model[k] = by_model.get(k, 0.0) + float(v or 0)
        tokens[0] += int((r.get("tokens") or [0, 0])[0])
        tokens[1] += int((r.get("tokens") or [0, 0])[1])
        user_turns += int(r.get("turns") or 0)
        if r.get("turns") or r.get("cost"):
            rudi_turns += int(r.get("turns") or 0) + 1
            costs.append(float(r.get("cost") or 0))
    lat.sort()

    def pick(q):
        return lat[min(len(lat) - 1, int(q * len(lat)))] if lat else None

    total = sum(roles.values())
    cap = float(manifest.get("cap") or 0)
    return {"cases": dict({"total": len(rows)}, **cases),
            "turns": {"user": user_turns, "rudi": rudi_turns},
            "rudi_latency_ms": {"p50": pick(0.5), "p95": pick(0.95), "max": lat[-1] if lat else None},
            "errors": {"total": sum(codes.values()), "by_code": codes},
            "models_served": models, "languages": langs, "guard": guard,
            "cost": {"total_eur": round(total, 6), "model_eur": round(total - roles.get("infra", 0.0), 6),
                     "infra_eur": round(roles.get("infra", 0.0), 6),
                     "by_role": {k: round(v, 6) for k, v in roles.items()},
                     "by_model": {k: round(v, 6) for k, v in sorted(by_model.items(), key=lambda x: -x[1])},
                     "tokens": {"in": tokens[0], "out": tokens[1]},
                     "per_case_eur": {"mean": round(total / len(costs), 6) if costs else 0.0, "max": round(max(costs), 6) if costs else 0.0},
                     "budget_used_pct": round(100 * total / cap, 1) if cap else None}}


def header_manifest(m):
    """The manifest fields the part header shows, in the documented shape."""
    s = m.get("settings") or {}
    started, finished = m.get("started_at"), m.get("finished_at")
    wall = None
    if started and finished:
        from arena_common import parse_iso
        a, b = parse_iso(started), parse_iso(finished)
        wall = int((b - a).total_seconds()) if a and b else None
    eng = m.get("engine") or {}
    return {"stem": m["stem"], "id": m["id"], "name": m.get("name") or "", "description": m.get("desc") or "",
            "tags": m.get("tags") or [], "input_file": m.get("input_file"), "input_sha256": m.get("input_sha256"),
            "submitted_by": m.get("by"), "submitted_at": m.get("submitted_at"), "started_at": started,
            "finished_at": finished, "wall_clock_s": wall, "status": m.get("final_status") or m.get("status"),
            "budget": {"max_cost_eur": m.get("cap"), "max_case_cost_eur": m.get("caseCap"), "set_by": m.get("budget_src") or ""},
            "run_settings": {"max_parallel_cases": s.get("max_parallel"), "launch_rate_per_min": s.get("launch_rate"),
                             "min_think_time_s": s.get("think_time"), "retries_per_turn": s.get("retries"),
                             "part_token_budget": s.get("part_budget")},
            "engine": {"channel": "chat", "path": "webchat-turn", "rudi_release": eng.get("rudi_release"),
                       "arena_release": ARENA_RELEASE, "personality_default": "deployed default",
                       "prompt_assets": eng.get("prompt_assets") or {}, "cascades": m.get("cascades") or {},
                       "evaluators": m.get("evaluators") or ["llm-judge"]}}


class Assembler:
    def __init__(self, store, s3, notify, reinvoke, remaining_ms=lambda: 900_000):
        self.store, self.s3, self.notify, self.reinvoke, self.remaining_ms = store, s3, notify, reinvoke, remaining_ms

    def run(self, event):
        st = self.store
        bid = event["batch"]
        m = st.manifest(bid)
        partial_at = event.get("partial_at") or (time.strftime("%Y%m%dT%H%MZ", time.gmtime()) if event.get("snapshot") else None)
        rows = st.index_rows(bid, m["total"])
        if partial_at:
            rows = [r for r in rows if r.get("outcome") not in ("queued", "running", None)]
        if not rows:
            log("assemble.nothing", batch=bid)
            return {"parts": 0}
        stem = file_stem(bid, m.get("name") or "")
        hm = header_manifest(m)
        summ = summary(m, rows)
        sizes = {}
        for r in rows:
            sizes[r["seq"]] = int(r.get("size") or 0) or len(arena_results.render_case(arena_results.stub_record(r, m)))
        plan_rows = [dict(r, size=sizes[r["seq"]] + OUTCOME_LINE_CHARS) for r in rows]
        probe_parts = [{"index": i, "seqs": [], "file": "x" * 80, "first": "0000", "last": "0000", "first_id": bid, "last_id": bid}
                       for i in (1, 2)]
        base = arena_results.render_header(hm, probe_parts[0], probe_parts, summ, [], partial_at)
        header_tokens = est_tokens(len(base)) + 40 * max(1, len(rows) // 1000)
        budget = int((m.get("settings") or {}).get("part_budget") or 450000)
        plan = event.get("plan") or arena_results.plan_parts(plan_rows, stem, budget, header_tokens, partial_at)
        st.b.put_json("batches/%s/output/plan%s.json" % (bid, "-" + partial_at if partial_at else ""), plan)
        by_seq = {r["seq"]: r for r in rows}
        prefix = "batches/%s/output/" % bid
        for part in plan[int(event.get("from_part") or 1) - 1:]:
            if self.remaining_ms() < MIN_REMAINING_MS:
                self.reinvoke(dict(event, plan=plan, from_part=part["index"], partial_at=partial_at))
                log("assemble.continue_later", batch=bid, next_part=part["index"])
                return {"parts": len(plan), "continued": True}
            part_rows = [by_seq[s] for s in part["seqs"]]
            w = S3Writer(self.s3, st.b.name, prefix + part["file"])
            w.write(arena_results.render_header(hm, part, plan, summ, part_rows, partial_at))
            for r in part_rows:
                text = st.result_yaml(bid, r["seq"])
                w.write(text if text else arena_results.render_case(arena_results.stub_record(r, m)))
            w.close()
            log("assemble.part", batch=bid, part=part["index"], of=len(plan), cases=len(part_rows))
        part_of = {s: p["index"] for p in plan for s in p["seqs"]}
        idx_name = "%s__%s.csv" % (stem, "index" if not partial_at else "partial-index-" + partial_at)
        st.b.put_text(prefix + idx_name, arena_results.index_csv(rows, part_of), "text/csv; charset=utf-8")
        files = [p["file"] for p in plan] + [idx_name]
        self.notify({"type": "assembled", "batch": bid, "parts": len(plan), "files": files,
                     "snapshot": bool(partial_at), "partial_at": partial_at})
        log("assemble.done", batch=bid, parts=len(plan), snapshot=bool(partial_at))
        return {"parts": len(plan), "files": files}


def handler(event, context=None):
    import boto3
    s3 = boto3.client("s3")
    sqs = boto3.client("sqs")
    lam = boto3.client("lambda")
    store = ArenaStore(s3, ARENA_BUCKET)

    def notify(msg):
        sqs.send_message(QueueUrl=DISPATCH_QUEUE_URL, MessageBody=json.dumps(msg), MessageGroupId="dispatch",
                         MessageDeduplicationId="asm-%s-%s" % (msg["batch"], msg.get("partial_at") or "final"))

    def reinvoke(payload):
        lam.invoke(FunctionName=SELF_FUNCTION, InvocationType="Event", Payload=json.dumps(payload).encode("utf-8"))

    remaining = context.get_remaining_time_in_millis if context else (lambda: 900_000)
    return Assembler(store, s3, notify, reinvoke, remaining).run(event)
