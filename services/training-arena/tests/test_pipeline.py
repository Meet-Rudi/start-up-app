"""
Dispatcher, ingest, assembler and API — and the whole pipeline end to end in memory:
submit → validate → launch → dispatch → run → count → assemble → download.

Asserted: parallelism, launch rate and fairness; batch / user-day / month budgets with
reservations; cancel; lost runners; results planned before writing and streamed; the API's
sign-in, roles and settings guards. Synthetic data only.
"""

import json
import unittest

import yaml

import fakes
from fakes import FakeS3, FakeEngine, FakeTransport, FakeSecretsClient
import arena_llm
import arena_auth
import api as api_mod
import assembler as asm_mod
import dispatcher as disp_mod
import ingest as ingest_mod
import runner
from arena_store import ArenaStore, ENGINE_DEFAULTS, BUDGET_DEFAULTS

arena_auth.ROUNDS = 1000
api_mod.ARENA_BUCKET = "arena"


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def cases_yaml(n, mode="scripted", cap=None, batch_cap=None):
    head = "format: rudi-arena/input@1\n"
    if batch_cap:
        head += "batch: {name: Pipe test, max_cost_eur: %s}\n" % batch_cap
    if cap:
        head += "defaults: {limits: {max_cost_eur: %s}}\n" % cap
    body = "".join("---\ncase: {key: c%d, mode: scripted, script: ['hello', 'walk more']}\n" % i for i in range(1, n + 1))
    return head + body


class World:
    """A whole arena in memory."""

    def __init__(self, settings=None, budgets=None):
        self.s3 = FakeS3()
        self.store = ArenaStore(self.s3, "arena")
        self.store.put_cascades(fakes.cascades())
        self.store.put_engine_settings(dict(ENGINE_DEFAULTS, think_time=0, **(settings or {})))
        self.store.put_budgets(dict(BUDGET_DEFAULTS, **(budgets or {})))
        self.store.b.put_text("prompts/arena_tester_prompt.md", "Play $name.")
        self.store.b.put_text("prompts/arena_judge_prompt.md", "Judge.")
        self.engine = FakeEngine()
        self.clock = Clock()
        self.dispatch_q, self.cases_q, self.invoked = [], [], []
        self.disp = disp_mod.Dispatcher(self.store, self.cases_q.append, lambda p: self.invoked.append(("assembler", p)), self.clock)
        self.secrets_client = FakeSecretsClient({"meetrudi/arena/bootstrap": {"username": "filip", "password": "Bootstrap-pass-1"}})
        self.auth = arena_auth.Auth(self.store, self.secrets_client, clock=self.clock)
        self.api = api_mod.Api(self.store, self.auth, self.s3, self.dispatch_q.append,
                               lambda fn, p: self.invoked.append((fn, p)), lambda: {"release": "abc1234", "personalities": ["seed-rudi-v2"]})

    # -- helpers
    def call(self, method, path, body=None, token=None, query=None):
        ev = {"requestContext": {"http": {"method": method}}, "rawPath": path, "body": json.dumps(body or {}),
              "headers": {"x-arena-token": token} if token else {}, "queryStringParameters": query}
        out = self.api.handle(ev)
        return out["statusCode"], json.loads(out["body"])

    def login(self, name="filip", pw="Bootstrap-pass-1", new="Better-passw0rd!"):
        code, body = self.call("POST", "/login", {"username": name, "password": pw})
        assert code == 200, body
        if body["user"]["must_change"]:
            code2, b2 = self.call("POST", "/account/password", {"current": pw, "new": new}, body["token"])
            assert code2 == 200, b2
        return body["token"]

    def ingest(self, event):
        ing = ingest_mod.Ingest(self.store, lambda key: fakes._Body(self.s3._b("arena")[key]), self.engine,
                                self.dispatch_q.append, lambda p: self.invoked.append(("ingest", p)))
        return ing.run(event)

    def drain_invocations(self):
        while self.invoked:
            fn, p = self.invoked.pop(0)
            if fn in ("meetrudi-arena-ingest", "ingest"):
                self.ingest(p)
            elif fn in ("assembler", "meetrudi-arena-assembler"):
                asm_mod.Assembler(self.store, self.s3, self.dispatch_q.append, lambda p2: self.invoked.append(("assembler", p2))).run(p)

    def tick(self):
        msgs, self.dispatch_q[:] = list(self.dispatch_q), []
        return self.disp.process(msgs)

    def run_cases(self):
        secrets = arena_llm.Secrets(FakeSecretsClient({"meetrudi-groq-tester_and_judge": "g", "meetrudi/arena/sim-ovh": "o"}))
        tracker = arena_llm.LimitTracker(self.store.b, clock=self.clock)
        transport = FakeTransport()
        deps = runner.Deps(self.store, self.engine, self.dispatch_q.append, self.cases_q.append,
                           lambda role, e: arena_llm.Cascade(role, e, tracker, secrets, transport, self.clock),
                           clock=self.clock, sleep=lambda s: None)
        deps.secrets, deps.transport = secrets, transport
        n = 0
        while self.cases_q:
            runner.CaseRun(deps, self.cases_q.pop(0)).run()
            n += 1
        return n

    def submit(self, token, text, name="", filename="b.yaml"):
        key = "inbox/filip/x/" + filename
        self.s3.put_object(Bucket="arena", Key=key, Body=text.encode("utf-8"))
        code, body = self.call("POST", "/batches", {"key": key, "name": name}, token)
        assert code == 201, body
        self.drain_invocations()
        return body["batch"]["id"]

    def launch(self, token, bid):
        code, body = self.call("POST", "/batches/%s/launch" % bid, {"synthetic": True}, token)
        assert code == 200, body
        self.drain_invocations()


class EndToEndTests(unittest.TestCase):
    def test_submit_validate_launch_run_assemble_download(self):
        w = World()
        tok = w.login()
        bid = w.submit(tok, cases_yaml(5), name="Pipe test")
        code, detail = w.call("GET", "/batches/" + bid, token=tok)
        self.assertEqual(detail["batch"]["status"], "ready")
        self.assertEqual(detail["validation"]["cases"], 5)
        self.assertEqual(len(w.store.manifest(bid)["input_sha256"]), 64)
        self.assertEqual(w.call("POST", "/batches/%s/launch" % bid, {}, tok)[0], 400)   # must confirm synthetic data
        w.launch(tok, bid)
        for _ in range(20):
            w.tick()
            if not w.run_cases() and not w.dispatch_q:
                break
        w.tick()
        w.drain_invocations()
        w.tick()
        m = w.store.manifest(bid)
        self.assertEqual(m["status"], "completed")
        self.assertEqual(m["c"]["completed"], 5)
        self.assertEqual(m["parts"], 1)
        part = m["files"][0]
        self.assertTrue(part.startswith(bid + "__pipe-test__results__p01of01"))
        docs = list(yaml.safe_load_all(w.s3.text("arena", "batches/%s/output/%s" % (bid, part))))
        self.assertEqual(len(docs), 6)
        self.assertEqual(docs[0]["summary"]["cases"]["completed"], 5)
        self.assertAlmostEqual(docs[0]["summary"]["cost"]["total_eur"], m["spent"], places=6)
        self.assertGreater(docs[0]["summary"]["cost"]["infra_eur"], 0)
        self.assertEqual(docs[0]["engine"]["rudi_release"], "abc1234")
        csv = w.s3.text("arena", "batches/%s/output/%s__pipe-test__index.csv" % (bid, bid))
        self.assertEqual(len(csv.strip().splitlines()), 6)
        code, url = w.call("GET", "/batches/%s/files/%s" % (bid, part), token=tok)
        self.assertEqual(code, 200)
        self.assertIn("signed.example", url["url"])
        code, case = w.call("GET", "/cases/%s-0002" % bid, token=tok)
        self.assertEqual(case["case"]["outcome"], "completed")
        self.assertEqual(case["case"]["tx"][0]["f"], "r")
        code, dash = w.call("GET", "/dashboard", token=tok, query={"days": "7"})
        today = dash["daily"][-1]
        self.assertEqual(today["total"], 5)
        self.assertGreater(today["roles"]["infra"], 0)
        self.assertIsNotNone(today["p50"])
        self.assertEqual(len(dash["expensive"]), 5)
        self.assertTrue(dash["expensive"][0]["id"].startswith(bid))

    def test_invalid_input_never_launches(self):
        w = World()
        tok = w.login()
        bid = w.submit(tok, "format: rudi-arena/input@1\n---\ncase: {key: a, mode: scripted, script: ['mail tom@example.org']}\n")
        code, d = w.call("GET", "/batches/" + bid, token=tok)
        self.assertEqual(d["batch"]["status"], "invalid")
        self.assertEqual(w.call("POST", "/batches/%s/launch" % bid, {"synthetic": True}, tok)[0], 409)


class DispatchTests(unittest.TestCase):
    def _launched(self, n, settings=None, budgets=None, cap=None, batch_cap=None, by_name="filip"):
        w = World(settings, budgets)
        tok = w.login()
        bid = w.submit(tok, cases_yaml(n, cap=cap, batch_cap=batch_cap))
        w.launch(tok, bid)
        return w, tok, bid

    def test_parallel_slots_and_launch_rate(self):
        w, _tok, bid = self._launched(6, settings={"max_parallel": 2, "per_batch_parallel": 2})
        self.assertEqual(w.tick(), 2)                                   # ceiling: min(2, (10-6)//2)
        self.assertEqual(w.tick(), 0)                                   # nothing finished, no slot
        w, _tok, bid = self._launched(6, settings={"account_limit": 1000, "live_reserve": 6, "max_parallel": 10,
                                                   "per_batch_parallel": 10, "launch_rate": 3})
        self.assertEqual(w.tick(), 3)                                   # launch rate caps the burst

    def test_round_robin_between_batches(self):
        w = World(settings={"account_limit": 1000, "max_parallel": 4, "per_batch_parallel": 2, "launch_rate": 60})
        tok = w.login()
        a = w.submit(tok, cases_yaml(5))
        b = w.submit(tok, cases_yaml(5))
        w.launch(tok, a)
        w.launch(tok, b)
        w.tick()
        per = {}
        for msg in w.cases_q:
            per[msg["batch"]] = per.get(msg["batch"], 0) + 1
        self.assertEqual(per, {a: 2, b: 2})

    def test_batch_budget_reserves_and_then_exhausts(self):
        w, tok, bid = self._launched(5, cap=0.004, batch_cap=0.01, settings={"account_limit": 1000, "max_parallel": 10,
                                                                             "per_batch_parallel": 10, "launch_rate": 60})
        self.assertEqual(w.tick(), 2)                                   # 2 × €0.004 reserved fits €0.01, a 3rd would not
        w.run_cases()
        w.tick()                                                        # actual spend is lower: more can go
        w.run_cases()
        for _ in range(5):
            w.tick()
            w.run_cases()
        w.drain_invocations()
        w.tick()
        m = w.store.manifest(bid)
        self.assertLessEqual(m["spent"], 0.01)
        if m["c"].get("over_budget"):
            self.assertEqual(m["status"], "budget_exhausted")
        rows = w.store.index_rows(bid, 5)
        self.assertTrue(all(r["outcome"] in ("completed", "over_budget") for r in rows))

    def test_user_day_and_month_budgets_hold_batches(self):
        w, tok, bid = self._launched(3, budgets={"user_day_budget": 0.001}, cap=0.002)
        self.assertEqual(w.tick(), 0)
        self.assertIn("daily budget", w.store.manifest(bid)["hold"])
        w, tok, bid = self._launched(3, budgets={"month_budget": 0.001}, cap=0.002)
        self.assertEqual(w.tick(), 0)
        self.assertIn("monthly budget", w.store.manifest(bid)["hold"])

    def test_pause_engine_and_breaker_stop_releases(self):
        w, tok, bid = self._launched(3)
        s = w.store.engine_settings()
        s["state"] = "paused"
        w.store.put_engine_settings(s)
        self.assertEqual(w.tick(), 0)
        s["state"] = "running"
        w.store.put_engine_settings(s)
        w.store.put_runtime("breaker", {"until": w.clock() + 600})
        self.assertEqual(w.tick(), 0)

    def test_cancel_closes_the_rest(self):
        w, tok, bid = self._launched(5)
        w.tick()
        code, _ = w.call("POST", "/batches/%s/cancel" % bid, {}, tok)
        self.assertEqual(code, 200)
        w.tick()
        w.run_cases()                                                   # running cases see the cancel and stop
        w.tick()
        w.drain_invocations()
        w.tick()
        m = w.store.manifest(bid)
        self.assertEqual(m["status"], "cancelled")
        self.assertEqual(m["c"]["cancelled"], 5)

    def test_quiet_hours_batch_says_so_and_can_be_started_now(self):
        w = World()
        tok = w.login()
        bid = w.submit(tok, cases_yaml(2))
        code, body = w.call("POST", "/batches/%s/launch" % bid, {"synthetic": True, "quiet_only": True}, tok)
        self.assertEqual(code, 200, body)
        w.drain_invocations()
        saved = disp_mod.in_quiet_hours, api_mod.in_quiet_hours
        disp_mod.in_quiet_hours = api_mod.in_quiet_hours = lambda *a, **k: False   # mid-afternoon
        try:
            self.assertEqual(w.tick(), 0)
            view = w.call("GET", "/batches/" + bid, token=tok)[1]["batch"]
            self.assertTrue(view["quiet_only"])
            self.assertIn("starts at 21:30", view["eta"])
            code, _ = w.call("POST", "/batches/%s/quiet" % bid, {"quiet_only": False}, tok)
            self.assertEqual(code, 200)
            self.assertEqual(w.tick(), 2)                               # released in the tick that read the control
            view = w.call("GET", "/batches/" + bid, token=tok)[1]["batch"]
            self.assertFalse(view["quiet_only"])
            self.assertNotIn("21:30", view["eta"])
        finally:
            disp_mod.in_quiet_hours, api_mod.in_quiet_hours = saved

    def test_lost_runner_is_requeued_once_then_errors(self):
        w, tok, bid = self._launched(1)
        w.tick()
        w.cases_q.clear()                                               # the runner never ran
        w.clock.t += disp_mod.LEASE_TTL_S + 1
        w.tick()
        self.assertEqual(w.cases_q[0]["attempt"], 2)
        w.cases_q.clear()
        w.clock.t += disp_mod.LEASE_TTL_S + 1
        w.tick()
        self.assertEqual(w.store.manifest(bid)["c"]["error"], 1)
        self.assertEqual(w.store.index_rows(bid, 1)[0]["stop"], "runner_lost")


class AssemblerTests(unittest.TestCase):
    def test_parts_are_streamed_and_split_by_size(self):
        w = World(settings={"part_budget": 12000, "account_limit": 1000, "max_parallel": 20, "per_batch_parallel": 20, "launch_rate": 600})
        tok = w.login()
        bid = w.submit(tok, cases_yaml(30))
        w.launch(tok, bid)
        for _ in range(10):
            w.tick()
            w.run_cases()
        asm_mod.CHUNK = 4096                                            # force the multipart path
        w.tick()
        w.drain_invocations()
        w.tick()
        m = w.store.manifest(bid)
        self.assertGreater(m["parts"], 1)
        seen = []
        for f in m["files"][:-1]:
            docs = list(yaml.safe_load_all(w.s3.text("arena", "batches/%s/output/%s" % (bid, f))))
            self.assertEqual(docs[0]["part"]["of"], m["parts"])
            seen += [d["case"]["id"] for d in docs[1:]]
        self.assertEqual(seen, ["%s-%04d" % (bid, i) for i in range(1, 31)])
        asm_mod.CHUNK = 8 * 1024 * 1024


class ApiTests(unittest.TestCase):
    def test_bootstrap_must_change_and_lockout(self):
        w = World()
        code, body = w.call("POST", "/login", {"username": "filip", "password": "Bootstrap-pass-1"})
        self.assertTrue(body["user"]["must_change"])
        self.assertEqual(w.call("GET", "/dashboard", token=body["token"])[0], 403)
        self.assertEqual(w.call("POST", "/account/password", {"current": "Bootstrap-pass-1", "new": "short"}, body["token"])[0], 400)
        tok = w.login()
        for _ in range(10):
            w.call("POST", "/login", {"username": "filip", "password": "wrong"})
        self.assertEqual(w.call("POST", "/login", {"username": "filip", "password": "Better-passw0rd!"})[0], 403)
        self.assertEqual(w.call("GET", "/me", token=tok)[0], 401)          # a locked account loses its sessions

    def test_no_bootstrap_secret_fails_closed(self):
        w = World()
        w.auth.secrets = FakeSecretsClient({})
        self.assertEqual(w.call("POST", "/login", {"username": "filip", "password": "x"})[0], 503)

    def test_roles_and_user_admin(self):
        w = World()
        tok = w.login()
        code, body = w.call("POST", "/admin/users", {"username": "anke", "role": "tester"}, tok)
        self.assertEqual(code, 201)
        anke = w.login("anke", body["temp_password"], "Anke-passw0rd!")
        self.assertEqual(w.call("GET", "/admin/settings", token=anke)[0], 403)
        bid = w.submit(tok, cases_yaml(1))
        self.assertEqual(w.call("POST", "/batches/%s/cancel" % bid, {}, anke)[0], 403)   # not hers
        self.assertEqual(w.call("DELETE", "/admin/users/filip", token=tok)[0], 409)        # not yourself
        code, d = w.call("GET", "/dashboard", token=anke)
        self.assertEqual([u["u"] for u in d["users"]], ["anke"])                          # a tester sees only her own spend

    def test_settings_guards(self):
        w = World()
        tok = w.login()
        code, s = w.call("GET", "/admin/settings", token=tok)
        self.assertEqual(s["settings"]["infra_turn"], BUDGET_DEFAULTS["infra"]["per_turn_eur"])
        cas = s["settings"]["cascades"]
        bad = json.loads(json.dumps(cas))
        bad["tester"][0]["key"] = "meetrudi-groq-firstkey"
        self.assertEqual(w.call("PUT", "/admin/settings", {"settings": {"cascades": bad}}, tok)[1]["error"], "bad_cascade")
        bad = json.loads(json.dumps(cas))
        bad["judge"][0]["key"] = "meetrudi/arena/rudi-groq"
        self.assertEqual(w.call("PUT", "/admin/settings", {"settings": {"cascades": bad}}, tok)[1]["error"], "bad_cascade")
        code, out = w.call("PUT", "/admin/settings", {"settings": {"max_parallel": 3, "infra_turn": 0.0002}}, tok)
        self.assertEqual(code, 200)
        self.assertIn("max_parallel 2 → 3", out["changed"])
        self.assertEqual(w.store.budgets()["infra"]["per_turn_eur"], 0.0002)
        self.assertEqual(w.call("PUT", "/admin/settings", {"settings": {"case_default": 0.9}}, tok)[1]["error"], "bad_setting")


if __name__ == "__main__":
    unittest.main()
