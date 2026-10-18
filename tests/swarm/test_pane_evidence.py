import json
from types import SimpleNamespace

import pytest
from scripts.swarm.store import AgentRecord

from scripts.swarm import runtime


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

    rt = runtime.HerdrRuntime(home=tmp_path, herdr=panes)
    store = SimpleNamespace(agents=lambda slug: agents if agents is not None else [agent()])
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


def test_rolling_capture_stays_bounded_and_incident_survives_recovery(tmp_path):
    panes = Panes(" ")
    collect(tmp_path, panes)
    panes.text = "Recovered"
    for at in range(101, 108):
        _, rows = collect(tmp_path, panes, at)
    assert len(rows) == 3
    assert sorted(row["at"] for row in rows) == [100, 106, 107]
