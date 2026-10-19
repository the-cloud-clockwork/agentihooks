import subprocess

import pytest

from scripts.swarm import release_wait
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm.tick import _claimable


def task(task_id, **fields):
    return {
        "id": task_id,
        "title": f"title {task_id}",
        "description": f"spec {task_id}",
        "state": "open",
        "claimed_by": "",
        "lane": "eng",
        "kind": "code",
        "territory": [f"area/{task_id}"],
        **fields,
    }


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def origin(tmp_path):
    bare, work = tmp_path / "origin.git", tmp_path / "work"
    git("init", "--bare", str(bare), cwd=tmp_path)
    git("init", str(work), cwd=tmp_path)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-m", "base", cwd=work)
    git("remote", "add", "origin", str(bare), cwd=work)
    git("tag", "v2.17.1", cwd=work)
    git("push", "origin", "HEAD:refs/heads/dev", "--tags", cwd=work)
    return work


@pytest.fixture
def store(origin):
    import fakeredis

    saved = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.create(SwarmConfig("sw", str(origin), max_eng=1, max_ci=0))
    return saved


def claimable(store, rows):
    return [t["id"] for t in _claimable("sw", store, rows, {"phases": [], "tasks": list(rows.values())}, "eng")]


def test_a_task_waiting_on_a_release_is_unclaimable_until_a_newer_tag_exists(store, origin):
    rows = {t["id"]: t for t in [task("proof", after_release="v2.17.1"), task("plain")]}
    assert claimable(store, rows) == ["plain"]
    git("tag", "v2.17.2", cwd=origin)
    git("push", "origin", "v2.17.2", cwd=origin)
    assert claimable(store, rows) == ["proof", "plain"]


def test_tags_reads_the_origin_tags(origin):
    assert release_wait.tags(str(origin)) == ["v2.17.1"]


def test_tags_asks_origin_for_tag_refs_with_a_bounded_wait(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "abc 1\trefs/tags/v1.0.0\ndef 2\trefs/tags/v1.0.1\n", "")

    monkeypatch.setattr(release_wait.subprocess, "run", run)
    assert release_wait.tags("/repo") == ["v1.0.0", "v1.0.1"]
    assert calls == [
        (
            ["git", "-C", "/repo", "ls-remote", "--tags", "--refs", "origin"],
            {"capture_output": True, "text": True, "timeout": 30},
        )
    ]


def test_tags_is_empty_when_the_lookup_times_out(monkeypatch):
    def run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(release_wait.subprocess, "run", run)
    assert release_wait.tags("/repo") == []


@pytest.mark.parametrize(("error", "why"), [(OSError(), "OSError"), (OSError("no git"), "no git")])
def test_lookup_names_why_it_failed(monkeypatch, error, why):
    def run(args, **kwargs):
        raise error

    monkeypatch.setattr(release_wait.subprocess, "run", run)
    assert release_wait.lookup("/repo") == ([], why)


def test_tags_is_empty_when_the_repository_has_no_origin(tmp_path):
    assert release_wait.tags(str(tmp_path)) == []


@pytest.mark.parametrize(
    ("tags", "floor", "expected"),
    [
        (["v2.17.1"], "v2.17.1", False),
        (["v2.17.2"], "v2.17.1", True),
        (["v2.18.0"], "v2.17.9", True),
        (["v3.0.0"], "v2.99.99", True),
        (["v2.16.9", "v2.17.0"], "v2.17.1", False),
        (["v2.17.2-rc1", "latest"], "v2.17.1", False),
        (["2.17.2"], "v2.17.1", True),
        (["v2.17.2"], "not a tag", False),
        ([], "v2.17.1", False),
    ],
)
def test_newer_compares_stable_versions(tags, floor, expected):
    assert release_wait.newer(tags, floor) is expected


def test_version_parses_three_numbers():
    assert release_wait.version("v10.2.33") == (10, 2, 33)
    assert release_wait.version("v1.2") is None
    assert release_wait.version("v1.2.3\n") is None


def test_checker_reads_tags_once_and_passes_tasks_without_the_field(monkeypatch):
    calls = []
    monkeypatch.setattr(release_wait, "tags", lambda repo: calls.append(repo) or ["v1.0.1"])
    released = release_wait.checker("/repo")
    assert released({"id": "a"}) is True
    assert calls == []
    assert released({"id": "b", "after_release": "v1.0.0"}) is True
    assert released({"id": "c", "after_release": "v1.0.1"}) is False
    assert calls == ["/repo"]


class Ledger:
    def __init__(self):
        self.alerts = []

    def raise_alert(self, slug, source, text):
        self.alerts.append((slug, source, text))


def test_a_failed_lookup_raises_one_release_alert_while_a_task_waits_on_a_release(monkeypatch):
    monkeypatch.setattr(release_wait, "lookup", lambda repo: ([], "no git"))
    ledger, config = Ledger(), SwarmConfig("sw", "/repo", max_eng=1, max_ci=0)
    held = [task("proof", after_release="v2.17.1"), task("late", after_release="v2.17.1")]
    assert release_wait.alert_pass("sw", config, ledger, {"tasks": [task("plain"), *held]}) == [
        "raised the release alert: the tag lookup for /repo failed"
    ]
    text = "The release tag lookup for /repo failed: no git. Tasks proof, late stay held for a newer release until it works."
    assert ledger.alerts == [("sw", release_wait.SOURCE, text)]
    live = {"source": release_wait.SOURCE, "state": "open"}
    assert release_wait.alert_pass("sw", config, ledger, {"tasks": held, "alerts": [live]}) == []
    done = [task("plain"), {**held[0], "state": "done"}]
    assert release_wait.alert_pass("sw", config, ledger, {"tasks": done}) == []
    assert len(ledger.alerts) == 1
    closed = {**live, "state": "done"}
    other = {"source": "promotion", "state": "open"}
    assert release_wait.alert_pass("sw", config, ledger, {"tasks": held, "alerts": [closed, other]})
    assert len(ledger.alerts) == 2


def test_a_working_lookup_raises_no_release_alert(origin):
    ledger = Ledger()
    held = {"tasks": [task("proof", after_release="v2.17.1")]}
    assert release_wait.alert_pass("sw", SwarmConfig("sw", str(origin), max_eng=1, max_ci=0), ledger, held) == []
    assert ledger.alerts == []


def test_the_tick_hands_the_release_alert_pass_its_slug_config_ledger_and_state(store, monkeypatch):
    from scripts.swarm import tick
    from tests.swarm.test_tick import FakeLedger, FakeRuntime

    seen = []
    monkeypatch.setattr(release_wait, "alert_pass", lambda *args: seen.append(args) or ["alerted"])
    ledger = FakeLedger([{"id": "a"}])
    actions = tick.tick("sw", store, ledger, FakeRuntime(), now_ms=4_321)
    [(slug, config, got_ledger, doc)] = seen
    assert (slug, config.slug, got_ledger, [t["id"] for t in doc["tasks"]]) == ("sw", "sw", ledger, ["a"])
    assert "alerted" in actions
