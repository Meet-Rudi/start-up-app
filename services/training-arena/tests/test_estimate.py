"""
Launch estimates: worst-case turns, normal vs free slow mode, quiet hours, the free daily reset,
and the end time a person reads. Synthetic numbers only.
"""

import datetime
import unittest

import fakes
from fakes import FakeS3
import arena_estimate as est
import arena_llm
from arena_store import ArenaStore, ENGINE_DEFAULTS, BUDGET_DEFAULTS
from arena_common import TZ


def at(text):
    return datetime.datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=TZ).timestamp()


def free_cascades():
    cas = fakes.cascades()
    cas["rudi"][0].update({"in": 0.0, "out": 0.0, "limits": {"rpm": 30, "rpd": 1000, "tpm": 8000, "tpd": 200000}})
    return cas


CRISIS = {"cases": 4, "turns": 18, "tester_calls": 10, "judge_calls": 10}   # 2 scripted × 4, 2 goal-based × 5
SETTINGS = dict(ENGINE_DEFAULTS, max_parallel=2, per_batch_parallel=2, think_time=2)


def plan(out, slow, quiet):
    return next(p for p in out["plans"] if p["free_slow"] == slow and p["quiet"] == quiet)


class ClockTests(unittest.TestCase):
    def test_quiet_hours_start_at_the_next_2130_and_pause_at_0630(self):
        now = at("2026-10-14 17:40")
        self.assertEqual(est.quiet_start(now), at("2026-10-14 21:30"))
        self.assertEqual(est.quiet_start(at("2026-10-14 23:10")), at("2026-10-14 23:10"))
        end = est.run_until(est.quiet_start(now), 10 * 3600, 1.0, quiet=True)      # 9 h tonight, 1 h tomorrow night
        self.assertEqual(end, at("2026-10-15 22:30"))

    def test_free_day_used_up_continues_after_midnight_utc(self):
        now = at("2026-10-14 17:40")
        end = est.run_until(now, 41, 40.0, per_day=36, today_left=5)
        midnight = datetime.datetime(2026, 10, 15, tzinfo=datetime.timezone.utc).timestamp()
        self.assertEqual(end, midnight + 36 * 40)


class EstimateTests(unittest.TestCase):
    def setUp(self):
        self.now = at("2026-10-14 17:40")
        self.out = est.estimate(CRISIS, free_cascades(), SETTINGS, dict(BUDGET_DEFAULTS), self.now)

    def test_normal_is_quick_and_partly_paid_free_slow_is_slow_and_free(self):
        normal, slow = plan(self.out, False, False), plan(self.out, True, False)
        infra = 4 * 0.0002 + 18 * 0.00015
        self.assertAlmostEqual(slow["eur"], infra, places=6)                    # only the AWS overhead
        self.assertGreater(normal["eur"], slow["eur"] + 0.003)                 # Rudi spills to OVH
        self.assertTrue(0.1 < normal["free_share"]["rudi"] < 0.6)              # what 2 parallel cases leave on Groq
        self.assertEqual((normal["parallel"], slow["parallel"]), (2, 1))
        self.assertLess(normal["duration_s"], 10 * 60)
        self.assertTrue(10 * 60 < slow["duration_s"] < 25 * 60)                 # ≈ 1.5 Rudi replies a minute
        self.assertEqual(normal["start"], "2026-10-14 17:40")
        self.assertRegex(slow["end"], r"^2026-10-14 1[78]:\d\d$")

    def test_quiet_hours_shift_the_start(self):
        for slow in (False, True):
            p = plan(self.out, slow, True)
            self.assertEqual(p["start"], "2026-10-14 21:30")
            self.assertTrue(p["end"] > "2026-10-14 21:30")

    def test_a_spent_free_day_moves_free_slow_past_the_reset(self):
        store = ArenaStore(FakeS3(), "arena")
        tracker = arena_llm.LimitTracker(store.b, clock=lambda: self.now)
        tracker.record(free_cascades()["rudi"][0], 160000)                     # of 85% × 200K
        out = est.estimate(CRISIS, free_cascades(), SETTINGS, dict(BUDGET_DEFAULTS), self.now, tracker)
        self.assertGreaterEqual(plan(out, True, False)["end"], "2026-10-15 02:00")   # 00:00 UTC = 02:00 here
        self.assertIn("worst case", out["assumptions"][0])

    def test_worst_case_turns_for_goal_based_cases(self):
        import arena_costs
        spec = {"mode": "intent", "stop": {"max_turns": 5, "max_attempts": 2, "success_when": {"text": "x"}}, "script": []}
        e = arena_costs.estimate_case(spec, arena_costs.estimate_model(fakes.cascades(), dict(BUDGET_DEFAULTS)))
        self.assertEqual((e["turns"], e["tester_calls"], e["judge_calls"]), (5, 5, 5))


if __name__ == "__main__":
    unittest.main()
