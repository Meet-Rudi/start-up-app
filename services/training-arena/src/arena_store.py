"""
MEET_RUDI Training Arena — the layout of the arena bucket, in one place.

    batches/<id>/manifest.json                     the batch (dispatcher is its only writer once launched)
    batches/<id>/input/<file>                      as submitted
    batches/<id>/validation.json
    batches/<id>/cases/<seq>/spec.json             resolved case
    batches/<id>/cases/<seq>/checkpoint.json       in-flight state
    batches/<id>/cases/<seq>/result.json|.yaml     the case record and its rendered document
    batches/<id>/index/<block>.json                one row per case, 1,000 cases a block
    batches/<id>/output/…                          plan, results parts, index CSV
    settings/engine.json · settings/budgets.json   super-admin settings
    config/cascades.json · config/evaluators.json  the arena's own models and evaluators
    defaults/…                                     seeded by deploy; read when the live copy is absent
    prompts/arena_*.md                             tester and judge prompts (seeded by deploy)
    runtime/leases/<id>/<seq>.json · runtime/*     in-flight accounting
    stats/daily/<day>.json · stats/users/<day>.json · stats/keys.json
    users/<name>.json · sessions/<sha256>.json · audit/<yyyy>/<mm>/<dd>/…

Deploy seeds `defaults/` and `prompts/` only, so a redeploy never overwrites what a super-admin
changed in the console.
"""

import copy

from arena_common import Bucket, iso, local_day, new_code

BLOCK = 1000

ENGINE_DEFAULTS = {
    "account_limit": 10, "live_reserve": 6, "execs_per_case": 2,
    "max_parallel": 2, "per_batch_parallel": 2, "launch_rate": 6, "think_time": 2,
    "max_turns_cap": 40, "turn_timeout": 60, "retries": 2, "backoff": 4, "daily_quota": 500,
    "part_budget": 450000, "breaker": True, "breaker_min": 10, "quiet_only": False,
    "tester_temp": 0.7, "tester_max_tokens": 400, "state": "running",
}
BUDGET_DEFAULTS = {
    "month_budget": 150.0, "user_day_budget": 20.0, "case_default": 0.05, "batch_default": 10.0,
    "case_ceiling": 0.50, "batch_ceiling": 100.0,
    "infra": {"per_turn_eur": 0.00015, "per_case_eur": 0.0002},
}
EVALUATOR_DEFAULTS = {"default": "llm-judge", "llm_judge": {"max_tokens": 700}, "evaluators": [], "routes": []}


def _merged(defaults, value):
    out = copy.deepcopy(defaults)
    for k, v in (value or {}).items():
        out[k] = _merged(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


class ArenaStore:
    def __init__(self, s3, bucket_name):
        self.b = Bucket(s3, bucket_name)
        self._prompts = {}

    # ------------------------------------------------------------------ settings and config
    def _configured(self, key, defaults):
        live = self.b.get_json(key)
        if live is None:
            live = self.b.get_json("defaults/" + key)
        return _merged(defaults, live)

    def engine_settings(self):
        return self._configured("settings/engine.json", ENGINE_DEFAULTS)

    def budgets(self):
        return self._configured("settings/budgets.json", BUDGET_DEFAULTS)

    def cascades(self):
        c = self.b.get_json("config/cascades.json") or self.b.get_json("defaults/config/cascades.json") or {}
        return {r: list(c.get(r) or []) for r in ("rudi", "tester", "judge")}

    def evaluators(self):
        return self._configured("config/evaluators.json", EVALUATOR_DEFAULTS)

    def put_engine_settings(self, s):
        self.b.put_json("settings/engine.json", s)

    def put_budgets(self, b):
        self.b.put_json("settings/budgets.json", b)

    def put_cascades(self, c):
        self.b.put_json("config/cascades.json", c)

    def prompt(self, name):
        if name not in self._prompts:
            text = self.b.get_text("prompts/" + name)
            if text is None:
                raise RuntimeError("prompt %s is missing from the arena bucket — run python deploy.py training-arena" % name)
            self._prompts[name] = text
        return self._prompts[name]

    # ------------------------------------------------------------------ batches
    @staticmethod
    def _bk(bid, rest):
        return "batches/%s/%s" % (bid, rest)

    def manifest(self, bid):
        return self.b.get_json(self._bk(bid, "manifest.json"))

    def put_manifest(self, m):
        m["updated_at"] = iso()
        self.b.put_json(self._bk(m["id"], "manifest.json"), m)

    def batch_ids(self, limit=300):
        """Most recent first (IDs start with the day, so they sort by submission day)."""
        resp = self.b.s3.list_objects_v2(Bucket=self.b.name, Prefix="batches/", Delimiter="/", MaxKeys=1000)
        ids = [p["Prefix"].split("/")[1] for p in resp.get("CommonPrefixes", [])]
        return sorted(ids, reverse=True)[:limit]

    def manifests(self, limit=300):
        out = []
        for bid in self.batch_ids(limit):
            m = self.manifest(bid)
            if m:
                out.append(m)
        return sorted(out, key=lambda m: m.get("submitted_at") or "", reverse=True)

    def input_key(self, bid, filename):
        return self._bk(bid, "input/" + filename)

    def validation(self, bid):
        return self.b.get_json(self._bk(bid, "validation.json"))

    def put_validation(self, bid, report):
        self.b.put_json(self._bk(bid, "validation.json"), report)

    # ------------------------------------------------------------------ cases
    def spec(self, bid, seq):
        return self.b.get_json(self._bk(bid, "cases/%06d/spec.json" % seq))

    def put_spec(self, bid, seq, spec):
        self.b.put_json(self._bk(bid, "cases/%06d/spec.json" % seq), spec)

    def checkpoint(self, bid, seq):
        return self.b.get_json(self._bk(bid, "cases/%06d/checkpoint.json" % seq))

    def put_checkpoint(self, bid, seq, cp):
        self.b.put_json(self._bk(bid, "cases/%06d/checkpoint.json" % seq), cp)

    def delete_checkpoint(self, bid, seq):
        self.b.delete(self._bk(bid, "cases/%06d/checkpoint.json" % seq))

    def result(self, bid, seq):
        return self.b.get_json(self._bk(bid, "cases/%06d/result.json" % seq))

    def result_yaml(self, bid, seq):
        return self.b.get_text(self._bk(bid, "cases/%06d/result.yaml" % seq))

    def result_yaml_key(self, bid, seq):
        return self._bk(bid, "cases/%06d/result.yaml" % seq)

    def put_result(self, bid, seq, record, yaml_text):
        self.b.put_text(self._bk(bid, "cases/%06d/result.yaml" % seq), yaml_text, "application/yaml; charset=utf-8")
        self.b.put_json(self._bk(bid, "cases/%06d/result.json" % seq), record)

    # ------------------------------------------------------------------ batch index
    def index_block(self, bid, block):
        return self.b.get_json(self._bk(bid, "index/%05d.json" % block), {})

    def put_index_block(self, bid, block, rows):
        self.b.put_json(self._bk(bid, "index/%05d.json" % block), rows)

    def index_rows(self, bid, total):
        rows = []
        for block in range(0, (int(total or 0) + BLOCK - 1) // BLOCK + 1):
            data = self.index_block(bid, block)
            rows += [data[k] for k in sorted(data, key=int)]
        return [r for r in rows if r]

    # ------------------------------------------------------------------ leases
    @staticmethod
    def lease_key(bid, seq):
        return "runtime/leases/%s/%06d.json" % (bid, seq)

    def lease(self, bid, seq):
        return self.b.get_json(self.lease_key(bid, seq))

    def put_lease(self, lease):
        self.b.put_json(self.lease_key(lease["batch"], lease["seq"]), lease)

    def delete_lease(self, bid, seq):
        self.b.delete(self.lease_key(bid, seq))

    def leases(self):
        return [lz for lz in (self.b.get_json(k) for k in self.b.list_keys("runtime/leases/")) if lz]

    # ------------------------------------------------------------------ runtime state
    def runtime(self, name, default=None):
        return self.b.get_json("runtime/%s.json" % name, default)

    def put_runtime(self, name, obj):
        self.b.put_json("runtime/%s.json" % name, obj)

    # ------------------------------------------------------------------ stats
    def daily(self, day):
        return self.b.get_json("stats/daily/%s.json" % day, None)

    def put_daily(self, day, obj):
        self.b.put_json("stats/daily/%s.json" % day, obj)

    def users_day(self, day):
        return self.b.get_json("stats/users/%s.json" % day, {})

    def put_users_day(self, day, obj):
        self.b.put_json("stats/users/%s.json" % day, obj)

    def keys_stats(self):
        return self.b.get_json("stats/keys.json", {})

    def put_keys_stats(self, obj):
        self.b.put_json("stats/keys.json", obj)

    # ------------------------------------------------------------------ users, sessions, audit
    def user(self, name):
        return self.b.get_json("users/%s.json" % name)

    def put_user(self, u):
        self.b.put_json("users/%s.json" % u["u"], u)

    def delete_user(self, name):
        self.b.delete("users/%s.json" % name)

    def users(self):
        return [u for u in (self.b.get_json(k) for k in self.b.list_keys("users/")) if u]

    def session(self, digest):
        return self.b.get_json("sessions/%s.json" % digest)

    def put_session(self, digest, s):
        self.b.put_json("sessions/%s.json" % digest, s)

    def delete_session(self, digest):
        self.b.delete("sessions/%s.json" % digest)

    def sessions(self):
        return [(k.split("/")[-1][:-5], self.b.get_json(k)) for k in self.b.list_keys("sessions/")]

    def audit(self, actor, action, target="", detail=""):
        when = iso()
        key = "audit/%s/%s-%s.json" % (when[:10].replace("-", "/"), when.replace(":", ""), new_code(4))
        self.b.put_json(key, {"at": when, "who": actor, "action": action, "target": target, "detail": detail})

    def audit_entries(self, days):
        out = []
        for day in days:
            for k in self.b.list_keys("audit/%s/" % day.replace("-", "/")):
                e = self.b.get_json(k)
                if e:
                    out.append(e)
        return sorted(out, key=lambda e: e.get("at") or "", reverse=True)


def today():
    return local_day()
