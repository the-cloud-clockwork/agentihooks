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
    monkeypatch.setattr(herdr_host.subprocess, "run", lambda *a, **kw: type("Result", (), {"returncode": 1})())
    assert herdr_host.repo_label(Path("/missing/project.one_two")) == "project-one-two"


def test_resource_encoding_preserves_distinct_names():
    assert nats.queue_stream("lane.eng") != nats.queue_stream("lane-eng")
    assert nats.queue_stream("lane_eng") != nats.queue_stream("lane-eng")
    assert subjects.consumer_name("lane.eng") != subjects.consumer_name("lane-eng")
    assert subjects.consumer_name("lane_eng") != subjects.consumer_name("lane-eng")


def test_routing_subjects_keep_protocol_token_separators():
    assert readiness.queue_name("rig", "eng") == "swarm.rig.ready.eng"
    assert subjects.subject("rig", "task", "ready") == "swarm.rig.task.ready"
