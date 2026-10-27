import json
import os
import signal
import subprocess
import sys

import pytest

from scripts.headless import profile_run
from scripts.headless.profile_run import ProfileResult, ProfileRun

pytestmark = pytest.mark.unit

SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string"}}, "required": ["verdict"]}

FAKE = """#!{python}
import json
import os
import sys
from pathlib import Path

plan = json.loads(Path(os.environ["FAKE_PLAN"]).read_text())
Path(os.environ["FAKE_CALL_LOG"]).write_text(
    json.dumps({{"argv": sys.argv[1:], "env": {{k: os.environ.get(k) for k in plan.get("capture_env", [])}}}})
)
for line in plan.get("stdout_lines", []):
    print(line)
if plan.get("write_file"):
    Path(plan["write_file"]).write_text(plan.get("write_text", ""))
sys.exit(plan.get("exit_code", 0))
"""


@pytest.fixture
def fake_agentihooks(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    path = bin_dir / "agentihooks"
    path.write_text(FAKE.format(python=sys.executable))
    path.chmod(0o755)
    call_log = tmp_path / "call.json"
    monkeypatch.setenv("FAKE_CALL_LOG", str(call_log))

    def configure(plan: dict) -> None:
        plan_path = tmp_path / "plan.json"
        plan_path.write_text(json.dumps(plan))
        monkeypatch.setenv("FAKE_PLAN", str(plan_path))

    def calls() -> dict:
        return json.loads(call_log.read_text())

    return configure, calls


@pytest.fixture(autouse=True)
def clean_tiers(monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_TARGET", raising=False)
    for name in ("AGENTIHOOKS_TIER_HIGH_CLAUDE", "AGENTIHOOKS_TIER_LOW_CLAUDE"):
        monkeypatch.delenv(name, raising=False)
    for name in ("AGENTIHOOKS_TIER_HIGH_CODEX", "AGENTIHOOKS_TIER_LOW_CODEX"):
        monkeypatch.delenv(name, raising=False)


def make_run(tmp_path, **overrides) -> ProfileRun:
    fields = {
        "profile": "package:engineer",
        "harness": "claude",
        "input": "hello",
        "schema": None,
        "tier": "low",
        "effort": "low",
        "timeout": 5.0,
        "run_dir": tmp_path / "run",
    }
    fields.update(overrides)
    return ProfileRun(**fields)


def result_event(verdict: str, session: str = "sess-1") -> str:
    event = {"type": "result", "result": "", "structured_output": {"verdict": verdict}, "session_id": session}
    return json.dumps(event)


# claude_args / codex_args


def test_claude_args_keeps_the_base_flags_before_the_schema_flag(tmp_path):
    run = make_run(tmp_path, harness="claude", schema=SCHEMA)
    assert profile_run.claude_args(run) == [
        "-p",
        "hello",
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
        "--permission-mode",
        "bypassPermissions",
        "--json-schema",
        json.dumps(SCHEMA),
    ]


def test_codex_args_keeps_the_base_flags_before_the_schema_flag(tmp_path):
    run = make_run(tmp_path, harness="codex", schema=SCHEMA)
    assert profile_run.codex_args(run) == [
        "exec",
        "--json",
        "--skip-git-repo-check",
        "-o",
        str(run.run_dir / "last-message.txt"),
        "--output-schema",
        str(run.run_dir / "output-schema.json"),
        "--",
        "hello",
    ]


# claude_result


def test_claude_result_skips_unparseable_and_non_matching_lines_to_find_the_last_result():
    text = "\n".join(
        [
            "not json",
            result_event("pass", session="s1"),
            json.dumps({"type": "system"}),
            "also not json",
        ]
    )
    assert profile_run.claude_result(text) == ({"verdict": "pass"}, "s1")


def test_claude_result_falls_back_to_the_result_text_when_structured_output_is_absent():
    text = json.dumps({"type": "result", "result": "plain text", "session_id": "s2"})
    assert profile_run.claude_result(text) == ("plain text", "s2")


def test_claude_result_defaults_to_empty_text_when_the_result_key_is_absent():
    text = json.dumps({"type": "result", "session_id": "s3"})
    assert profile_run.claude_result(text) == ("", "s3")


def test_claude_result_without_any_result_event_is_empty():
    text = "\n".join(["not json", json.dumps({"type": "system"})])
    assert profile_run.claude_result(text) == ("", None)


# codex_result


def test_codex_result_initial_session_is_none_without_a_thread_started_event(tmp_path):
    run = make_run(tmp_path)
    text = "\n".join(["not json", json.dumps({"type": "system"})])
    assert profile_run.codex_result(run, text) == ("", None)


def test_codex_result_requires_the_type_to_be_thread_started_not_merely_a_dict(tmp_path):
    run = make_run(tmp_path)
    text = "\n".join([json.dumps({"type": "system"}), json.dumps({"type": "thread.started", "thread_id": "th-x"})])
    _, session = profile_run.codex_result(run, text)
    assert session == "th-x"


def test_codex_result_skips_unparseable_lines_to_find_thread_started(tmp_path):
    run = make_run(tmp_path)
    text = "\n".join(["not json", json.dumps({"type": "thread.started", "thread_id": "th-y"})])
    _, session = profile_run.codex_result(run, text)
    assert session == "th-y"


def test_codex_result_reads_the_message_file_when_one_was_written(tmp_path):
    run = make_run(tmp_path)
    run.run_dir.mkdir()
    (run.run_dir / "last-message.txt").write_text("hi there")
    output, _ = profile_run.codex_result(run, "")
    assert output == "hi there"


# execute


def test_execute_spawns_the_child_with_closed_stdin_its_own_session_and_the_given_timeout(monkeypatch, tmp_path):
    captured = {}

    class FakeProcess:
        pid = 1

        def wait(self, timeout=None):
            captured["wait_timeout"] = timeout
            return 0

    def fake_popen(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(profile_run.subprocess, "Popen", fake_popen)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    exit_code = profile_run.execute(["argv"], {"E": "1"}, run_dir, 7.5)
    assert exit_code == 0
    assert captured["args"] == (["argv"],)
    assert captured["kwargs"].get("stdin") == subprocess.DEVNULL
    assert captured["kwargs"].get("stdout") is not None and captured["kwargs"]["stdout"].name == str(
        run_dir / profile_run.TRANSCRIPT
    )
    assert captured["kwargs"].get("stderr") is not None and captured["kwargs"]["stderr"].name == str(
        run_dir / profile_run.STDERR
    )
    assert captured["kwargs"].get("start_new_session") is True
    assert captured["wait_timeout"] == 7.5


# run_profile wiring


def test_the_claude_argv_without_a_schema(fake_agentihooks, tmp_path):
    configure, calls = fake_agentihooks
    configure({"stdout_lines": [result_event("pass")]})
    profile_run.run_profile(make_run(tmp_path, harness="claude"))
    assert calls()["argv"] == [
        "select-profile",
        "package:engineer",
        "--agent",
        "claude",
        "--model",
        "sonnet",
        "--effort",
        "low",
        "--",
        "-p",
        "hello",
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
        "--permission-mode",
        "bypassPermissions",
    ]


def test_the_claude_argv_with_a_schema_writes_no_codex_schema_file(fake_agentihooks, tmp_path):
    configure, calls = fake_agentihooks
    configure({"stdout_lines": [result_event("pass")]})
    run = make_run(tmp_path, harness="claude", schema=SCHEMA)
    profile_run.run_profile(run)
    assert calls()["argv"][-2:] == ["--json-schema", json.dumps(SCHEMA)]
    assert not (run.run_dir / "output-schema.json").exists()


def test_the_codex_argv_without_a_schema(fake_agentihooks, tmp_path):
    configure, calls = fake_agentihooks
    run = make_run(tmp_path, harness="codex")
    configure({"write_file": str(run.run_dir / "last-message.txt"), "write_text": "hi"})
    profile_run.run_profile(run)
    assert calls()["argv"] == [
        "select-profile",
        "package:engineer",
        "--agent",
        "codex",
        "--model",
        "gpt-6-luna",
        "--effort",
        "low",
        "--",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "-o",
        str(run.run_dir / "last-message.txt"),
        "--",
        "hello",
    ]


def test_the_codex_argv_with_a_schema(fake_agentihooks, tmp_path):
    configure, calls = fake_agentihooks
    run = make_run(tmp_path, harness="codex", schema=SCHEMA)
    configure({"write_file": str(run.run_dir / "last-message.txt"), "write_text": json.dumps({"verdict": "pass"})})
    profile_run.run_profile(run)
    argv = calls()["argv"]
    assert argv[argv.index("--output-schema") + 1] == str(run.run_dir / "output-schema.json")
    assert json.loads((run.run_dir / "output-schema.json").read_text()) == SCHEMA


def test_the_environment_carries_the_worker_run_flags(fake_agentihooks, tmp_path):
    configure, calls = fake_agentihooks
    configure(
        {
            "stdout_lines": [result_event("pass")],
            "capture_env": [profile_run.WORKER_RUN, profile_run.WORKER_RUN_ID],
        }
    )
    run = make_run(tmp_path)
    profile_run.run_profile(run)
    assert calls()["env"] == {profile_run.WORKER_RUN: "1", profile_run.WORKER_RUN_ID: run.run_dir.name}


def test_the_transcript_is_written_to_the_run_dir(fake_agentihooks, tmp_path):
    configure, _ = fake_agentihooks
    configure({"stdout_lines": [result_event("pass")]})
    run = make_run(tmp_path)
    result = profile_run.run_profile(run)
    assert result.transcript == run.run_dir / "transcript.jsonl"
    assert result.transcript.read_text().splitlines() == [result_event("pass")]


def test_run_profile_tolerates_a_pre_existing_run_dir(fake_agentihooks, tmp_path):
    configure, _ = fake_agentihooks
    configure({"stdout_lines": [result_event("pass")]})
    run = make_run(tmp_path)
    run.run_dir.mkdir()
    result = profile_run.run_profile(run)
    assert result.status == "ok"


def test_run_profile_creates_missing_parent_directories(fake_agentihooks, tmp_path):
    configure, _ = fake_agentihooks
    configure({"stdout_lines": [result_event("pass")]})
    run = make_run(tmp_path, run_dir=tmp_path / "nested" / "deeper" / "run")
    result = profile_run.run_profile(run)
    assert result.status == "ok"


class _FakeTimedOutProcess:
    pid = 4321

    def __init__(self):
        self.calls = 0
        self.wait_timeouts = []

    def wait(self, timeout=None):
        self.calls += 1
        self.wait_timeouts.append(timeout)
        if self.calls == 1:
            raise subprocess.TimeoutExpired(cmd="agentihooks", timeout=timeout)
        return None


def test_a_timeout_kills_the_whole_process_group(monkeypatch, tmp_path):
    killed = []
    fake_process = _FakeTimedOutProcess()
    monkeypatch.setattr(profile_run.subprocess, "Popen", lambda *a, **k: fake_process)
    monkeypatch.setattr(profile_run.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(profile_run.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))
    run = make_run(tmp_path, timeout=0.01)
    result = profile_run.run_profile(run)
    assert result == ProfileResult("timeout", None, run.run_dir / "transcript.jsonl", None, None)
    assert killed == [(4321, signal.SIGKILL)]
    assert fake_process.calls == 2
    assert fake_process.wait_timeouts[0] == 0.01


def test_a_schema_valid_reply_gives_ok(fake_agentihooks, tmp_path):
    configure, _ = fake_agentihooks
    configure({"stdout_lines": [result_event("pass", session="sess-1")]})
    run = make_run(tmp_path, schema=SCHEMA)
    result = profile_run.run_profile(run)
    assert result == ProfileResult("ok", {"verdict": "pass"}, run.run_dir / "transcript.jsonl", "sess-1", 0)


def test_a_schema_none_result_carries_every_field(fake_agentihooks, tmp_path):
    configure, _ = fake_agentihooks
    configure({"stdout_lines": [result_event("pass", session="sess-9")]})
    run = make_run(tmp_path, schema=None)
    result = profile_run.run_profile(run)
    assert result == ProfileResult("ok", {"verdict": "pass"}, run.run_dir / "transcript.jsonl", "sess-9", 0)


def test_a_schema_invalid_reply_gives_invalid(fake_agentihooks, tmp_path):
    configure, _ = fake_agentihooks
    bad = json.dumps({"type": "result", "result": "", "structured_output": {"other": 1}, "session_id": "sess-5"})
    configure({"stdout_lines": [bad]})
    run = make_run(tmp_path, schema=SCHEMA)
    result = profile_run.run_profile(run)
    assert result == ProfileResult("invalid", {"other": 1}, run.run_dir / "transcript.jsonl", "sess-5", 0)


def test_a_nonzero_exit_gives_failed(fake_agentihooks, tmp_path):
    configure, _ = fake_agentihooks
    configure({"stdout_lines": [result_event("pass", session="sess-7")], "exit_code": 3})
    run = make_run(tmp_path)
    result = profile_run.run_profile(run)
    assert result == ProfileResult("failed", {"verdict": "pass"}, run.run_dir / "transcript.jsonl", "sess-7", 3)


def test_a_codex_run_resolves_session_and_output_from_the_stream_and_file(fake_agentihooks, tmp_path):
    configure, _ = fake_agentihooks
    run = make_run(tmp_path, harness="codex", schema=SCHEMA)
    configure(
        {
            "stdout_lines": [json.dumps({"type": "thread.started", "thread_id": "th-1"})],
            "write_file": str(run.run_dir / "last-message.txt"),
            "write_text": json.dumps({"verdict": "pass"}),
        }
    )
    result = profile_run.run_profile(run)
    assert result.status == "ok"
    assert result.output == {"verdict": "pass"}
    assert result.session == "th-1"


def test_auto_resolves_through_the_callers_harness(fake_agentihooks, tmp_path, monkeypatch):
    configure, calls = fake_agentihooks
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
    run = make_run(tmp_path, harness="auto")
    configure({"write_file": str(run.run_dir / "last-message.txt"), "write_text": "hi"})
    profile_run.run_profile(run)
    argv = calls()["argv"]
    assert argv[argv.index("--agent") + 1] == "codex"
