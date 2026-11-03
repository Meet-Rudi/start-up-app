"""
MEET_RUDI platform — the 13-minute rule.

AWS kills a Lambda at 15 minutes, mid-write if need be. Our rule (decision #32): no instance does
work past 13 minutes. Long work starts handing over to a successor at 12 minutes, so the
checkpoint is written and the successor invoked well before 13, and the last two minutes stay
free for closing cleanly. Functions that may run long keep their CloudFormation Timeout at 900 s
so AWS's timer is only the backstop.

Shorter functions follow the same proportions: a 60-second Lambda works for at most 52 seconds
and starts handing over at 48.

    budget = RunBudget.for_lambda(context)
    for item in work:
        if budget.should_hand_over():
            return hand_over(...)        # runtime.handover
        process(item)
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

LAMBDA_HARD_LIMIT_S = 900.0  # AWS's own limit
MAX_RUN_S = 780.0  # 13 minutes: no instance works longer
HANDOVER_LEAD_S = 60.0  # start handing over this long before MAX_RUN_S
KILL_MARGIN_S = LAMBDA_HARD_LIMIT_S - MAX_RUN_S


class RunBudget:
    """How long this run may keep working, measured from when it was created."""

    def __init__(
        self,
        run_s: float = MAX_RUN_S,
        lead_s: float = HANDOVER_LEAD_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if run_s <= 0:
            raise ValueError("run_s must be positive")
        if not 0 <= lead_s < run_s:
            raise ValueError("lead_s must be between 0 and run_s")
        self.run_s = float(run_s)
        self.lead_s = float(lead_s)
        self._clock = clock
        self._start = clock()

    @classmethod
    def for_lambda(
        cls, context: Any = None, clock: Callable[[], float] = time.monotonic
    ) -> RunBudget:
        """Budget for the current invocation, scaled to the time AWS has left it.

        `context` is the Lambda context object (only get_remaining_time_in_millis is used);
        without one, the full 15-minute limit is assumed.
        """
        remaining = LAMBDA_HARD_LIMIT_S
        if context is not None:
            remaining = max(0.0, context.get_remaining_time_in_millis() / 1000.0)
        run_s = min(MAX_RUN_S, remaining * MAX_RUN_S / LAMBDA_HARD_LIMIT_S)
        if run_s <= 0:
            raise ValueError("no time left in this invocation")
        lead_s = run_s * HANDOVER_LEAD_S / MAX_RUN_S
        return cls(run_s, lead_s, clock)

    def elapsed(self) -> float:
        return self._clock() - self._start

    def remaining(self) -> float:
        """Seconds of work left before the run must have stopped."""
        return max(0.0, self.run_s - self.elapsed())

    def should_hand_over(self) -> bool:
        """True once the hand-over lead has begun: checkpoint and invoke a successor now."""
        return self.elapsed() >= self.run_s - self.lead_s

    def exhausted(self) -> bool:
        """True once the run has used its whole budget; do nothing but return."""
        return self.elapsed() >= self.run_s
