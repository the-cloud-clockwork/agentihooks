import json
from types import SimpleNamespace

import pytest

from scripts.hive import herdr_stats

pytestmark = pytest.mark.unit

MACHINES = [
    {"label": "hive-1", "enabled": True},
    {"label": "hive-2", "enabled": True},
    {"label": "off", "enabled": False},
]


@pytest.fixture
def redis():
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


def listing(*agents):
    return {"result": {"panes": [{"agent": agent} if agent else {"pane_id": "shell"} for agent in agents]}}


class Herdr:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, argv, **kwargs):
        self.calls.append(tuple(argv[1:]))
        answer = self.answers.get(tuple(argv[1:]))
        if isinstance(answer, Exception):
            raise answer
        return SimpleNamespace(
            returncode=1 if answer is None else 0, stdout="" if answer is None else json.dumps(answer)
        )


@pytest.fixture
def herdr(monkeypatch):
    def install(answers):
        fake = Herdr(answers)
        monkeypatch.setattr(herdr_stats.subprocess, "run", fake)
        return fake

    return install


def test_measure_counts_agent_panes_on_the_client_and_each_enabled_saved_machine(herdr):
    fake = herdr(
        {
            ("machine", "list", "--json"): MACHINES,
            ("pane", "list"): listing("claude", "claude", "codex", None),
            ("--machine", "hive-1", "pane", "list"): listing("claude", "codex", "codex"),
            ("--machine", "hive-2", "pane", "list"): listing("claude", "other"),
        }
    )
    assert herdr_stats.measure("/bin/herdr") == {
        "machines": {
            "master": {"claude": 2, "codex": 1},
            "hive-1": {"claude": 1, "codex": 2},
            "hive-2": {"claude": 1, "codex": 0},
        },
        "silent": [],
    }
    assert ("--machine", "off", "pane", "list") not in fake.calls


def test_a_machine_that_does_not_answer_is_named_silent_never_counted_as_zero(herdr):
    herdr(
        {
            ("machine", "list", "--json"): MACHINES,
            ("pane", "list"): listing("claude"),
            ("--machine", "hive-1", "pane", "list"): herdr_stats.subprocess.TimeoutExpired("herdr", 8),
            ("--machine", "hive-2", "pane", "list"): None,
        }
    )
    measured = herdr_stats.measure("/bin/herdr")
    assert measured == {"machines": {"master": {"claude": 1, "codex": 0}}, "silent": ["hive-1", "hive-2"]}


def test_a_listing_without_panes_counts_no_agents():
    assert herdr_stats.agent_panes({"result": {}}) == {"claude": 0, "codex": 0}


def test_a_client_without_saved_machines_counts_itself(herdr):
    herdr({("machine", "list", "--json"): None, ("pane", "list"): listing("codex")})
    assert herdr_stats.measure("/bin/herdr") == {"machines": {"master": {"claude": 0, "codex": 1}}, "silent": []}


def test_publish_stores_the_reading_until_it_is_thirty_seconds_old(redis, herdr):
    herdr({("machine", "list", "--json"): [], ("pane", "list"): listing("claude")})
    herdr_stats.publish(redis, 5_000, "/bin/herdr")
    assert herdr_stats.read(redis, 35_000) == {
        "machines": {"master": {"claude": 1, "codex": 0}},
        "silent": [],
        "measured_at": 5_000,
    }
    assert herdr_stats.read(redis, 35_001) is None
    assert 29_000 < redis.pttl(herdr_stats.KEY) <= 30_000


def test_publish_writes_nothing_where_herdr_is_not_installed(redis, monkeypatch):
    monkeypatch.setattr(herdr_stats.shutil, "which", lambda name: None)
    herdr_stats.publish(redis, 5_000)
    assert redis.get(herdr_stats.KEY) is None


def test_publish_finds_the_herdr_binary_by_its_name(redis, monkeypatch):
    binaries = []

    def run(argv, **kwargs):
        binaries.append(argv[0])
        answer = [] if argv[1:] == ["machine", "list", "--json"] else listing("codex")
        return SimpleNamespace(returncode=0, stdout=json.dumps(answer))

    monkeypatch.setattr(herdr_stats.subprocess, "run", run)
    monkeypatch.setattr(herdr_stats.shutil, "which", lambda name: "/opt/herdr" if name == "herdr" else None)
    herdr_stats.publish(redis, 5_000)
    assert herdr_stats.read(redis, 5_000)["machines"] == {"master": {"claude": 0, "codex": 1}}
    assert binaries == ["/opt/herdr", "/opt/herdr"]


def test_herdr_runs_with_its_output_captured_as_text_inside_the_timeout(monkeypatch):
    seen = {}

    def run(argv, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="[]")

    monkeypatch.setattr(herdr_stats.subprocess, "run", run)
    assert herdr_stats.herdr("/bin/herdr", "machine", "list", "--json") == []
    assert seen == {"capture_output": True, "text": True, "timeout": herdr_stats.TIMEOUT_S, "check": False}
