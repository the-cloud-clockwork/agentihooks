import contextlib
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


@pytest.mark.wall_clock
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


def test_the_guard_installs_its_alarm_handler_and_restores_the_previous_one():
    import signal

    previous = signal.getsignal(signal.SIGALRM)
    budget = PromptBudget(soft=5, hard=5)

    with budget.guarded():
        installed = signal.getsignal(signal.SIGALRM)

    assert installed == budget._on_alarm
    assert budget._installed is True
    assert signal.getsignal(signal.SIGALRM) == previous


def test_the_guard_restores_a_python_alarm_handler():
    import signal

    def outer(*_):
        pass

    previous = signal.signal(signal.SIGALRM, outer)
    try:
        with PromptBudget(soft=5, hard=5).guarded():
            pass
        restored = signal.getsignal(signal.SIGALRM)
    finally:
        signal.signal(signal.SIGALRM, previous)

    assert restored is outer


def _fake_alarm(monkeypatch, left):
    timers = []
    monkeypatch.setattr("hooks.prompt_budget.signal.setitimer", lambda which, seconds: timers.append(seconds))
    budget = PromptBudget(start=0.0, soft=10, hard=left, clock=lambda: 0.0)
    budget._installed = True
    return budget, timers


def test_each_step_arms_the_alarm_for_the_hard_budget_left(monkeypatch):
    budget, timers = _fake_alarm(monkeypatch, 0.25)

    budget.run("step", lambda: None)

    assert timers == [0.25, 0]


def test_an_alarm_during_a_step_marks_the_budget_spent(monkeypatch):
    budget, _ = _fake_alarm(monkeypatch, 5)
    after = Mock()

    budget.run("slow", lambda: budget._on_alarm(None, None))
    budget.run("after", after)

    assert (budget.spent, budget.cut, budget.skipped) == (True, "slow", ["after"])
    after.assert_not_called()


def test_a_fresh_budget_has_spent_nothing_and_holds_no_alarm():
    budget = PromptBudget(soft=5, hard=5)

    assert (budget.spent, budget._armed, budget._installed, budget.running) == (False, False, False, "")


@pytest.mark.parametrize(("now", "ran"), [(9.5, True), (10.0, False)])
def test_the_soft_budget_boundary(now, ran):
    step = Mock()
    budget = PromptBudget(start=0.0, soft=10, hard=20, clock=lambda: now)

    budget.run("step", step)

    assert step.called is ran
    assert budget.spent is (not ran)


def test_the_hard_budget_holds_back_steps_only_while_the_alarm_is_installed():
    step = Mock()
    budget = PromptBudget(start=0.0, soft=30, hard=1, clock=lambda: 1.0)

    budget.run("free", step)
    with budget.guarded():
        budget.run("held", step)

    assert step.call_count == 1
    assert budget.skipped == ["held"]


@pytest.mark.wall_clock
def test_a_nearly_spent_hard_budget_still_cuts_quickly():
    budget = PromptBudget(soft=5, hard=0.3)

    with budget.guarded():
        budget.run("slow", time.sleep, 0.9)

    assert budget.cut == "slow"
    assert budget.spent is True


def test_the_alarm_is_cleared_after_each_step():
    import signal

    with PromptBudget(soft=5, hard=5).guarded() as budget:
        budget.run("fast", lambda: None)
        assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
        assert budget._armed is False


def test_a_cut_inside_a_step_raising_a_block_keeps_the_block():
    from hooks.hook_manager import BlockAction

    budget = PromptBudget(soft=5, hard=5, keep=(BlockAction,))

    def step():
        try:
            raise BlockAction("denied")
        finally:
            budget._on_alarm(None, None)

    with pytest.raises(BlockAction), budget.guarded():
        budget.run("block", step)

    assert budget.cut == ""


def test_a_cut_that_escapes_run_while_a_block_unwinds_keeps_the_block():
    from hooks.hook_manager import BlockAction
    from hooks.prompt_budget import Spent

    budget = PromptBudget(soft=5, hard=5, keep=(BlockAction,))

    with pytest.raises(BlockAction), budget.guarded():
        try:
            raise BlockAction("denied")
        finally:
            raise Spent

    assert budget.spent is False


def test_a_block_wrapped_by_another_error_survives_a_cut():
    from hooks.hook_manager import BlockAction

    budget = PromptBudget(soft=5, hard=5, keep=(BlockAction,))

    def step():
        try:
            raise BlockAction("denied")
        finally:
            try:
                raise OSError("redis down")
            except OSError:
                budget._on_alarm(None, None)

    with pytest.raises(BlockAction), budget.guarded():
        budget.run("block", step)


def test_a_cut_while_a_step_handles_a_block_keeps_the_block():
    from hooks.hook_manager import BlockAction

    budget = PromptBudget(soft=5, hard=5, keep=(BlockAction,))

    def step():
        try:
            raise BlockAction("denied")
        except BlockAction:
            budget._on_alarm(None, None)

    with pytest.raises(BlockAction), budget.guarded():
        budget.run("block", step)


def test_a_cut_inside_a_step_handling_another_error_is_still_a_cut():
    from hooks.hook_manager import BlockAction

    budget = PromptBudget(soft=5, hard=5, keep=(BlockAction,))

    def step():
        try:
            raise OSError("redis down")
        except OSError:
            budget._on_alarm(None, None)

    with budget.guarded():
        assert budget.run("step", step) is None

    assert budget.cut == "step"


def test_a_cut_while_run_settles_keeps_the_block(monkeypatch):
    from hooks.hook_manager import BlockAction
    from hooks.prompt_budget import Spent

    budget = PromptBudget(soft=5, hard=5)
    monkeypatch.setattr(budget, "_disarm", Mock(side_effect=[Spent(), None]))

    def block():
        raise BlockAction("denied")

    with pytest.raises(BlockAction):
        budget.run("block", block)


def test_the_report_names_slow_steps_and_elapsed_time(monkeypatch):
    log = Mock()
    monkeypatch.setattr("hooks.prompt_budget.log", log)
    now = [100.0]
    budget = PromptBudget(start=100.0, soft=10, hard=20, clock=lambda: now[0])

    def step(seconds):
        now[0] += seconds

    budget.run("edge", step, 1.0)
    budget.run("slow", step, 1.234)
    budget.run("fast", step, 0.5)
    budget.report()

    log.assert_called_once_with(
        "prompt hook budget",
        {"elapsed_s": 2.73, "cut": "", "skipped": [], "slow": {"edge": 1.0, "slow": 1.23}},
    )


def test_the_report_names_skipped_steps_and_stays_quiet_otherwise(monkeypatch):
    log = Mock()
    monkeypatch.setattr("hooks.prompt_budget.log", log)

    PromptBudget(soft=5, hard=5).report()
    log.assert_not_called()

    late = PromptBudget(start=0.0, soft=10, hard=20, clock=lambda: 11.0)
    late.run("late", Mock())
    late.report()
    assert log.call_args.args[1]["skipped"] == ["late"]


def test_the_budget_reads_its_settings(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_PROMPT_BUDGET_S", "7")
    monkeypatch.setenv("AGENTIHOOKS_PROMPT_HARD_BUDGET_S", "9")

    budget = PromptBudget()

    assert (budget.soft, budget.hard) == (7.0, 9.0)


def test_the_process_start_falls_back_to_now_without_proc(monkeypatch):
    from hooks.prompt_budget import process_start

    monkeypatch.setattr("hooks.prompt_budget.Path", Mock(side_effect=OSError))

    assert process_start(clock=lambda: 5.0) == 5.0


def test_the_process_start_reads_its_start_tick_from_proc(monkeypatch):
    from types import SimpleNamespace

    import hooks.prompt_budget as pb

    fields = ["S", *(str(n) for n in range(1, 30))]
    fields[19] = "50000"
    stat = "4242 (py (x)) " + " ".join(fields)
    monkeypatch.setattr(pb, "Path", lambda path: SimpleNamespace(read_text=lambda: {"/proc/self/stat": stat}[path]))
    monkeypatch.setattr(pb.time, "clock_gettime", lambda clock: {pb.time.CLOCK_BOOTTIME: 1000.0}[clock])
    monkeypatch.setattr(pb.os, "sysconf", lambda name: {"SC_CLK_TCK": 100}[name])

    assert pb.process_start(clock=lambda: 2000.0) == 1500.0


def test_the_running_step_is_named_only_while_it_runs():
    budget = PromptBudget(soft=5, hard=5)
    seen = []

    budget.run("probe", lambda: seen.append(budget.running))

    assert seen == ["probe"]
    assert budget.running == ""


@pytest.mark.wall_clock
def test_the_process_start_counts_the_time_since_the_interpreter_began():
    code = "import time\nfrom hooks.prompt_budget import process_start\ntime.sleep(1.5)\nprint(time.monotonic() - process_start())"

    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=_PROJECT_ROOT, timeout=60)

    assert 1.4 < float(done.stdout) < 30, done.stderr


class _Recorder:
    def __init__(self, typed):
        self.calls = []
        self._typed = typed

    def run(self, name, step, *args):
        self.calls.append((name, step, args))
        return self._typed if name == "operator_words" else None


@pytest.mark.parametrize("typed", [False, True])
def test_the_prompt_steps_run_in_order_under_their_names(monkeypatch, typed):
    from hooks import config
    from hooks import hook_manager as hm

    monkeypatch.setattr(config, "BROADCAST_ENABLED", True)
    payload = {"session_id": "s", "prompt": "p", "cwd": "/w"}
    budget = _Recorder(typed)

    hm._prompt_steps(budget, payload, "s")

    assert budget.calls == [
        ("secrets", hm._prompt_secrets, (payload,)),
        ("amygdala", hm._prompt_amygdala, ("s",)),
        ("confirm_inbox", hm._confirm_inbox, ("s",)),
        ("swarm_heartbeat", hm._swarm_heartbeat, ("working", "p")),
        ("swarm_ledger", hm._swarm_ledger, (payload,)),
        ("operator_words", hm._operator_words, (payload,)),
        ("operator_mode", hm._operator_mode, (payload, typed)),
        ("master_coding", hm._master_coding, (payload, typed)),
        ("trace_flush", hm._request_trace_flush, (payload, "prompt")),
        ("quota_usage", hm._prompt_quota_usage, ("s",)),
        ("quota_policy", hm._prompt_quota_policy, (payload, "s")),
        ("claude_md_cap", hm._prompt_claude_md_cap, (payload, "s")),
        ("enforcements", hm._prompt_enforcements, (payload, "s")),
        ("ci_manifesto", hm._prompt_ci_manifesto, ("s",)),
        ("rules_refresh", hm._prompt_rules_refresh, ("s",)),
        ("prod_bypass", hm._prompt_prod_bypass, (payload, "s")),
        ("signals", hm._prompt_signals, (payload, "s", typed)),
        *([("gate_lifts", hm._arm_gate_lifts, (payload,))] if typed else []),
        ("voice", hm._prompt_voice, (payload, "s", typed)),
        ("controls", hm._prompt_controls, (payload, "s", typed)),
        ("broadcasts", hm._prompt_broadcasts, ("s",)),
        ("refocus", hm._inject_refocus, ("s", "prompt")),
        ("ledger_decision", hm._inject_ledger_decision, (payload,)),
    ]


@pytest.mark.parametrize("started", [None, 12.5])
def test_the_prompt_handler_budgets_from_the_process_start(monkeypatch, started):
    from contextlib import contextmanager

    from hooks import hook_manager as hm

    seen = {}

    class FakeBudget:
        def __init__(self, start, keep):
            seen["start"] = start
            seen["keep"] = keep

        @contextmanager
        def guarded(self, on_overrun):
            seen["overrun"] = on_overrun
            yield self

    monkeypatch.setattr(hm, "PromptBudget", FakeBudget)
    monkeypatch.setattr(hm, "_EVENT_STARTED", started)
    monkeypatch.setattr(hm, "_prompt_steps", Mock())

    hm.on_user_prompt_submit({"session_id": "s"})

    assert seen == {
        "start": started,
        "keep": (hm.BlockAction,),
        "overrun": hm._prompt_overrun if started is not None else None,
    }


def test_the_watchdog_names_the_running_step_and_flushes(monkeypatch):
    from hooks import hook_manager as hm

    flush, exit_, otel_flush, log = Mock(), Mock(), Mock(), Mock()
    monkeypatch.setattr("hooks.targets.emitter.flush", flush)
    monkeypatch.setattr(hm.os, "_exit", exit_)
    monkeypatch.setattr(hm.otel, "flush", otel_flush)
    monkeypatch.setattr("hooks.prompt_budget.log", log)
    budget = PromptBudget(soft=5, hard=5)
    budget.running = "refocus"

    hm._prompt_overrun(budget)

    flush.assert_called_once_with("UserPromptSubmit")
    otel_flush.assert_called_once_with()
    exit_.assert_called_once_with(0)
    assert log.call_args.args[1]["cut"] == "refocus"


def test_the_watchdog_exits_even_when_its_flush_fails(monkeypatch):
    from hooks import hook_manager as hm

    exit_ = Mock()
    monkeypatch.setattr("hooks.targets.emitter.flush", Mock(side_effect=OSError("closed pipe")))
    monkeypatch.setattr(hm.os, "_exit", exit_)
    monkeypatch.setattr("hooks.prompt_budget.log", Mock())

    with contextlib.suppress(OSError):
        hm._prompt_overrun(PromptBudget(soft=5, hard=5))

    exit_.assert_called_once_with(0)


def test_a_bad_budget_setting_falls_back_to_the_default(monkeypatch):
    from hooks.prompt_budget import seconds

    monkeypatch.setenv("AGENTIHOOKS_PROMPT_BUDGET_S", "10s")

    assert seconds("AGENTIHOOKS_PROMPT_BUDGET_S", 10.0) == 10.0
    assert PromptBudget().soft == 10.0
    for value in ("nan", "inf", "-3", "0"):
        monkeypatch.setenv("AGENTIHOOKS_PROMPT_BUDGET_S", value)
        assert seconds("AGENTIHOOKS_PROMPT_BUDGET_S", 10.0) == 10.0
    monkeypatch.setenv("AGENTIHOOKS_PROMPT_BUDGET_S", "0.5")
    assert seconds("AGENTIHOOKS_PROMPT_BUDGET_S", 10.0) == 0.5


def test_a_cut_that_lands_outside_a_step_never_escapes_the_budget():
    from hooks.prompt_budget import Spent

    budget = PromptBudget(soft=5, hard=5)

    with budget.guarded():
        raise Spent

    assert budget.spent


@pytest.mark.wall_clock
def test_an_outer_real_timer_survives_the_budget():
    import signal

    fired = []

    def outer(*_):
        fired.append(True)

    previous = signal.signal(signal.SIGALRM, outer)
    signal.setitimer(signal.ITIMER_REAL, 50)
    try:
        with PromptBudget(soft=5, hard=0.2).guarded() as budget:
            budget.run("slow", time.sleep, 30)
        remaining, _ = signal.getitimer(signal.ITIMER_REAL)
        restored = signal.getsignal(signal.SIGALRM)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)

    assert remaining > 40
    assert restored is outer
    assert fired == []


_HOOK = """
import runpy
import signal
import sys
import time
from hooks import hook_manager
from hooks.common import inject_context


def blocked(*args, **kwargs):
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
    time.sleep(120)


hook_manager._confirm_inbox = lambda session_id: inject_context("kept block", also_log=False, skip_compression=True)
hook_manager._swarm_heartbeat = blocked if sys.argv[1] == "blocked" else lambda *args, **kwargs: time.sleep(120)
runpy.run_module("hooks", run_name="__main__")
"""


@pytest.mark.wall_clock
@pytest.mark.parametrize("step", ["sleeping", "blocked"])
def test_a_slow_prompt_step_cannot_push_the_hook_past_its_budget(tmp_path, step):
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
        [sys.executable, "-c", _HOOK, step],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=_PROJECT_ROOT,
        env=env,
        timeout=60,
    )

    assert done.returncode == 0, done.stderr
    assert time.monotonic() - began < 25
    assert "kept block" in done.stdout
