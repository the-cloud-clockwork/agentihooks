import re
from pathlib import Path

import pytest

from scripts import herdr_host
from scripts.events import nats, subjects
from scripts.hive import machine
from scripts.swarm import naming, readiness

ENV = {"AGENTIHOOKS_AGENT_NAME": "engineer@123456-0001", "AGENTIHOOKS_SWARM_TASK": "task.one_two"}


@pytest.mark.parametrize(
    ("kind", "generate"),
    [
        ("stream", lambda: subjects.stream_name("rig", "QUEUE_eng")),
        ("queue", lambda: nats.queue_stream(readiness.queue_name("rig", "eng"))),
        ("consumer", lambda: subjects.consumer_name("hub.task_ready")),
        ("hive", lambda: machine.hive_name(12)),
        ("seat", lambda: naming.seat_label("eng", 2)),
        ("swarm", lambda: naming.swarm_name_suggestions("Build.this_system")[0]),
        ("ledger", lambda: naming.plan_slug("Build.this_system.md", "2026.10_25")),
        ("worktree", lambda: naming.worktree(ENV)),
        ("branch", lambda: naming.tmp_worktree(ENV)),
        ("pane", lambda: naming.pane_label("engineer@rig.one_two")),
        ("workspace", lambda: naming.space("/missing/project.one_two", "123456")),
        ("scratch", lambda: naming.scratch_folder("rig.one_two", "task.one_two")),
    ],
)
def test_generated_names_use_only_hyphen_separators(kind, generate):
    name = generate()
    assert "." not in name, (kind, name)
    assert re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", name), (kind, name)


def test_repository_labels_normalize_dots_and_underscores(monkeypatch):
    monkeypatch.setattr(
        herdr_host.subprocess, "run", lambda *a, **kw: type("Result", (), {"returncode": 1, "stdout": ""})()
    )
    assert herdr_host.repo_label(Path("/missing/project.one_two")) == "project-one-two"


def test_resource_encoding_preserves_distinct_names():
    assert nats.queue_stream("lane.eng") != nats.queue_stream("lane-eng")
    assert nats.queue_stream("lane_eng") != nats.queue_stream("lane-eng")
    assert subjects.consumer_name("lane.eng") != subjects.consumer_name("lane-eng")
    assert subjects.consumer_name("lane_eng") != subjects.consumer_name("lane-eng")


def test_routing_subjects_keep_protocol_token_separators():
    assert readiness.queue_name("rig", "eng") == "swarm.rig.ready.eng"
    assert subjects.subject("rig", "task", "ready") == "swarm.rig.task.ready"


@pytest.mark.parametrize(
    ("lane", "index", "expected"),
    [("master", 1, "master"), ("master", 2, "master-2"), ("eng", 1, "eng-1"), ("ci", 2, "ci-2"), ("plan", 1, "plan-1")],
)
def test_seat_labels_use_the_shared_builder(lane, index, expected):
    assert naming.seat_label(lane, index) == expected


@pytest.mark.parametrize(("lane", "index"), [("eng.one", 1), ("eng", 0), ("eng", -1), ("eng", True), ("eng", "1")])
def test_invalid_seats_are_refused(lane, index):
    with pytest.raises(naming.NamingError, match="^a seat needs a known lane and a positive integer$"):
        naming.seat_label(lane, index)


@pytest.mark.parametrize("ordinal", [0, -1, True, "1"])
def test_invalid_hive_ordinals_are_refused(ordinal):
    with pytest.raises(ValueError, match="^a hive needs a positive integer$"):
        machine.hive_name(ordinal)


@pytest.mark.parametrize("placement", ["tab", "workspace"])
def test_created_herdr_labels_use_the_shared_generator(monkeypatch, placement):
    seen = []
    monkeypatch.setattr(
        herdr_host, "target_workspace", lambda *args: (None if placement == "workspace" else "space", "repo.one_two")
    )
    monkeypatch.setattr(herdr_host, "_cli", lambda args, environ: seen.append(args) or {"root_pane": "pane"})
    monkeypatch.setattr(herdr_host, "_placed", lambda value: value)
    assert herdr_host.open_pane(Path("/missing"), "agent.one_two", {}, placement, "", {}) == "pane"
    command = seen[0]
    expected = "repo-one-two" if placement == "workspace" else "agent-one-two"
    assert command[command.index("--label") + 1] == expected


def test_master_seats_call_the_shared_generator(monkeypatch):
    from scripts.swarm_v2 import masters

    monkeypatch.setattr(masters, "seat_label", lambda lane, index: (lane, index))
    assert masters.seat(2) == ("master", 2)


@pytest.mark.parametrize("title", ["", "...", "___"])
def test_empty_pane_labels_have_a_stable_fallback(title):
    assert naming.pane_label(title) == "pane"


def test_dispatch_seats_use_the_same_label_rules():
    assert naming.seat_label("dispatch", 1) == "dispatch"
    assert naming.seat_label("dispatch", 2) == "dispatch-2"


def test_herdr_agent_names_trim_generated_separators():
    assert herdr_host.agent_name("__Name--") == "name"


@pytest.mark.parametrize("existing", [None, "repo.one_two", "repo-one-two"])
def test_workspace_lookup_reuses_current_and_legacy_labels(monkeypatch, existing):
    workspaces = {} if existing is None else {existing: "space"}
    calls = []
    launch_env = {"HERDR_SOCKET": "named-socket"}

    def find_workspace(label, environ):
        assert environ is launch_env
        return workspaces.get(label)

    monkeypatch.setattr(herdr_host, "find_workspace", find_workspace)
    monkeypatch.setattr(herdr_host, "_placed", lambda value: value)

    def cli(args, environ):
        assert environ is launch_env
        calls.append(args)
        if args[:2] == ["workspace", "create"]:
            workspaces[args[args.index("--label") + 1]] = "space"
        return {"root_pane": "pane"}

    monkeypatch.setattr(herdr_host, "_cli", cli)
    for _ in range(2):
        herdr_host.open_pane(Path("/missing"), "agent.one", {}, "tab", "repo.one_two", launch_env)
    assert sum(args[:2] == ["workspace", "create"] for args in calls) == (1 if existing is None else 0)
    assert calls[-1][:2] == ["tab", "create"]
    assert calls[-1][calls[-1].index("--workspace") + 1] == "space"
