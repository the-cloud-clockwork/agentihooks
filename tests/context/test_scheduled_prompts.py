import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hooks import hook_manager
from hooks.context import operator_mode, operator_words, scheduled_prompts
from hooks.context.scheduled_prompts import AUTOMATED
from scripts.swarm import delivery

pytestmark = pytest.mark.unit

MASTER = {"AGENTIHOOKS_SWARM": "demo", "AGENTIHOOKS_AGENT_NAME": "master@a1-1"}
PRESENT = "Operator present in this pane during the quiet window. Answer his message."
NUDGE = "Safety Nudge: In case the main tick or controller is down. Read the swarm status and act on what is stale."
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def ledgers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import fakeredis

    redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("scripts.swarm.store.redis_client", lambda: redis)
    monkeypatch.setattr("scripts.scheduler.service.ensure_running", lambda: True)
    path = tmp_path / "ledgers"
    (path / ".sessions").mkdir(parents=True)
    monkeypatch.setenv("LEDGER_DIR", str(path))
    for name in ("AGENTIHOOKS_SWARM", "AGENTIHOOKS_AGENT_NAME", "AGENTIHOOKS_SWARM_TASK"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(hook_manager, "_swarm_heartbeat", lambda *a, **k: None)
    return path


def bind(monkeypatch: pytest.MonkeyPatch, env: dict[str, str] = MASTER) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def schedule(session: str, prompt: str, tool: str = "CronCreate") -> None:
    hook_manager._scheduled_prompt(
        {"session_id": session, "tool_name": tool, "tool_input": {"cron": "17 * * * *", "prompt": prompt}}
    )


def prompt(capsys: pytest.CaptureFixture[str], session: str, text: str) -> str:
    hook_manager.on_user_prompt_submit({"session_id": session, "prompt": text, "hook_event_name": "UserPromptSubmit"})
    return capsys.readouterr().out


def opened(capsys: pytest.CaptureFixture[str], session: str) -> None:
    prompt(capsys, session, "You are master@a1-1, the master of swarm demo")


@pytest.mark.feature("operator-presence-automated-prompts")
@pytest.mark.parametrize("tool", ["CronCreate", "ScheduleWakeup"])
def test_a_session_cron_prompt_in_a_swarm_master_is_automated_never_operator_presence(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tool: str
) -> None:
    bind(monkeypatch)
    opened(capsys, "m1")
    schedule("m1", NUDGE, tool)
    out = prompt(capsys, "m1", NUDGE)
    assert PRESENT not in out
    assert AUTOMATED in out
    assert not operator_mode.present("m1", MASTER)
    assert operator_mode._load("m1").get("pane_at") is None


@pytest.mark.feature("operator-presence-automated-prompts")
def test_a_cron_prompt_never_switches_presence_on_or_off(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bind(monkeypatch)
    opened(capsys, "m1")
    schedule("m1", "Reminder: type operator on when done")
    out = prompt(capsys, "m1", "Reminder: type operator on when done")
    assert operator_mode.ON_NOTICE not in out
    assert not operator_mode.present("m1", MASTER)
    assert operator_mode.ON_NOTICE in prompt(capsys, "m1", "operator on")
    schedule("m1", "Reminder: operator off at noon")
    out = prompt(capsys, "m1", "Reminder: operator off at noon")
    assert operator_mode.OFF_NOTICE not in out
    assert AUTOMATED in out
    assert operator_mode.present("m1", MASTER)
    assert operator_mode.OFF_NOTICE in prompt(capsys, "m1", "operator off")
    assert not operator_mode.present("m1", MASTER)


@pytest.mark.feature("operator-presence-automated-prompts")
def test_a_swarm_delivered_nudge_is_told_it_is_automated(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bind(monkeypatch)
    opened(capsys, "m1")
    out = prompt(capsys, "m1", delivery.marked(NUDGE))
    assert AUTOMATED in out
    assert PRESENT not in out
    assert not operator_mode.present("m1", MASTER)


@pytest.mark.feature("operator-presence-automated-prompts")
def test_the_hourly_safety_nudge_as_sent_to_a_master_pane_is_automated(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.swarm import safety_nudge

    sent: list[str] = []

    def herdr(argv: list[str]) -> dict[str, str]:
        if argv[:2] == ["agent", "prompt"]:
            sent.append(argv[-1])
        return {}

    messenger = delivery.HerdrMessenger(herdr=herdr)
    monkeypatch.setattr(messenger, "agent_status", lambda agent: next(iter(delivery.READY)))
    monkeypatch.setattr(messenger, "typed_input", lambda agent: "")
    entry = {"seat": "master@demo", "harness": "claude", "pane": "w:p1"}
    assert safety_nudge.nudge(messenger, entry) == "sent"
    assert sent == [delivery.marked(safety_nudge.TEXT)]
    bind(monkeypatch)
    opened(capsys, "m1")
    out = prompt(capsys, "m1", sent[0])
    assert AUTOMATED in out
    assert PRESENT not in out
    assert not operator_mode.present("m1", MASTER)


@pytest.mark.feature("operator-presence-automated-prompts")
def test_a_typed_operator_message_opens_a_reply_and_is_never_called_automated(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bind(monkeypatch)
    opened(capsys, "m1")
    schedule("m1", NUDGE)
    out = prompt(capsys, "m1", "Why is the hub slow?")
    assert PRESENT in out
    assert AUTOMATED not in out
    assert operator_mode.present("m1", MASTER)


@pytest.mark.feature("operator-presence-automated-prompts")
def test_a_cron_inside_the_quiet_window_is_still_automated(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bind(monkeypatch)
    opened(capsys, "m1")
    schedule("m1", NUDGE)
    prompt(capsys, "m1", "Why is the hub slow?")
    out = prompt(capsys, "m1", NUDGE)
    assert AUTOMATED in out
    assert PRESENT not in out


@pytest.mark.feature("operator-presence-automated-prompts")
def test_a_swarm_worker_starts_off_and_a_cron_keeps_it_off(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    worker = {"AGENTIHOOKS_SWARM": "demo", "AGENTIHOOKS_AGENT_NAME": "engineer@a1-2"}
    bind(monkeypatch, worker)
    assert not operator_mode.present("w1", worker)
    prompt(capsys, "w1", "You are engineer@a1-2 in swarm demo")
    schedule("w1", NUDGE)
    out = prompt(capsys, "w1", NUDGE)
    assert AUTOMATED in out
    assert operator_mode.WORKER_ROUTE in out
    assert not operator_mode.present("w1", worker)


@pytest.mark.feature("operator-presence-automated-prompts")
def test_a_standalone_session_keeps_the_operator_present_and_gets_no_automated_notice(
    capsys: pytest.CaptureFixture[str],
) -> None:
    schedule("solo", NUDGE)
    out = prompt(capsys, "solo", NUDGE)
    assert AUTOMATED not in out
    assert operator_mode.present("solo", {"LEDGER_DIR": os.environ["LEDGER_DIR"]})


@pytest.mark.feature("operator-presence-automated-prompts")
def test_a_cron_prompt_is_never_recorded_as_the_operator_words(monkeypatch: pytest.MonkeyPatch) -> None:
    bind(monkeypatch)
    schedule("m1", NUDGE)
    assert not operator_words.typed({"session_id": "m1", "prompt": NUDGE})
    assert not operator_words.typed({"session_id": "m1", "prompt": "  " + NUDGE.replace(" ", "\n", 1)})
    assert operator_words.matching("master@a1-1", NUDGE) == ""


@pytest.mark.feature("operator-presence-automated-prompts")
def test_only_scheduler_tools_with_a_prompt_are_recorded_and_the_newest_are_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert not scheduled_prompts.record({"session_id": "s", "tool_name": "Bash", "tool_input": {"prompt": "x"}})
    assert not scheduled_prompts.record({"session_id": "s", "tool_name": "CronCreate", "tool_input": {"prompt": " "}})
    assert not scheduled_prompts.record({"tool_name": "CronCreate", "tool_input": {"prompt": "x"}})
    assert not scheduled_prompts.record({"session_id": "s", "tool_name": "CronCreate", "tool_input": "x"})
    for n in range(scheduled_prompts.KEPT + 1):
        assert scheduled_prompts.record(
            {"session_id": "s", "tool_name": "CronCreate", "tool_input": {"prompt": f"p{n}"}}
        )
    assert not scheduled_prompts.fired("s", "p0")
    assert scheduled_prompts.fired("s", "p1")
    assert scheduled_prompts.fired("s", f"p{scheduled_prompts.KEPT}")
    monkeypatch.setattr(scheduled_prompts, "_agent", lambda: "agent-2")
    assert not scheduled_prompts.fired("other", "p1")
    assert not scheduled_prompts.fired("", "p1")
    assert not scheduled_prompts.fired("s", "")


@pytest.mark.feature("operator-presence-automated-prompts")
def test_an_unreadable_record_counts_no_prompt_as_scheduled() -> None:
    path = scheduled_prompts._path("s")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert not scheduled_prompts.fired("s", "p")
    path.write_text(json.dumps({"p": 1}))
    assert not scheduled_prompts.fired("s", "p")
    path.write_text(json.dumps(["p", 3]))
    assert scheduled_prompts.fired("s", "p")


@pytest.mark.feature("operator-presence-automated-prompts")
def test_an_unwritable_record_home_records_nothing_and_never_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setattr(scheduled_prompts, "_path", lambda session_id: blocker / "under" / "s.json")
    payload = {"session_id": "s", "tool_name": "CronCreate", "tool_input": {"prompt": "x"}}
    assert not scheduled_prompts.record(payload)
    hook_manager._scheduled_prompt(payload)
    assert not scheduled_prompts.fired("s", "x")


@pytest.mark.feature("operator-presence-automated-prompts")
def test_a_cleared_session_in_the_same_agent_still_finds_its_crons(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(scheduled_prompts, "_agent", lambda: "agent-7")
    bind(monkeypatch)
    schedule("before", NUDGE)
    hook_manager.on_session_end({"session_id": "before", "reason": "clear"})
    opened(capsys, "after")
    out = prompt(capsys, "after", NUDGE)
    assert AUTOMATED in out
    assert PRESENT not in out
    monkeypatch.setattr(scheduled_prompts, "_agent", lambda: "agent-8")
    assert not scheduled_prompts.fired("after", NUDGE)


@pytest.mark.feature("operator-presence-automated-prompts")
def test_session_end_drops_the_record_and_a_clear_keeps_the_agent_record(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduled_prompts, "_agent", lambda: "agent-7")
    schedule("s", "p")
    scheduled_prompts.forget({"session_id": "s", "reason": "clear"})
    assert not scheduled_prompts._path("s").exists()
    assert scheduled_prompts.fired("t", "p")
    schedule("t", "p")
    scheduled_prompts.forget({"session_id": "t", "reason": "prompt_input_exit"})
    assert not scheduled_prompts._path("t").exists()
    assert not scheduled_prompts._path("agent-7").exists()
    assert not scheduled_prompts.fired("t", "p")
    scheduled_prompts.forget({"reason": "other"})


@pytest.mark.feature("operator-presence-automated-prompts")
def test_each_owner_keeps_its_record_in_one_named_file_under_the_state_home(monkeypatch: pytest.MonkeyPatch) -> None:
    from hooks import config

    assert (
        scheduled_prompts._path("Ab@x.y-z_1 /") == config.AGENTIHOOKS_HOME / "scheduled_prompts" / "Ab@x.y-z_1__.json"
    )
    monkeypatch.setattr(scheduled_prompts, "_agent", lambda: "agent-7")
    schedule("Sess", "p")
    assert json.loads((config.AGENTIHOOKS_HOME / "scheduled_prompts" / "Sess.json").read_text()) == ["p"]
    assert json.loads((config.AGENTIHOOKS_HOME / "scheduled_prompts" / "agent-7.json").read_text()) == ["p"]


@pytest.mark.feature("operator-presence-automated-prompts")
def test_a_record_creates_every_missing_parent_folder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(scheduled_prompts, "_path", lambda owner: tmp_path / "a" / "b" / f"{owner}.json")
    assert scheduled_prompts.record({"session_id": "s", "tool_name": "CronCreate", "tool_input": {"prompt": "x"}})
    assert scheduled_prompts.fired("s", "x")


def run_hook(env: dict[str, str], payload: dict) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "hooks"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.feature("operator-presence-automated-prompts")
def test_smoke_the_hook_entry_point_marks_a_cron_prompt_automated_and_a_typed_one_present(tmp_path: Path) -> None:
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("AGENTIHOOKS_")},
        **MASTER,
        "AGENTIHOOKS_HOME": str(tmp_path / "home"),
        "LEDGER_DIR": str(tmp_path / "ledgers"),
        "AGENTIHOOKS_DISABLE_BYPASS_LOOKUP": "1",
        "BROADCAST_ENABLED": "false",
    }
    base = {"session_id": "smoke", "cwd": str(tmp_path), "transcript_path": ""}
    run_hook(env, {**base, "hook_event_name": "UserPromptSubmit", "prompt": "You are master@a1-1 of swarm demo"})
    run_hook(
        env,
        {
            **base,
            "hook_event_name": "PostToolUse",
            "tool_name": "CronCreate",
            "tool_input": {"cron": "17 * * * *", "prompt": NUDGE, "recurring": True},
            "tool_response": {"id": "e31ab9de"},
        },
    )
    cron = run_hook(env, {**base, "hook_event_name": "UserPromptSubmit", "prompt": NUDGE})
    assert AUTOMATED in cron
    assert PRESENT not in cron
    typed = run_hook(env, {**base, "hook_event_name": "UserPromptSubmit", "prompt": "Why is the hub slow?"})
    assert PRESENT in typed
    assert AUTOMATED not in typed
