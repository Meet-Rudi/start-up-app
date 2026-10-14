"""
MEET_RUDI Training Arena — meetrudi-arena-dispatcher: pace, budgets, progress. One tick at a time.

Triggered by the FIFO queue meetrudi-arena-dispatch.fifo (single message group, so ticks never
overlap) and by a one-minute schedule that only drops a tick on that queue. It is the ONLY writer
of a launched batch's manifest, the batch index, spend and the dashboard stats, so no two
functions ever race to update the same number.

Messages:
    launch     {batch}                        a batch is ready to run
    finished   {batch, seq, attempt, row}     a runner finished a case
    control    {action, batch}                pause / resume / cancel a batch
    assembled  {batch, parts, files, ...}     the assembler wrote the results
    tick       {}                             nothing new — just look again

Each tick: apply messages → expire lost leases → release the next cases → finalise batches with
nothing left. A case is released only if a slot, the launch rate, the batch budget, the
submitter's day budget and the arena's month budget all allow it; its ceiling is reserved until
it finishes, so no budget is ever overrun by more than the calls in flight.
"""

import os
import json
import time

import arena_costs
from arena_common import iso, log, local_day, in_quiet_hours, case_id
from arena_store import ArenaStore, BLOCK

ARENA_BUCKET = os.environ.get("ARENA_BUCKET", "")
CASES_QUEUE_URL = os.environ.get("CASES_QUEUE_URL", "")
DISPATCH_QUEUE_URL = os.environ.get("DISPATCH_QUEUE_URL", "")
ASSEMBLER_FUNCTION = os.environ.get("ASSEMBLER_FUNCTION", "meetrudi-arena-assembler")
LEASE_TTL_S = 20 * 60
LAT_BUCKETS = [250, 500, 750, 1000, 1500, 2000, 2500, 3000, 4000, 5000, 6000, 7500, 10000, 15000, 20000, 30000, 60000, 120000]
NO_VERDICT = ("completed", "cancelled", "over_budget")
NOT_OK = ("failed", "violation", "error")
FINAL = ("completed", "completed_with_errors", "budget_exhausted", "cancelled")


def lat_bucket(ms):
    for b in LAT_BUCKETS:
        if ms <= b:
            return str(b)
    return "inf"


def empty_day(day):
    return {"date": day, "success": 0, "failed": 0, "violation": 0, "error": 0, "none": 0, "total": 0,
            "lat_hist": {}, "models": {}, "errors": {}, "roles": {r: 0.0 for r in arena_costs.ROLES}, "cost": {},
            "modes": {m: {"n": 0, "eur": 0.0, "max": 0.0, "over": 0} for m in ("scripted", "intent", "hybrid")}}


class Dispatcher:
    def __init__(self, store, send_case, invoke_assembler, clock=time.time):
        self.store, self.send_case, self.invoke_assembler, self.clock = store, send_case, invoke_assembler, clock

    # ------------------------------------------------------------------ one tick
    def process(self, messages):
        st = self.store
        self.now = self.clock()
        self.settings, self.budgets = st.engine_settings(), st.budgets()
        self.active = st.runtime("active", {"batches": []})
        self.state = st.runtime("dispatcher", {"tokens": float(self.settings["launch_rate"]), "refill_at": self.now})
        self.manifests, self.dirty = {}, set()
        self.blocks, self.dirty_blocks = {}, set()
        self.days, self.users, self.keys = {}, {}, None
        self.leases = {(lz["batch"], int(lz["seq"])): lz for lz in st.leases()}
        for m in messages:
            kind = m.get("type")
            try:
                if kind == "launch":
                    self._launch(m["batch"])
                elif kind == "finished":
                    self._finished(m)
                elif kind == "control":
                    self._control(m)
                elif kind == "assembled":
                    self._assembled(m)
            except Exception as e:  # noqa: BLE001 - one bad message must not stop the tick
                log("dispatch.message_failed", type=kind, batch=m.get("batch"), error=type(e).__name__, detail=str(e)[:200])
        self._expire_leases()
        released = self._release()
        self._finalise()
        self._save()
        log("dispatch.tick", messages=len(messages), released=released, inflight=len(self.leases),
            active=len(self.active["batches"]))
        return released

    # ------------------------------------------------------------------ cached state
    def manifest(self, bid):
        if bid not in self.manifests:
            self.manifests[bid] = self.store.manifest(bid)
        return self.manifests[bid]

    def _touch(self, m):
        self.dirty.add(m["id"])

    def row(self, bid, seq):
        block = (int(seq) - 1) // BLOCK
        key = (bid, block)
        if key not in self.blocks:
            self.blocks[key] = self.store.index_block(bid, block)
        return self.blocks[key].get(str(seq))

    def set_row(self, bid, seq, row):
        block = (int(seq) - 1) // BLOCK
        key = (bid, block)
        if key not in self.blocks:
            self.blocks[key] = self.store.index_block(bid, block)
        self.blocks[key][str(seq)] = row
        self.dirty_blocks.add(key)

    def day(self, d):
        if d not in self.days:
            self.days[d] = self.store.daily(d) or empty_day(d)
        return self.days[d]

    def users_today(self):
        d = local_day()
        if d not in self.users:
            self.users[d] = self.store.users_day(d)
        return self.users[d]

    def _save(self):
        for bid in self.dirty:
            self.store.put_manifest(self.manifests[bid])
        for (bid, block) in self.dirty_blocks:
            self.store.put_index_block(bid, block, self.blocks[(bid, block)])
        for d, obj in self.days.items():
            self.store.put_daily(d, obj)
        for d, obj in self.users.items():
            self.store.put_users_day(d, obj)
        if self.keys is not None:
            self.store.put_keys_stats(self.keys)
        self.store.put_runtime("active", self.active)
        self.store.put_runtime("dispatcher", self.state)

    # ------------------------------------------------------------------ messages
    def _launch(self, bid):
        m = self.manifest(bid)
        if not m or m.get("status") not in ("queued",):
            return
        if bid not in self.active["batches"]:
            self.active["batches"].append(bid)
        log("dispatch.launch", batch=bid, cases=m.get("total"))

    def _finished(self, msg):
        bid, seq, attempt = msg["batch"], int(msg["seq"]), int(msg.get("attempt") or 1)
        lease = self.leases.get((bid, seq))
        if not lease or int(lease.get("attempt") or 1) != attempt:
            return                                                  # already counted, or a stale runner
        self._count(bid, seq, msg["row"], lease)

    def _count(self, bid, seq, row, lease):
        m = self.manifest(bid)
        outcome = row.get("outcome") or "error"
        c = m["c"]
        c["running"] = max(0, int(c.get("running") or 0) - 1)
        c[outcome] = int(c.get(outcome) or 0) + 1
        m["spent"] = round(float(m.get("spent") or 0) + float(row.get("cost") or 0), 6)
        m["reserved"] = max(0.0, round(float(m.get("reserved") or 0) - float(lease.get("reserved") or 0), 6))
        roles = m.setdefault("roles", {r: 0.0 for r in arena_costs.ROLES})
        for r, v in (row.get("roles") or {}).items():
            roles[r] = round(float(roles.get(r) or 0) + float(v or 0), 6)
        self._engine_versions(m, row.get("engine") or {})
        slim = {k: v for k, v in row.items() if k not in ("engine",)}
        self.set_row(bid, seq, slim)
        self._stats(m, row)
        self.store.delete_lease(bid, seq)
        self.leases.pop((bid, seq), None)
        self._touch(m)

    @staticmethod
    def _engine_versions(m, eng):
        e = m.setdefault("engine", {})
        rel = eng.get("release")
        if rel:
            if e.get("rudi_release") in (None, "", rel):
                e["rudi_release"] = rel
            elif rel not in str(e["rudi_release"]):
                e["rudi_release"] = "%s (mixed: %s)" % (e["rudi_release"], rel)
        assets = e.setdefault("prompt_assets", {})
        for k, v in (eng.get("assets") or {}).items():
            if k not in assets:
                assets[k] = v
            elif v and v not in str(assets[k]):
                assets[k] = "%s (mixed: %s)" % (assets[k], v)

    def _stats(self, m, row):
        day = self.day(local_day())
        outcome = row.get("outcome")
        if outcome in ("success", "failed", "violation", "error"):
            day[outcome] += 1
        else:
            day["none"] += 1
        day["total"] += 1
        for ms in row.get("lat") or []:
            b = lat_bucket(int(ms))
            day["lat_hist"][b] = day["lat_hist"].get(b, 0) + 1
        for name, n in (row.get("models") or {}).items():
            day["models"][name] = day["models"].get(name, 0) + n
        for code, n in (row.get("err_codes") or {}).items():
            day["errors"][code] = day["errors"].get(code, 0) + n
        for r, v in (row.get("roles") or {}).items():
            day["roles"][r] = round(day["roles"].get(r, 0.0) + float(v or 0), 6)
        for name, v in (row.get("cost_models") or {}).items():
            day["cost"][name] = round(day["cost"].get(name, 0.0) + float(v or 0), 6)
        mode = day["modes"].setdefault(row.get("mode") or "intent", {"n": 0, "eur": 0.0, "max": 0.0, "over": 0})
        cost = float(row.get("cost") or 0)
        if outcome != "over_budget" or cost > 0:
            mode["n"] += 1
            mode["eur"] = round(mode["eur"] + cost, 6)
            mode["max"] = max(mode["max"], cost)
        if outcome == "over_budget":
            mode["over"] += 1
        users = self.users_today()
        users[m["by"]] = round(float(users.get(m["by"]) or 0) + cost, 6)
        if self.keys is None:
            self.keys = self.store.keys_stats()
        if row.get("key") and outcome not in NO_VERDICT:
            k = self.keys.setdefault(row["key"], {"runs": 0, "bad": 0, "kinds": {}})
            k["runs"] += 1
            if outcome in NOT_OK:
                k["bad"] += 1
                k["kinds"][outcome] = k["kinds"].get(outcome, 0) + 1
            k["last"], k["when"] = outcome, local_day()

    def _control(self, msg):
        m = self.manifest(msg["batch"])
        if not m or m.get("status") in FINAL + ("assembling",):
            return
        action = msg.get("action")
        if action == "pause" and m["status"] in ("queued", "running"):
            m["status_before_pause"], m["status"] = m["status"], "paused"
        elif action == "resume" and m["status"] == "paused":
            m["status"] = m.pop("status_before_pause", "running")
        elif action == "cancel":
            self._close_remaining(m, "cancelled", "cancelled")
            m["status"], m["final_status"] = "cancelled", "cancelled"
        elif action == "quiet":
            m["quiet_only"] = bool(msg.get("quiet_only"))
        self._touch(m)
        log("dispatch.control", batch=m["id"], action=action)

    def _assembled(self, msg):
        m = self.manifest(msg["batch"])
        if not m:
            return
        if msg.get("snapshot"):
            m.setdefault("snapshots", []).append({"at": msg.get("partial_at"), "files": msg.get("files") or []})
        else:
            m["parts"], m["files"] = int(msg.get("parts") or 0), msg.get("files") or []
            m["status"] = m.get("final_status") or "completed"
        self._touch(m)

    def _close_remaining(self, m, outcome, stop):
        """Record every case not yet started as `outcome` (never run)."""
        bid = m["id"]
        n = 0
        for seq in range(int(m.get("cursor") or 0) + 1, int(m["total"]) + 1):
            r = self.row(bid, seq) or {"seq": seq, "id": case_id(bid, seq, m["total"])}
            r.update({"outcome": outcome, "stop": stop, "cost": 0.0, "turns": 0})
            self.set_row(bid, seq, r)
            n += 1
        c = m["c"]
        c["queued"] = 0
        c[outcome] = int(c.get(outcome) or 0) + n
        m["cursor"] = int(m["total"])
        if n:
            day = self.day(local_day())
            day["none"] += n
            day["total"] += n
        return n

    # ------------------------------------------------------------------ leases
    def _expire_leases(self):
        for (bid, seq), lz in list(self.leases.items()):
            if self.now - float(lz.get("heartbeat_at") or lz.get("released_at") or self.now) < LEASE_TTL_S:
                continue
            if int(lz.get("attempt") or 1) < 2:
                lz.update({"attempt": int(lz.get("attempt") or 1) + 1, "heartbeat_at": self.now})
                self.store.put_lease(lz)
                self.send_case({"batch": bid, "seq": seq, "attempt": lz["attempt"]})
                log("dispatch.requeue_lost", batch=bid, seq=seq, attempt=lz["attempt"])
            else:
                row = dict(self.row(bid, seq) or {"seq": seq, "id": case_id(bid, seq, (self.manifest(bid) or {}).get("total") or seq)})
                row.update({"outcome": "error", "stop": "runner_lost", "cost": 0.0, "errs": 1, "err_codes": {"runner_lost": 1}})
                self._count(bid, seq, row, lz)
                log("dispatch.runner_lost", batch=bid, seq=seq)

    # ------------------------------------------------------------------ release
    def ceiling(self):
        s = self.settings
        hard = max(1, (int(s["account_limit"]) - int(s["live_reserve"])) // max(1, int(s["execs_per_case"])))
        return max(0, min(int(s["max_parallel"]), hard))

    def _release(self):
        s, b = self.settings, self.budgets
        if s.get("state") != "running":
            return 0
        breaker = self.store.runtime("breaker", {}) or {}
        if float(breaker.get("until") or 0) > self.now:
            return 0
        rate = float(s["launch_rate"])
        self.state["tokens"] = min(rate, float(self.state.get("tokens") or 0) + (self.now - float(self.state.get("refill_at") or self.now)) / 60.0 * rate)
        self.state["refill_at"] = self.now
        free = self.ceiling() - len(self.leases)
        if free <= 0:
            return 0
        month_prefix = local_day()[:8]
        month_spent = self._month_spent(month_prefix)
        reserved_total = sum(float(lz.get("reserved") or 0) for lz in self.leases.values())
        users = self.users_today()
        user_reserved = {}
        for lz in self.leases.values():
            user_reserved[lz.get("by")] = user_reserved.get(lz.get("by"), 0.0) + float(lz.get("reserved") or 0)
        per_batch = {}
        for (bid, _seq) in self.leases:
            per_batch[bid] = per_batch.get(bid, 0) + 1
        released = 0
        order = list(self.active["batches"])
        rr = int(self.state.get("rr") or 0) % max(1, len(order)) if order else 0
        order = order[rr:] + order[:rr]
        progress = True
        while free > 0 and self.state["tokens"] >= 1 and progress:
            progress = False
            for bid in order:
                if free <= 0 or self.state["tokens"] < 1:
                    break
                m = self.manifest(bid)
                if not m or m["status"] not in ("queued", "running") or int(m["cursor"]) >= int(m["total"]):
                    continue
                if (s.get("quiet_only") or m.get("quiet_only")) and not in_quiet_hours():
                    continue
                if per_batch.get(bid, 0) >= int(s["per_batch_parallel"]):
                    continue
                seq = int(m["cursor"]) + 1
                row = self.row(bid, seq) or {}
                cap = float(row.get("cap") or m.get("caseCap") or b["case_default"])
                if float(m.get("spent") or 0) + float(m.get("reserved") or 0) + cap > float(m["cap"]) + 1e-9:
                    if per_batch.get(bid, 0) == 0:
                        n = self._close_remaining(m, "over_budget", "batch_budget")
                        m["final_status"] = "budget_exhausted"
                        self._touch(m)
                        log("dispatch.batch_budget_exhausted", batch=bid, not_started=n)
                    continue
                if float(users.get(m["by"]) or 0) + user_reserved.get(m["by"], 0.0) + cap > float(b["user_day_budget"]) + 1e-9:
                    m["hold"] = "the submitter's daily budget is spent"
                    self._touch(m)
                    continue
                if month_spent + reserved_total + cap > float(b["month_budget"]) + 1e-9:
                    m["hold"] = "the arena's monthly budget is spent"
                    self._touch(m)
                    break
                self._release_one(m, seq, row, cap)
                m.pop("hold", None)
                per_batch[bid] = per_batch.get(bid, 0) + 1
                reserved_total += cap
                user_reserved[m["by"]] = user_reserved.get(m["by"], 0.0) + cap
                free -= 1
                self.state["tokens"] -= 1
                released += 1
                progress = True
        self.state["rr"] = (rr + 1) % max(1, len(self.active["batches"]) or 1)
        return released

    def _month_spent(self, prefix):
        days = {k.rsplit("/", 1)[-1][:-5] for k in self.store.b.list_keys("stats/daily/" + prefix)}
        days |= {d for d in self.days if d.startswith(prefix)}
        total = 0.0
        for d in days:
            obj = self.days.get(d) or self.store.daily(d) or {}
            total += sum(float(v or 0) for v in (obj.get("roles") or {}).values())
        return total

    def _release_one(self, m, seq, row, cap):
        lease = {"batch": m["id"], "seq": seq, "attempt": 1, "released_at": self.now, "heartbeat_at": self.now,
                 "reserved": cap, "by": m["by"]}
        self.store.put_lease(lease)
        self.leases[(m["id"], seq)] = lease
        self.send_case({"batch": m["id"], "seq": seq, "attempt": 1})
        m["cursor"] = seq
        c = m["c"]
        c["queued"] = max(0, int(c.get("queued") or 0) - 1)
        c["running"] = int(c.get("running") or 0) + 1
        m["reserved"] = round(float(m.get("reserved") or 0) + cap, 6)
        if m["status"] == "queued":
            m["status"] = "running"
        m.setdefault("started_at", iso())
        if row:
            row["outcome"] = "running"
            self.set_row(m["id"], seq, row)
        self._touch(m)

    # ------------------------------------------------------------------ finalise
    def _finalise(self):
        still = []
        for bid in self.active["batches"]:
            m = self.manifest(bid)
            if not m:
                continue
            inflight = sum(1 for (b_, _s) in self.leases if b_ == bid)
            done = int(m.get("cursor") or 0) >= int(m["total"]) and inflight == 0 and m["status"] != "paused"
            if not done:
                still.append(bid)
                continue
            c = m["c"]
            m["final_status"] = m.get("final_status") or ("completed_with_errors" if int(c.get("error") or 0) else "completed")
            m["status"] = "assembling"
            m["finished_at"] = iso()
            self._touch(m)
            self.invoke_assembler({"batch": bid})
            log("dispatch.batch_done", batch=bid, final=m["final_status"])
        self.active["batches"] = still


# --------------------------------------------------------------------------- Lambda wiring
def handler(event, context=None):
    import boto3
    sqs = boto3.client("sqs")
    records = (event or {}).get("Records")
    if not records:                                   # the schedule: just drop a tick on the queue
        bucket_ts = int(time.time() // 30)
        sqs.send_message(QueueUrl=DISPATCH_QUEUE_URL, MessageBody=json.dumps({"type": "tick"}),
                         MessageGroupId="dispatch", MessageDeduplicationId="tick-%d" % bucket_ts)
        return {"ok": True, "tick": True}
    lam = boto3.client("lambda")
    store = ArenaStore(boto3.client("s3"), ARENA_BUCKET)

    def send_case(msg):
        sqs.send_message(QueueUrl=CASES_QUEUE_URL, MessageBody=json.dumps(msg))

    def invoke_assembler(payload):
        lam.invoke(FunctionName=ASSEMBLER_FUNCTION, InvocationType="Event", Payload=json.dumps(payload).encode("utf-8"))

    msgs = [json.loads(r["body"]) for r in records]
    Dispatcher(store, send_case, invoke_assembler).process(msgs)
    return {"ok": True}
