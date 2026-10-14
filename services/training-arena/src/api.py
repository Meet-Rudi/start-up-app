"""
MEET_RUDI Training Arena — meetrudi-arena-api: the console's only door (Lambda Function URL).

The console is static on GitHub Pages, so this function owns every decision: who is signed in,
what their role allows, whether a batch may be launched. The Function URL pins CORS to the Pages
origin. Every /admin/* route re-checks the super-admin role; batch actions check that the caller
submitted the batch or is a super-admin. Responses are shaped for the console (site/training-arena).

Routes (session token in X-Arena-Token):
    GET  /health
    POST /login {username, password}               POST /logout
    GET  /me                                        POST /account/password {current, new}
    GET  /dashboard?days=30
    POST /uploads {filename, size}                  → signed PUT url into inbox/<user>/
    POST /batches {key | s3_uri, name, quiet_only, free_slow}  → draft batch, validation started
    GET  /batches                                   GET /batches/{id}
    POST /batches/{id}/{validate,launch,pause,resume,cancel,rerun,snapshot}
    POST /batches/{id}/quiet {quiet_only}           → hold for / release from quiet hours
    POST /batches/{id}/free_slow {on}               → pace to the free tiers / back to normal speed
    GET  /batches/{id}/cases?offset=&limit=         GET /batches/{id}/files/{name}
    GET  /cases/{case_id}                           GET /cases/{case_id}/download
    --- super-admin ---
    GET/PUT /admin/settings                         POST /admin/engine/{pause,resume,drain,kill}
    GET/POST /admin/users                           POST /admin/users/{name}/{reset,lock,unlock,role}
    DELETE /admin/users/{name}                      GET /admin/audit · /admin/storage · /admin/versions
"""

import os
import re
import json
import base64
import datetime

from arena_auth import Auth, AuthError
from arena_common import (iso, log, display, local_day, new_batch_id, slug, file_stem, split_case_id,
                          BATCH_ID_RE, parse_iso, now_utc, TZ, in_quiet_hours)
from arena_store import ArenaStore, ENGINE_DEFAULTS

ARENA_BUCKET = os.environ.get("ARENA_BUCKET", "")
DISPATCH_QUEUE_URL = os.environ.get("DISPATCH_QUEUE_URL", "")
INGEST_FUNCTION = os.environ.get("INGEST_FUNCTION", "meetrudi-arena-ingest")
ASSEMBLER_FUNCTION = os.environ.get("ASSEMBLER_FUNCTION", "meetrudi-arena-assembler")
ENGINE_FUNCTION = os.environ.get("ENGINE_FUNCTION", "meetrudi-engine-turn")
RELEASE = os.environ.get("RELEASE", "unknown")
ALLOWED_EXT = (".yaml", ".yml", ".json", ".jsonl")
LIVE_KEYS = ("meetrudi-groq-firstkey", "meetrudi-ovh-firstkey")       # live Rudi's keys: never the arena's
URL_TTL = 900


class Http(Exception):
    def __init__(self, status, code, detail=""):
        super().__init__(code)
        self.status, self.code, self.detail = status, code, detail


def resp(status, obj):
    return {"statusCode": status, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(obj, ensure_ascii=False, default=str)}


class Api:
    def __init__(self, store, auth, s3, send_dispatch, invoke, engine_info=None, clock=None):
        self.store, self.auth, self.s3 = store, auth, s3
        self.send_dispatch, self.invoke = send_dispatch, invoke
        self.engine_info = engine_info or (lambda: {})

    # ------------------------------------------------------------------ plumbing
    def handle(self, event):
        method = ((event.get("requestContext") or {}).get("http") or {}).get("method") or event.get("httpMethod") or "GET"
        path = (event.get("rawPath") or event.get("path") or "/").rstrip("/") or "/"
        if method == "OPTIONS":
            return resp(200, {})
        headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        query = event.get("queryStringParameters") or {}
        body = self._body(event)
        try:
            if path in ("/", "/health"):
                return resp(200, {"ok": True, "service": "meetrudi-arena-api", "release": RELEASE})
            if path == "/login" and method == "POST":
                token, user = self.auth.login(body.get("username"), body.get("password"))
                return resp(200, {"token": token, "user": user})
            got = self.auth.session(headers.get("x-arena-token"))
            if not got:
                raise Http(401, "unauthorized", "Your session has ended. Sign in again.")
            self.sess, self.user = got
            self.token = headers.get("x-arena-token")
            if self.user.get("status") == "must_change" and path not in ("/account/password", "/logout", "/me"):
                raise Http(403, "must_change_password", "Choose your own password first.")
            return self._route(method, path, query, body)
        except AuthError as e:
            return resp(e.status, {"error": e.code, "detail": e.detail})
        except Http as e:
            return resp(e.status, {"error": e.code, "detail": e.detail})
        except Exception as e:  # noqa: BLE001 - never leak a stack trace to the browser
            log("api.error", path=path, error=type(e).__name__, detail=str(e)[:200])
            return resp(500, {"error": "internal"})

    @staticmethod
    def _body(event):
        raw = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw).decode("utf-8")
        try:
            obj = json.loads(raw)
        except ValueError:
            return {}
        return obj if isinstance(obj, dict) else {}

    def _sa(self):
        if self.user.get("role") != "superadmin":
            raise Http(403, "forbidden", "That is for super-admins.")

    def _owner_or_sa(self, m):
        if m.get("by") != self.user["u"] and self.user.get("role") != "superadmin":
            raise Http(403, "forbidden", "Only the submitter or a super-admin can do that.")

    def _batch(self, bid):
        if not BATCH_ID_RE.match(bid or ""):
            raise Http(404, "not_found", "No such batch.")
        m = self.store.manifest(bid)
        if not m:
            raise Http(404, "not_found", "No such batch.")
        return m

    def _route(self, method, path, q, body):
        parts = [p for p in path.split("/") if p]
        if parts == ["logout"] and method == "POST":
            self.auth.logout(self.token)
            return resp(200, {"ok": True})
        if parts == ["me"]:
            return resp(200, self._me())
        if parts == ["account", "password"] and method == "POST":
            self.auth.change_password(self.user, body.get("current"), body.get("new"))
            return resp(200, {"ok": True})
        if parts == ["dashboard"]:
            return resp(200, self._dashboard(int(q.get("days") or 30)))
        if parts == ["uploads"] and method == "POST":
            return resp(200, self._upload(body))
        if parts == ["batches"] and method == "GET":
            return resp(200, {"batches": [self._batch_view(m) for m in self.store.manifests()]})
        if parts == ["batches"] and method == "POST":
            return resp(201, {"batch": self._batch_view(self._create(body))})
        if len(parts) >= 2 and parts[0] == "batches":
            m = self._batch(parts[1])
            if len(parts) == 2 and method == "GET":
                return resp(200, self._batch_detail(m))
            if len(parts) == 3 and method == "POST":
                return resp(200, self._batch_action(m, parts[2], body))
            if len(parts) == 3 and parts[2] == "cases":
                return resp(200, self._cases(m, q))
            if len(parts) == 4 and parts[2] == "files":
                return resp(200, {"url": self._file_url(m, parts[3])})
        if len(parts) >= 2 and parts[0] == "cases":
            if len(parts) == 2:
                return resp(200, {"case": self._case_view(parts[1])})
            if len(parts) == 3 and parts[2] == "download":
                bid, seq = split_case_id(parts[1])
                self._batch(bid)
                return resp(200, {"url": self._presign_get(self.store.result_yaml_key(bid, seq), "%s.yaml" % parts[1])})
        if parts and parts[0] == "admin":
            self._sa()
            return self._admin(method, parts[1:], q, body)
        raise Http(404, "not_found", "No such route.")

    # ------------------------------------------------------------------ views
    def _me(self):
        b = self.store.budgets()
        spent = float(self.store.users_day(local_day()).get(self.user["u"]) or 0)
        return {"user": self.auth._public(self.user), "spent_today": spent,
                "budgets": {k: b[k] for k in ("user_day_budget", "month_budget", "case_default", "batch_default",
                                              "case_ceiling", "batch_ceiling")},
                "settings": {k: self.store.engine_settings()[k] for k in ("max_parallel", "launch_rate", "think_time", "max_turns_cap")},
                "bucket": ARENA_BUCKET}

    def _eta(self, m):
        remaining = int(m["c"].get("queued") or 0) + int(m["c"].get("running") or 0)
        done = sum(int(m["c"].get(k) or 0) for k in ("success", "failed", "violation", "error", "completed", "cancelled", "over_budget"))
        if not remaining or m.get("status") not in ("running", "queued"):
            return ""
        if m.get("hold"):
            return "held — " + m["hold"]
        slow = "free slow — " if m.get("free_slow") else ""
        if not in_quiet_hours():
            if m.get("quiet_only"):
                return slow + "waits for quiet hours — starts at 21:30 (Europe/Brussels)"
            if self.store.engine_settings().get("quiet_only"):
                return slow + "waits for quiet hours (arena setting) — starts at 21:30 (Europe/Brussels)"
        started = parse_iso(m.get("started_at"))
        if not started or not done:
            return slow + ("starts when a slot frees" if m.get("status") == "queued" else "after the first cases finish")
        per_case = (now_utc() - started).total_seconds() / done
        finish = now_utc() + datetime.timedelta(seconds=per_case * remaining)
        return slow + "≈ " + finish.astimezone(TZ).strftime("%H:%M" if finish.date() == now_utc().date() else "%d %b %H:%M")

    def _batch_view(self, m):
        c = dict({k: 0 for k in ("success", "failed", "violation", "error", "completed", "cancelled", "over_budget",
                                 "running", "queued")}, **(m.get("c") or {}))
        return {"id": m["id"], "name": m.get("name") or "", "by": m.get("by"), "status": m.get("status"),
                "submitted": display(m.get("submitted_at")), "started": display(m.get("started_at")),
                "finished": display(m.get("finished_at")), "total": int(m.get("total") or 0), "c": c,
                "eta": self._eta(m), "parts": int(m.get("parts") or 0), "modes": m.get("modes") or {},
                "locales": m.get("locales") or {}, "tags": m.get("tags") or [], "desc": m.get("desc") or "",
                "spent": float(m.get("spent") or 0), "cap": float(m.get("cap") or 0), "caseCap": float(m.get("caseCap") or 0),
                "roles": m.get("roles") or {}, "files": m.get("files") or [], "hold": m.get("hold") or "",
                "quiet_only": bool(m.get("quiet_only")), "free_slow": bool(m.get("free_slow")), "stem": m.get("stem"), "input_file": m.get("input_file"),
                "validation": m.get("validation") or {}, "rudi_release": (m.get("engine") or {}).get("rudi_release") or "",
                "snapshots": m.get("snapshots") or []}

    def _batch_detail(self, m):
        return {"batch": self._batch_view(m), "validation": self.store.validation(m["id"])}

    def _cases(self, m, q):
        rows = self.store.index_rows(m["id"], m.get("total") or 0)
        off, lim = int(q.get("offset") or 0), min(5000, int(q.get("limit") or 2000))
        keep = ("seq", "id", "key", "title", "mode", "loc", "persona", "outcome", "stop", "turns", "dur", "errs", "cost", "cap", "roles", "tags")
        return {"total": len(rows), "rows": [{k: r.get(k) for k in keep} for r in rows[off:off + lim]]}

    def _case_view(self, cid):
        bid, seq = split_case_id(cid)
        if not bid:
            raise Http(404, "not_found", "No such case.")
        m = self._batch(bid)
        rec = self.store.result(bid, seq)
        row = (self.store.index_block(bid, (seq - 1) // 1000) or {}).get(str(seq)) or {}
        if not rec:
            if not row:
                raise Http(404, "not_found", "No such case.")
            v = {k: row.get(k) for k in ("key", "title", "mode", "persona", "outcome", "stop", "turns", "dur", "errs", "cost", "cap")}
            v.update({"id": cid, "n": seq, "loc": row.get("loc"), "verdict": "This case has not finished yet." if row.get("outcome") in ("queued", "running") else "",
                      "checks": [], "final": {}, "errors": [], "tx": None, "batch": self._batch_view(m)})
            return v
        return dict(case_view(rec), batch=self._batch_view(m))

    # ------------------------------------------------------------------ dashboard
    def _dashboard(self, days):
        days = max(1, min(90, days))
        today = datetime.datetime.now(TZ).date()
        daily = []
        for i in range(days - 1, -1, -1):
            d = (today - datetime.timedelta(days=i)).isoformat()
            daily.append(day_view(self.store.daily(d), d))
        manifests = self.store.manifests(200)
        active = [m for m in manifests if m.get("status") in ("queued", "running", "paused", "launching", "assembling")]
        recent = [m for m in manifests if m not in active][:20]
        keys = self.store.keys_stats()
        failing = sorted(({"key": k, "runs": v["runs"], "bad": v["bad"], "last": v.get("last"), "when": v.get("when"),
                           "kinds": ", ".join("%d %s" % (n, kind) for kind, n in (v.get("kinds") or {}).items())}
                          for k, v in keys.items() if v.get("runs", 0) >= 2 and v.get("bad")),
                         key=lambda x: (-x["bad"] / x["runs"], -x["runs"]))[:5]
        spend = self.store.users_day(local_day())
        if self.user.get("role") == "superadmin":
            users = [{"u": n, "spentToday": float(v or 0)} for n, v in spend.items()]
            if not any(x["u"] == self.user["u"] for x in users):
                users.append({"u": self.user["u"], "spentToday": 0.0})
        else:
            users = [{"u": self.user["u"], "spentToday": float(spend.get(self.user["u"]) or 0)}]
        b = self.store.budgets()
        # The period's most expensive cases, from the batches that ran in it (index rows only).
        since = (today - datetime.timedelta(days=days - 1)).isoformat()
        expensive = []
        for m in active + recent:
            if (display(m.get("started_at")) or "9999")[:10] < since:
                continue
            for r in self.store.index_rows(m["id"], m.get("total") or 0):
                if float(r.get("cost") or 0) > 0:
                    expensive.append({k: r.get(k) for k in ("id", "key", "turns", "cost", "cap", "outcome", "stop")})
        expensive.sort(key=lambda r: -float(r["cost"]))
        return {"daily": daily, "batches": [self._batch_view(m) for m in active + recent], "failingKeys": failing,
                "expensive": expensive[:6],
                "users": users, "budgets": {"month_budget": b["month_budget"], "user_day_budget": b["user_day_budget"]},
                "now": display(iso())}

    # ------------------------------------------------------------------ batches
    def _upload(self, body):
        name = os.path.basename(str(body.get("filename") or ""))
        if not name.lower().endswith(ALLOWED_EXT):
            raise Http(400, "bad_type", "Use .yaml, .yml, .json or .jsonl.")
        key = "inbox/%s/%s/%s" % (self.user["u"], datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S"), name)
        url = self.s3.generate_presigned_url("put_object", Params={"Bucket": ARENA_BUCKET, "Key": key}, ExpiresIn=URL_TTL)
        return {"url": url, "key": key}

    def _create(self, body, only_keys=None, rerun_of=None, source_key=None):
        key = source_key or body.get("key")
        if body.get("s3_uri"):
            m = re.match(r"^s3://([^/]+)/(.+)$", str(body["s3_uri"]).strip())
            if not m or m.group(1) != ARENA_BUCKET:
                raise Http(400, "bad_location", "Only the arena bucket's inbox/ is readable: s3://%s/inbox/…" % ARENA_BUCKET)
            key = m.group(2)
        if not key or not (key.startswith("inbox/") or rerun_of) or not key.lower().endswith(ALLOWED_EXT) or ".." in key:
            raise Http(400, "bad_location", "Give a .yaml, .json or .jsonl object under inbox/.")
        try:
            self.s3.head_object(Bucket=ARENA_BUCKET, Key=key)
        except Exception:  # noqa: BLE001
            raise Http(404, "not_found", "No file at s3://%s/%s." % (ARENA_BUCKET, key))
        bid = new_batch_id()
        while self.store.manifest(bid):
            bid = new_batch_id()
        name = slug(body.get("name") or "")
        ext = os.path.splitext(key)[1].lower()
        stem = file_stem(bid, name)
        stored = "%s__input%s" % (stem, ext)
        dest = self.store.input_key(bid, stored)
        self.s3.copy_object(Bucket=ARENA_BUCKET, Key=dest, CopySource={"Bucket": ARENA_BUCKET, "Key": key})
        m = {"id": bid, "name": name, "name_from_console": bool(name), "stem": stem, "by": self.user["u"], "status": "draft",
             "submitted_at": iso(), "input_file": stored, "input_key": dest, "source": key, "quiet_only": bool(body.get("quiet_only")),
             "free_slow": bool(body.get("free_slow")),
             "desc": "", "tags": [], "c": {}, "total": 0, "only_keys": only_keys, "rerun_of": rerun_of}
        self.store.put_manifest(m)
        self.user["batches"] = int(self.user.get("batches") or 0) + 1
        self.store.put_user(self.user)
        self.store.audit(self.user["u"], "batch.submit", bid, "from %s" % ("rerun of " + rerun_of if rerun_of else key))
        self.invoke(INGEST_FUNCTION, {"batch": bid, "mode": "validate"})
        return m

    def _batch_action(self, m, action, body):
        u = self.user["u"]
        if action == "validate":
            self._owner_or_sa(m)
            if m["status"] not in ("draft", "invalid", "ready"):
                raise Http(409, "conflict", "This batch has already been launched.")
            self.invoke(INGEST_FUNCTION, {"batch": m["id"], "mode": "validate"})
            return {"ok": True}
        if action == "launch":
            self._owner_or_sa(m)
            if body.get("synthetic") is not True:
                raise Http(400, "confirm_synthetic", "Confirm that the file holds synthetic data only.")
            if m["status"] != "ready":
                raise Http(409, "not_ready", "Only a batch that passed validation can be launched.")
            m["quiet_only"] = bool(body.get("quiet_only", m.get("quiet_only")))
            m["free_slow"] = bool(body.get("free_slow", m.get("free_slow")))
            m["launched_by"] = u
            self.store.put_manifest(m)
            self.invoke(INGEST_FUNCTION, {"batch": m["id"], "mode": "launch"})
            self.store.audit(u, "batch.launch", m["id"], "%s cases" % m.get("total"))
            return {"ok": True}
        if action in ("pause", "resume", "cancel"):
            self._owner_or_sa(m)
            if m["status"] in ("draft", "invalid", "ready") and action == "cancel":
                m["status"] = "cancelled"
                self.store.put_manifest(m)                       # never launched: the API still owns it
            else:
                self.send_dispatch({"type": "control", "action": action, "batch": m["id"], "by": u})
            self.store.audit(u, "batch." + action, m["id"])
            return {"ok": True}
        if action == "quiet":
            self._owner_or_sa(m)
            if m["status"] in ("assembling", "completed", "completed_with_errors", "budget_exhausted", "cancelled"):
                raise Http(409, "conflict", "This batch has finished.")
            quiet = bool(body.get("quiet_only"))
            if m["status"] in ("draft", "invalid", "ready"):
                m["quiet_only"] = quiet
                self.store.put_manifest(m)                       # never launched: the API still owns it
            else:
                self.send_dispatch({"type": "control", "action": "quiet", "quiet_only": quiet, "batch": m["id"], "by": u})
            self.store.audit(u, "batch.quiet", m["id"], "quiet hours only" if quiet else "start now")
            return {"ok": True}
        if action == "free_slow":
            self._owner_or_sa(m)
            if m["status"] in ("assembling", "completed", "completed_with_errors", "budget_exhausted", "cancelled"):
                raise Http(409, "conflict", "This batch has finished.")
            on = bool(body.get("on"))
            if m["status"] in ("draft", "invalid", "ready"):
                m["free_slow"] = on
                self.store.put_manifest(m)                       # never launched: the API still owns it
            else:
                self.send_dispatch({"type": "control", "action": "free_slow", "on": on, "batch": m["id"], "by": u})
            self.store.audit(u, "batch.free_slow", m["id"], "on — paced to the free tiers" if on else "off — normal speed")
            return {"ok": True}
        if action == "rerun":
            self._owner_or_sa(m)
            only = None
            if body.get("only") == "not_ok":
                only = [r["key"] for r in self.store.index_rows(m["id"], m.get("total") or 0)
                        if r.get("outcome") in ("failed", "violation", "error", "over_budget")]
                if not only:
                    raise Http(409, "nothing_to_rerun", "No case of this batch ended badly.")
            new = self._create({"name": (m.get("name") or "rerun")[:34] + "-rerun", "quiet_only": m.get("quiet_only"),
                                "free_slow": m.get("free_slow")},
                               only_keys=only, rerun_of=m["id"], source_key=m["input_key"])
            return {"batch": self._batch_view(new)}
        if action == "snapshot":
            if m["status"] not in ("running", "paused", "queued"):
                raise Http(409, "conflict", "Snapshots are for running batches; finished ones have their results.")
            self.invoke(ASSEMBLER_FUNCTION, {"batch": m["id"], "snapshot": True})
            return {"ok": True}
        raise Http(404, "not_found", "No such action.")

    def _file_url(self, m, name):
        if "/" in name or ".." in name:
            raise Http(400, "bad_name", "")
        if name == m.get("input_file"):
            return self._presign_get(m["input_key"], name)
        files = set(m.get("files") or []) | {f for s in m.get("snapshots") or [] for f in s.get("files") or []}
        if name not in files:
            raise Http(404, "not_found", "No such file.")
        return self._presign_get("batches/%s/output/%s" % (m["id"], name), name)

    def _presign_get(self, key, filename):
        return self.s3.generate_presigned_url("get_object", ExpiresIn=URL_TTL, Params={
            "Bucket": ARENA_BUCKET, "Key": key, "ResponseContentDisposition": 'attachment; filename="%s"' % filename})

    # ------------------------------------------------------------------ admin
    def _admin(self, method, parts, q, body):
        u = self.user["u"]
        if parts == ["settings"] and method == "GET":
            return resp(200, {"settings": admin_settings(self.store)})
        if parts == ["settings"] and method == "PUT":
            changes = save_admin_settings(self.store, body.get("settings") or {})
            self.store.audit(u, "settings.update", "engine", " · ".join(changes)[:900])
            return resp(200, {"settings": admin_settings(self.store), "changed": changes})
        if len(parts) == 2 and parts[0] == "engine" and method == "POST":
            state = {"pause": "paused", "resume": "running", "drain": "draining", "kill": "killed"}.get(parts[1])
            if not state:
                raise Http(404, "not_found", "")
            s = self.store.engine_settings()
            s["state"] = state
            self.store.put_engine_settings(s)
            self.store.audit(u, "engine." + parts[1], "engine")
            if state == "running":
                self.send_dispatch({"type": "tick"})
            return resp(200, {"state": state})
        if parts == ["users"] and method == "GET":
            spend = self.store.users_day(local_day())
            return resp(200, {"users": [user_view(x, spend) for x in sorted(self.store.users(), key=lambda x: x["u"])]})
        if parts == ["users"] and method == "POST":
            pw = self.auth.create(u, body.get("username"), body.get("role") or "tester")
            return resp(201, {"temp_password": pw})
        if len(parts) == 2 and parts[0] == "users" and method == "DELETE":
            self.auth.delete(u, parts[1])
            return resp(200, {"ok": True})
        if len(parts) == 3 and parts[0] == "users" and method == "POST":
            name, act = parts[1], parts[2]
            if act == "reset":
                return resp(200, {"temp_password": self.auth.reset(u, name)})
            if act in ("lock", "unlock"):
                self.auth.lock(u, name, act == "lock")
                return resp(200, {"ok": True})
            if act == "role":
                self.auth.set_role(u, name, body.get("role"))
                return resp(200, {"ok": True})
        if parts == ["audit"]:
            n = max(1, min(60, int(q.get("days") or 14)))
            today = datetime.datetime.now(TZ).date()
            days = [(today - datetime.timedelta(days=i)).isoformat() for i in range(n)]
            entries = self.store.audit_entries(days)
            kind = q.get("kind") or ""
            rows = [[display(e["at"]), e["who"], e["action"], e.get("target") or "", e.get("detail") or ""]
                    for e in entries if not kind or e["action"].startswith(kind) or (kind == "breaker" and e["who"] == "system")]
            return resp(200, {"audit": rows[:500]})
        if parts == ["storage"]:
            return resp(200, {"storage": storage_view(self.store), "bucket": ARENA_BUCKET})
        if parts == ["versions"]:
            info = self.engine_info() or {}
            ms = self.store.manifests(20)
            last = next((m for m in ms if (m.get("engine") or {}).get("rudi_release")), {})
            rows = [["Arena release", "training-arena@" + RELEASE, "this API"],
                    ["Rudi engine (meetrudi-engine-turn)", "whatsapp@" + str(info.get("release") or "unknown"),
                     "personalities: " + ", ".join(info.get("personalities") or []) if info else "not reachable"]]
            if last:
                rows.append(["Last batch answered by", "whatsapp@" + str(last["engine"]["rudi_release"]), last["id"]])
                for k, v in sorted(((last.get("engine") or {}).get("prompt_assets") or {}).items()):
                    rows.append([k, str(v)[:12] + "…" if v else "—", "as read for " + last["id"]])
            return resp(200, {"versions": rows})
        raise Http(404, "not_found", "No such admin route.")


# --------------------------------------------------------------------------- shaping helpers
def _pct_from_hist(hist, q):
    total = sum(hist.values())
    if not total:
        return None
    seen = 0
    for b in sorted(hist, key=lambda x: float("inf") if x == "inf" else int(x)):
        seen += hist[b]
        if seen >= q * total:
            return None if b == "inf" else int(b)
    return None


def day_view(d, date):
    d = d or {}
    roles = dict({"rudi": 0.0, "tester": 0.0, "judge": 0.0, "infra": 0.0}, **(d.get("roles") or {}))
    modes = d.get("modes") or {}
    hist = d.get("lat_hist") or {}
    total = int(d.get("total") or 0)
    return {"date": date, "success": d.get("success", 0), "failed": d.get("failed", 0), "violation": d.get("violation", 0),
            "error": d.get("error", 0), "none": d.get("none", 0), "total": total,
            "p50": _pct_from_hist(hist, 0.5), "p95": _pct_from_hist(hist, 0.95), "models": d.get("models") or {},
            "modes": {m: dict({"n": 0, "eur": 0.0, "max": 0.0, "over": 0}, **(modes.get(m) or {})) for m in ("scripted", "intent", "hybrid")},
            "roles": roles, "cost": d.get("cost") or {}, "infra": roles["infra"],
            "errors": dict({"rate_limited": 0, "timeout": 0, "engine_error": 0, "tester_error": 0, "judge_error": 0}, **(d.get("errors") or {}))}


def user_view(u, spend):
    return {"u": u["u"], "role": u["role"], "status": u.get("status") or "active", "last": display(u.get("last")) or "never",
            "batches": int(u.get("batches") or 0), "created": (u.get("created") or "")[:10], "by": u.get("by") or "",
            "note": u.get("note") or "", "spentToday": float(spend.get(u["u"]) or 0)}


def storage_view(store):
    rows = []
    for prefix, holds, kept in (("batches/", "Inputs, case records, results — one folder per batch", "until a super-admin deletes the batch"),
                                ("inbox/", "Files waiting to be submitted", "30 days"),
                                ("stats/", "Dashboard aggregates", "kept"), ("audit/", "Who did what", "2 years"),
                                ("runtime/", "Leases, limits, dispatcher state", "2 days"),
                                ("users/", "Accounts (password hashes)", "kept")):
        size, n, token = 0, 0, None
        while n < 20000:
            kw = {"Bucket": store.b.name, "Prefix": prefix, "MaxKeys": 1000}
            if token:
                kw["ContinuationToken"] = token
            r = store.b.s3.list_objects_v2(**kw)
            for o in r.get("Contents", []):
                size += int(o.get("Size") or 0)
                n += 1
            if not r.get("IsTruncated"):
                break
            token = r.get("NextContinuationToken")
        mb = size / 1048576
        rows.append([prefix, holds, ("%.1f MB" % mb if mb >= 0.1 else "%d KB" % (size // 1024)) + (" +" if n >= 20000 else ""), kept])
    return rows


def case_view(rec):
    """A case record (result.json) in the shape the console's case viewer renders."""
    c, r, tx = rec["case"], rec["result"], rec.get("transcript") or []
    out_tx = []
    for e in tx:
        meta = e.get("meta") or {}
        if e.get("from") == "user" and meta.get("gap"):
            out_tx.append({"gap": "%s later on Rudi's clock" % meta["gap"]})
        at = parse_iso(e.get("at"))
        m = {}
        if e.get("from") == "user":
            m["source"] = meta.get("source")
            if meta.get("pushing"):
                m["pushing"] = True
            if meta.get("gap"):
                m["gap"] = meta["gap"]
            if meta.get("cost_eur"):
                m["eur"] = meta["cost_eur"]
            note = meta.get("note") or ""
            if meta.get("failover"):
                note = (note + " · " if note else "") + "failover past " + ", ".join(meta["failover"])
            if note:
                m["note"] = note
        else:
            for k_src, k_dst in (("latency_ms", "ms"), ("model", "model"), ("phase", "phase"), ("lang", "lang"), ("cost_eur", "eur")):
                if meta.get(k_src) is not None:
                    m[k_dst] = meta[k_src]
            if meta.get("note"):
                m["note"] = meta["note"]
            g = meta.get("guard") or {}
            if g.get("leak_blocked"):
                m["guard"] = "leak guard replaced the reply"
            elif g.get("pii_redacted"):
                m["guard"] = "identifiers removed: " + ", ".join(g["pii_redacted"])
            if meta.get("retry"):
                m["err"] = "retried after %s" % meta["retry"].get("first_error")
            j = meta.get("judge")
            if j:
                bits = []
                if "success" in j:
                    bits.append("success: %s" % ("YES" if j["success"] else "no" if j["success"] is False else "?"))
                if "abort" in j:
                    bits.append("abort: %s" % ("YES" if j["abort"] else "no" if j["abort"] is False else "?"))
                if "expect" in j:
                    bits.append("expect: %s" % ("pass" if j["expect"] else "fail" if j["expect"] is False else "?"))
                m["judge"] = " · ".join(bits) + ((" — " + j["why"]) if j.get("why") else "")
                m["ok"] = bool(j.get("success")) or j.get("expect") is True
                m["bad"] = bool(j.get("abort")) or j.get("expect") is False
        out_tx.append({"t": e.get("turn"), "f": "u" if e.get("from") == "user" else "r",
                       "at": at.astimezone(TZ).strftime("%H:%M:%S") if at else "", "x": e.get("text") or "", "m": m})
        if e.get("from") == "user" and meta.get("unanswered"):
            out_tx.append({"unanswered": "No reply from Rudi — see Errors."})
    checks = [[ch.get("result"), ch.get("check"), " · ".join(x for x in (ch.get("why"), ch.get("note"),
                                                                       ("€%.5f" % ch["cost_eur"]) if ch.get("cost_eur") else "") if x)]
              for ch in r.get("checks") or []]
    cost = r.get("cost") or {}
    roles = cost.get("by_role") or {}
    return {"id": c["id"], "n": int(c["id"].rsplit("-", 1)[-1]), "key": c.get("key"), "title": c.get("title"), "mode": c.get("mode"),
            "loc": (c.get("locale") or {}).get("requested"), "persona": (c.get("persona") or {}).get("display_name") or "—",
            "outcome": r.get("outcome"), "stop": r.get("stop_reason"), "turns": (r.get("counts") or {}).get("user_turns"),
            "dur": (r.get("timing") or {}).get("duration_s"), "errs": len(r.get("errors") or []), "verdict": r.get("verdict"),
            "checks": checks, "final": r.get("final_state") or {},
            "errors": [[str(e.get("at_turn")), e.get("code"), (e.get("detail") or "") + (" — recovered" if e.get("recovered") else "")]
                       for e in r.get("errors") or []],
            "cost": cost.get("eur"), "cap": cost.get("cap_eur"),
            "roles": {k: roles.get(k, 0.0) for k in ("rudi", "tester", "judge", "infra")}, "tx": out_tx}


# --------------------------------------------------------------------------- admin settings
ENGINE_RANGES = {"max_parallel": (1, 200), "per_batch_parallel": (1, 200), "launch_rate": (1, 600), "think_time": (0, 60),
                 "live_reserve": (0, 900), "account_limit": (1, 10000), "execs_per_case": (1, 4), "max_turns_cap": (1, 200),
                 "turn_timeout": (10, 300), "retries": (0, 5), "backoff": (1, 60), "daily_quota": (1, 1000000),
                 "part_budget": (50000, 480000), "breaker_min": (1, 240), "tester_temp": (0, 1.5), "tester_max_tokens": (100, 2000)}
BUDGET_RANGES = {"month_budget": (0, 100000), "user_day_budget": (0, 10000), "case_default": (0.001, 10),
                 "batch_default": (0.01, 10000), "case_ceiling": (0.001, 10), "batch_ceiling": (0.01, 100000)}


def admin_settings(store):
    """Engine + budgets + cascades, flat, in the console's shape."""
    s, b = store.engine_settings(), store.budgets()
    out = {k: s[k] for k in ENGINE_DEFAULTS}
    out.update({k: b[k] for k in BUDGET_RANGES})
    out["infra_turn"] = b["infra"]["per_turn_eur"]
    out["infra_case"] = b["infra"]["per_case_eur"]
    out["cascades"] = store.cascades()
    return out


def _num(v, lo, hi, name):
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not lo <= v <= hi:
        raise Http(400, "bad_setting", "%s must be between %s and %s." % (name, lo, hi))
    return v


def check_cascades(c):
    for role in ("rudi", "tester", "judge"):
        for e in c.get(role) or []:
            key, ep = str(e.get("key") or ""), str(e.get("endpoint") or "")
            if not ep.startswith("https://") or not e.get("model") or not e.get("name"):
                raise Http(400, "bad_cascade", "%s entry %r needs a name, a model and an https endpoint." % (role, e.get("name")))
            if key in LIVE_KEYS:
                raise Http(400, "bad_cascade", "%s entry %r uses live Rudi's key — the arena must use its own." % (role, e.get("name")))
            if role == "rudi" and not key.startswith("meetrudi/arena/rudi-"):
                raise Http(400, "bad_cascade", "Rudi entries need a key under meetrudi/arena/rudi- (the engine refuses others).")
            if role != "rudi" and key.startswith("meetrudi/arena/rudi-"):
                raise Http(400, "bad_cascade", "The %s may not use a key of Rudi's cascade." % role)
            for f in ("in", "out"):
                _num(e.get(f, 0), 0, 1000, "%s price" % e.get("name"))
        if not any(e.get("on", True) for e in c.get(role) or []):
            raise Http(400, "bad_cascade", "The %s cascade needs at least one entry switched on." % role)


def save_admin_settings(store, new):
    s, b, c = store.engine_settings(), store.budgets(), store.cascades()
    changes = []
    for k, (lo, hi) in ENGINE_RANGES.items():
        if k in new and new[k] != s.get(k):
            changes.append("%s %s → %s" % (k, s.get(k), new[k]))
            s[k] = _num(new[k], lo, hi, k)
    for k in ("breaker", "quiet_only"):
        if k in new and bool(new[k]) != bool(s.get(k)):
            changes.append("%s %s → %s" % (k, s.get(k), bool(new[k])))
            s[k] = bool(new[k])
    for k, (lo, hi) in BUDGET_RANGES.items():
        if k in new and new[k] != b.get(k):
            changes.append("%s %s → %s" % (k, b.get(k), new[k]))
            b[k] = _num(new[k], lo, hi, k)
    for k, f in (("infra_turn", "per_turn_eur"), ("infra_case", "per_case_eur")):
        if k in new and new[k] != b["infra"].get(f):
            changes.append("%s %s → %s" % (k, b["infra"].get(f), new[k]))
            b["infra"][f] = _num(new[k], 0, 1, k)
    if b["case_default"] > b["case_ceiling"] or b["batch_default"] > b["batch_ceiling"]:
        raise Http(400, "bad_setting", "A default cannot be above its ceiling.")
    if "cascades" in new and json.dumps(new["cascades"], sort_keys=True) != json.dumps(c, sort_keys=True):
        check_cascades(new["cascades"])
        changes.append("cascades changed")
        store.put_cascades({r: new["cascades"].get(r) or [] for r in ("rudi", "tester", "judge")})
    store.put_engine_settings(s)
    store.put_budgets(b)
    return changes


# --------------------------------------------------------------------------- Lambda wiring
_API = None


def _build():
    import boto3
    from botocore.config import Config
    region = os.environ.get("AWS_REGION", "eu-central-1")
    # Regional endpoint + SigV4, so a browser PUT to a signed URL is never redirected (a redirect
    # would fail the bucket's CORS check).
    s3 = boto3.client("s3", region_name=region, endpoint_url="https://s3.%s.amazonaws.com" % region,
                      config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
    sqs = boto3.client("sqs")
    lam = boto3.client("lambda")
    store = ArenaStore(s3, ARENA_BUCKET)
    auth = Auth(store, boto3.client("secretsmanager"), os.environ.get("BOOTSTRAP_SECRET", "meetrudi/arena/bootstrap"))

    def send_dispatch(msg):
        sqs.send_message(QueueUrl=DISPATCH_QUEUE_URL, MessageBody=json.dumps(msg), MessageGroupId="dispatch",
                         MessageDeduplicationId="api-%s-%s-%s" % (msg["type"], msg.get("batch", ""), iso()))

    def invoke(fn, payload):
        lam.invoke(FunctionName=fn, InvocationType="Event", Payload=json.dumps(payload).encode("utf-8"))

    def engine_info():
        try:
            r = lam.invoke(FunctionName=ENGINE_FUNCTION, InvocationType="RequestResponse", Payload=b'{"op": "info"}')
            return json.loads(r["Payload"].read() or b"{}")
        except Exception:  # noqa: BLE001
            return {}

    return Api(store, auth, s3, send_dispatch, invoke, engine_info)


def handler(event, context=None):
    global _API
    if _API is None:
        _API = _build()
    return _API.handle(event)
