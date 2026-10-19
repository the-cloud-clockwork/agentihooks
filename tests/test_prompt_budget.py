import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from hooks.prompt_budget import PromptBudget

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _swallowing_sleep(seconds):
    try:
        time.sleep(seconds)
    except Exception:
        time.sleep(seconds)


def test_a_step_still_running_at_the_hard_budget_is_cut_and_the_rest_skipped():
    budget = PromptBudget(soft=5, hard=0.3)
    after = Mock()
    began = time.monotonic()

    with budget.guarded():
        first = budget.run("first", lambda: "kept")
        slow = budget.run("slow", _swallowing_sleep, 30)
        budget.run("after", after)

    assert time.monotonic() - began < 2
    assert (first, slow) == ("kept", None)
    assert budget.cut == "slow"
    assert budget.skipped == ["after"]
    after.assert_not_called()


def test_steps_after_the_soft_budget_are_skipped_without_running():
    now = [0.0]
    budget = PromptBudget(start=0.0, soft=10, hard=20, clock=lambda: now[0])
    late = Mock()

    def slow():
        now[0] += 11

    budget.run("slow", slow)
    budget.run("late", late)

    late.assert_not_called()
    assert budget.skipped == ["late"]
    assert budget.cut == ""


def test_the_budget_counts_from_the_given_process_start():
    now = [100.0]
    budget = PromptBudget(start=89.0, soft=10, hard=20, clock=lambda: now[0])
    step = Mock()

    budget.run("step", step)

    step.assert_not_called()
    assert budget.skipped == ["step"]


def test_a_block_action_from_a_step_still_propagates():
    from hooks.hook_manager import BlockAction

    budget = PromptBudget(soft=5, hard=5)

    def block():
        raise BlockAction("denied")

    with pytest.raises(BlockAction), budget.guarded():
        budget.run("block", block)


def test_the_alarm_is_cleared_after_the_last_step():
    import signal

    previous = signal.getsignal(signal.SIGALRM)
    budget = PromptBudget(soft=5, hard=0.2)

    with budget.guarded():
        budget.run("fast", lambda: None)

    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert signal.getsignal(signal.SIGALRM) == previous


def test_the_budget_logs_the_cut_and_skipped_steps(monkeypatch):
    log = Mock()
    monkeypatch.setattr("hooks.prompt_budget.log", log)
    budget = PromptBudget(soft=5, hard=0.2)

    with budget.guarded():
        budget.run("slow", time.sleep, 30)
        budget.run("after", Mock())

    message, fields = log.call_args.args
    assert message == "prompt hook budget"
    assert fields["cut"] == "slow"
    assert fields["skipped"] == ["after"]


_SLOW_HOOK = """
import time
from hooks import hook_manager
from hooks.common import inject_context
from hooks.prompt_budget import process_start

hook_manager._confirm_inbox = lambda session_id: inject_context("kept block", also_log=False, skip_compression=True)
hook_manager._swarm_heartbeat = lambda *args, **kwargs: time.sleep(120)
hook_manager._EVENT_STARTED = process_start()
hook_manager.main()
"""


def test_a_slow_prompt_step_cannot_push_the_hook_past_its_budget(tmp_path):
    env = {
        **os.environ,
        "AGENTIHOOKS_HOME": str(tmp_path / "home"),
        "AGENTIHOOKS_TARGET": "claude",
        "AGENTIHOOKS_PROMPT_BUDGET_S": "8",
        "AGENTIHOOKS_PROMPT_HARD_BUDGET_S": "10",
    }
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": "budget", "prompt": "hello", "cwd": ""}
    began = time.monotonic()

    done = subprocess.run(
        [sys.executable, "-c", _SLOW_HOOK],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=_PROJECT_ROOT,
        env=env,
        timeout=60,
    )

    assert done.returncode == 0, done.stderr
    assert time.monotonic() - began < 20
    assert "kept block" in done.stdout
