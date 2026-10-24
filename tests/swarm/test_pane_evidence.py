import json
from types import SimpleNamespace

import pytest

from scripts.swarm import runtime
from scripts.swarm.store import AgentRecord


def agent(**changes):
    return AgentRecord(
        **(
            {
                "name": "engineer@abc",
                "task": "t1",
                "lane": "eng",
                "state": "working",
                "started_at": 1,
                "pane_id": "w4:p7K",
            }
            | changes
        )
    )


class Panes:
    def __init__(self, text="Ready", state="working"):
        self.text, self.state, self.calls = text, state, []

    def __call__(self, args):
        self.calls.append(args)
        if args[:2] == ["agent", "get"]:
            return {
                "agent": {
                    "name": "engineer@abc",
                    "pane_id": "w4:p7K",
                    "terminal_id": "term_one",
                    "agent_status": self.state,
                }
            }
        if args[:2] == ["pane", "read"]:
            if isinstance(self.text, Exception):
                raise self.text
            return {"text": self.text}
        if args[:2] == ["pane", "process-info"]:
            return {
                "process_info": {
                    "shell_pid": 10,
                    "foreground_process_group_id": 20,
                    "foreground_processes": [
                        {
                            "pid": 21,
                            "name": "codex",
                            "cwd": "/work",
                            "argv": ["private"],
                            "cmdline": "private",
                            "env": "private",
                        }
                    ],
                }
            }
        raise AssertionError(args)


def collect(tmp_path, panes, at=100, agents=None):
    from scripts.swarm import pane_evidence

    def roster(slug):
        assert slug == "swarm"
        return agents if agents is not None else [agent()]

    rt = runtime.HerdrRuntime(home=tmp_path, herdr=panes)
    store = SimpleNamespace(agents=roster)
    actions = pane_evidence.sweep("swarm", store, rt, at)
    files = sorted((tmp_path / "swarm" / "pane-evidence").glob("*.json"))
    return actions, [json.loads(p.read_text()) for p in files]


def test_tick_capture_preserves_screen_and_safe_process_identity(tmp_path):
    panes = Panes("\x1b[31mReady\x1b[0m")
    actions, rows = collect(tmp_path, panes)
    assert actions == []
    assert len(rows) == 1
    row = rows[0]
    assert row["at"] == 100
    assert row["agent"] == "engineer@abc"
    assert row["task"] == "t1"
    assert row["pane"] == {"pane_id": "w4:p7K", "terminal_id": "term_one", "agent_status": "working"}
    assert row["screen_ansi"] == "\x1b[31mReady\x1b[0m"
    assert row["screen_text"] == "Ready"
    assert row["reason"] == ""
    assert row["process"] == {
        "shell_pid": 10,
        "foreground_process_group_id": 20,
        "foreground_processes": [{"pid": 21, "name": "codex", "cwd": "/work"}],
    }
    assert panes.calls == [
        ["agent", "get", "w4:p7K"],
        ["pane", "read", "w4:p7K", "--source", "visible", "--format", "ansi"],
        ["pane", "process-info", "--pane", "w4:p7K"],
    ]


@pytest.mark.parametrize(
    ("text", "state", "reason"),
    [
        (" \n", "working", "blank screen"),
        (None, "working", "missing screen"),
        ("bad\ufffd", "working", "invalid screen text"),
        ("bad\x00", "working", "invalid screen text"),
        ("\x1b[31m \x1b[0m", "working", "blank screen"),
        ("Ready", "unknown", "unknown pane state"),
        (TimeoutError("private"), "working", "screen read failed: TimeoutError"),
    ],
)
def test_unreadable_incident_keeps_last_healthy_screen_and_process(tmp_path, text, state, reason):
    panes = Panes()
    collect(tmp_path, panes, 100)
    panes.text, panes.state = text, state
    actions, rows = collect(tmp_path, panes, 200)
    assert actions == ["pane evidence saved for engineer@abc: " + reason]
    assert any(row["at"] == 100 and row["screen_text"] == "Ready" for row in rows)
    assert any(row["at"] == 200 and row["reason"] == reason and row["process"]["shell_pid"] == 10 for row in rows)
    if text is None or isinstance(text, Exception):
        assert all(row["screen_ansi"] == row["screen_text"] == "" for row in rows if row["at"] == 200)


def test_rolling_capture_stays_bounded_and_incident_survives_recovery(tmp_path):
    panes = Panes(" ")
    collect(tmp_path, panes)
    panes.text = "Recovered"
    for at in range(101, 108):
        _, rows = collect(tmp_path, panes, at)
    assert len(rows) == 3
    assert sorted(row["at"] for row in rows) == [100, 106, 107]


@pytest.mark.parametrize(
    "changes", [{"runtime_backend": "kubernetes"}, {"runtime_target": {"host_id": "remote"}}, {"pane_id": ""}]
)
def test_foreign_or_unbound_panes_are_not_read(tmp_path, changes):
    panes = Panes()
    assert collect(tmp_path, panes, agents=[agent(**changes)]) == ([], [])
    assert panes.calls == []


def test_runtime_without_terminal_capability_has_no_capture(tmp_path):
    from scripts.swarm import pane_evidence

    assert pane_evidence.sweep("swarm", None, object(), 100) == []


def test_ownership_loss_does_not_read_another_conversation(tmp_path):
    def other(args):
        assert args == ["agent", "get", "w4:p7K"]
        return {"agent": {"name": "someone_else"}}

    assert collect(tmp_path, other) == ([], [])


def test_missing_pane_has_no_capture(tmp_path):
    def gone(args):
        raise RuntimeError("gone")

    actions, rows = collect(tmp_path, gone)
    assert actions == ["pane evidence saved for engineer@abc: pane lookup failed: RuntimeError"]
    expected = {
        "at": 100,
        "agent": "engineer@abc",
        "task": "t1",
        "pane": {"pane_id": "w4:p7K"},
        "screen_ansi": "",
        "screen_text": "",
        "process": {},
        "reason": "pane lookup failed: RuntimeError",
        "process_error": "",
    }
    assert rows == [{**expected, "previous": None}, expected]


def test_process_failure_still_saves_the_screen(tmp_path):
    panes = Panes()

    def broken(args):
        if args[:2] == ["pane", "process-info"]:
            raise TimeoutError("private")
        return panes(args)

    _, rows = collect(tmp_path, broken)
    assert rows[0]["screen_text"] == "Ready"
    assert rows[0]["process_error"] == "TimeoutError"
    assert rows[0]["process"] == {"shell_pid": None, "foreground_process_group_id": None, "foreground_processes": []}


def test_capture_failure_is_visible_and_does_not_stop_the_next_pane(tmp_path, monkeypatch):
    from scripts.swarm import pane_evidence

    calls = []

    def fail(directory, rt, who, at):
        calls.append(who.name)
        if who.task == "t1":
            raise OSError("private")
        return ["second captured"]

    monkeypatch.setattr(pane_evidence, "capture", fail)
    actions, _ = collect(tmp_path, Panes(), agents=[agent(), agent(name="other", task="t2")])
    assert calls == ["engineer@abc", "other"]
    assert actions == ["pane evidence failed for engineer@abc: OSError", "second captured"]


def test_incident_freezes_the_last_healthy_screen_until_recovery(tmp_path):
    panes = Panes("Healthy")
    collect(tmp_path, panes, 100)
    panes.text = " "
    collect(tmp_path, panes, 200)
    actions, rows = collect(tmp_path, panes, 300)
    assert actions == []
    incident = next(row for row in rows if "previous" in row)
    assert incident["at"] == 200
    assert incident["previous"]["at"] == 100
    assert incident["previous"]["screen_text"] == "Healthy"
    panes.text = "Recovered"
    collect(tmp_path, panes, 400)
    panes.text = None
    _, rows = collect(tmp_path, panes, 500)
    incident = next(row for row in rows if "previous" in row)
    assert incident["at"] == 500
    assert incident["previous"]["screen_text"] == "Recovered"


def test_record_count_screen_size_redaction_and_permissions_are_bounded(tmp_path, monkeypatch):
    from scripts.swarm import pane_evidence

    monkeypatch.setattr(pane_evidence, "RECORD_LIMIT", 3)
    monkeypatch.setattr(pane_evidence, "SCREEN_LIMIT", 8)
    seen = []
    monkeypatch.setattr(pane_evidence, "redact", lambda text, *, mode: seen.append((text, mode)) or "redacted output")
    panes = Panes("private")
    for name in ("one", "two", "three", "four"):
        row_agent = agent(name=name)

        def named(args):
            answer = panes(args)
            if args[:2] == ["agent", "get"]:
                answer["agent"]["name"] = name
            return answer

        collect(tmp_path, named, agents=[row_agent])
    files = list((tmp_path / "swarm" / "pane-evidence").glob("*.json"))
    assert len(files) == 3
    assert seen == [("private", "strict")] * 4
    assert all(json.loads(path.read_text())["screen_ansi"] == "redacted" for path in files)
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in files)
    assert files[0].parent.stat().st_mode & 0o777 == 0o700


def test_ansi_cannot_hide_a_secret_from_redaction(tmp_path, monkeypatch):
    from scripts.swarm import pane_evidence

    monkeypatch.setattr(pane_evidence, "redact", lambda text, *, mode: text.replace("private", "[redacted]"))
    _, rows = collect(tmp_path, Panes("pri\x1b[31mvate"))
    assert rows[0]["screen_ansi"] == "[redacted]"
    assert rows[0]["screen_text"] == "[redacted]"


def test_changing_failure_reasons_keeps_the_first_incident_and_healthy_predecessor(tmp_path):
    panes = Panes("Healthy")
    collect(tmp_path, panes, 100)
    for at, text in [(200, " "), (300, TimeoutError()), (400, "bad\ufffd")]:
        panes.text = text
        _, rows = collect(tmp_path, panes, at)
    incident = next(row for row in rows if "previous" in row)
    assert incident["at"] == 200
    assert incident["previous"]["screen_text"] == "Healthy"


@pytest.mark.parametrize(
    ("args", "output", "expected"),
    [
        (["pane", "read", "w1:p1"], "screen", {"text": "screen"}),
        (["agent", "get", "w1:p1"], '{"result":{"agent":{"pane_id":"w1:p1"}}}', {"agent": {"pane_id": "w1:p1"}}),
    ],
)
def test_live_diagnostic_transport_has_a_short_timeout(tmp_path, monkeypatch, args, output, expected):
    from scripts import herdr_host
    from scripts.swarm import pane_evidence

    calls = []
    monkeypatch.setattr(herdr_host, "binary", lambda: "herdr")

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout=output)

    monkeypatch.setattr(pane_evidence, "subprocess", SimpleNamespace(run=run))
    rt = runtime.HerdrRuntime(home=tmp_path)
    assert pane_evidence._call(rt, args) == expected
    assert calls == [(["herdr", *args], {"capture_output": True, "text": True, "timeout": 2, "check": True})]


def test_sweep_budget_defers_panes_and_next_tick_starts_with_the_oldest_capture(tmp_path, monkeypatch):
    from scripts.swarm import pane_evidence

    agents = [agent(), agent(name="second")]
    rt = runtime.HerdrRuntime(home=tmp_path, herdr=Panes())
    store = SimpleNamespace(agents=lambda slug: agents)
    seen = []

    def capture(directory, rt, who, at):
        seen.append(who.name)
        return pane_evidence._save(directory, who, {"reason": "", "at": at})

    monkeypatch.setattr(pane_evidence, "capture", capture)
    for at in (100, 200):
        readings = iter([0, 0, 8])
        monkeypatch.setattr(pane_evidence, "time", SimpleNamespace(monotonic=lambda: next(readings)))
        assert pane_evidence.sweep("swarm", store, rt, at) == ["pane evidence capture budget exhausted"]
    assert seen == ["engineer@abc", "second"]


def test_empty_pane_reply_is_not_an_owned_pane(tmp_path):
    assert collect(tmp_path, lambda args: {}) == ([], [])


def test_filesystem_failure_during_budget_sorting_does_not_escape_the_sweep(tmp_path, monkeypatch):
    from scripts.swarm import pane_evidence

    def unreadable(*args):
        raise PermissionError("private")

    monkeypatch.setattr(pane_evidence, "_last_capture", unreadable)
    actions, rows = collect(tmp_path, Panes())
    assert actions == ["pane evidence sweep failed: PermissionError"]
    assert rows == []


def test_unwrapped_pane_reply_and_missing_state_are_captured(tmp_path):
    panes = Panes()

    def unwrapped(args):
        reply = panes(args)
        if args[:2] == ["agent", "get"]:
            reply = reply["agent"]
            del reply["agent_status"]
        return reply

    actions, rows = collect(tmp_path, unwrapped)
    assert actions == ["pane evidence saved for engineer@abc: unknown pane state"]
    assert rows[0]["pane"] == {"pane_id": "w4:p7K", "terminal_id": "term_one"}
    assert rows[0]["screen_text"] == "Ready"
    assert rows[0]["reason"] == "unknown pane state"


def test_ansi_redaction_uses_strict_mode_even_when_plain_text_has_no_secret(tmp_path, monkeypatch):
    from scripts.swarm import pane_evidence

    calls = []

    def redact(text, *, mode=None):
        calls.append((text, mode))
        return text

    monkeypatch.setattr(pane_evidence, "redact", redact)
    collect(tmp_path, Panes("\x1b[31mReady\x1b[0m"))
    assert calls == [("Ready", "strict"), ("\x1b[31mReady\x1b[0m", "strict")]


def test_oldest_record_is_pruned_instead_of_lexical_filename_order(tmp_path, monkeypatch):
    import os

    from scripts.swarm import pane_evidence

    monkeypatch.setattr(pane_evidence, "RECORD_LIMIT", 3)
    directory = tmp_path / "swarm" / "pane-evidence"
    directory.mkdir(parents=True)
    for name, stamp in [("a.json", 300), ("m.json", 200), ("z.json", 100)]:
        path = directory / name
        path.write_text("{}")
        os.utime(path, ns=(stamp, stamp))
    collect(tmp_path, Panes())
    assert (directory / "a.json").exists()
    assert (directory / "m.json").exists()
    assert not (directory / "z.json").exists()
    assert len(list(directory.glob("*.json"))) == 3


def test_record_suffixes_keep_latest_previous_and_incident_discoverable(tmp_path):
    panes = Panes()
    collect(tmp_path, panes)
    panes.text = None
    collect(tmp_path, panes, 200)
    names = [path.name.rsplit(".", 2)[1] for path in (tmp_path / "swarm" / "pane-evidence").glob("*.json")]
    assert sorted(names) == ["incident", "latest", "previous"]


def test_atomic_write_uses_the_target_filesystem_and_keeps_renamed_file(tmp_path, monkeypatch):
    from scripts.swarm import pane_evidence

    real = pane_evidence.tempfile.NamedTemporaryFile
    seen = []

    def temporary(**kwargs):
        seen.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(pane_evidence, "tempfile", SimpleNamespace(NamedTemporaryFile=temporary))
    directory = tmp_path / "records"
    directory.mkdir()
    path = directory / "snapshot.json"
    pane_evidence._write(path, {"screen": "Ready"})
    assert seen == [{"mode": "w", "dir": directory, "delete": False}]
    assert json.loads(path.read_text()) == {"screen": "Ready"}
    assert list(directory.iterdir()) == [path]
