import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts.swarm import release_wait
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm.tick import _claimable
from scripts.tick.adapters import github

ORIGIN = "github.com/owner/project"
ENDPOINT = ["api", "repos/owner/project/git/matching-refs/tags"]


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


def config(repo: str = "/gone", origin: str = ORIGIN) -> SwarmConfig:
    return SwarmConfig("sw", repo, max_eng=1, max_ci=0, repo_origin=origin)


class Api:
    """Answer each audited GitHub read with the next tag list, or raise the next error; the last answer repeats."""

    def __init__(self, *answers: list[str] | Exception) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, list[str], float]] = []

    def __call__(
        self, caller: str, args: list[str], *, timeout: float, **options: object
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((caller, args, timeout))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        refs = [{"ref": f"refs/tags/{name}", "object": {"sha": f"sha {name}"}} for name in answer]
        return subprocess.CompletedProcess(["gh", *args], 0, json.dumps(refs), "")


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Callable[..., Api]:
    def install(*answers: list[str] | Exception) -> Api:
        fake = Api(*answers)
        monkeypatch.setattr(github.github_reads, "rest", fake)
        return fake

    return install


@pytest.fixture
def store() -> RedisStore:
    import fakeredis

    saved = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.create(config())
    return saved


def claimable(store, rows):
    return [t["id"] for t in _claimable("sw", store, rows, {"phases": [], "tasks": list(rows.values())}, "eng")]


def test_a_task_waiting_on_a_release_is_unclaimable_until_a_newer_tag_exists(
    store: RedisStore, api: Callable[..., Api]
) -> None:
    rows = {t["id"]: t for t in [task("proof", after_release="v2.17.1"), task("plain")]}
    api(["v2.17.1"])
    assert claimable(store, rows) == ["plain"]
    api(["v2.17.1", "v2.17.2"])
    assert claimable(store, rows) == ["proof", "plain"]


def test_the_lookup_reads_tags_from_the_github_api_with_git_absent(
    api: Callable[..., Api], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    fake = api(["v1.0.0", "v1.0.2", "v1.0.1"])
    assert release_wait.lookup(config()) == (["v1.0.0", "v1.0.2", "v1.0.1"], "")
    [(caller, args, timeout)] = fake.calls
    assert (caller, args) == (github.CALLER, ENDPOINT)
    assert 14 < timeout <= release_wait.SECONDS
    released = release_wait.checker(config())
    assert released({"id": "a", "after_release": "v1.0.1"}) is True
    assert released({"id": "b", "after_release": "v1.0.2"}) is False


def test_the_repository_falls_back_to_the_origin_in_the_checkout_git_config(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text('[remote "origin"]\n\turl = git@github.com:Owner/Project.git\n')
    assert release_wait.repository(config(str(tmp_path), "")) == "owner/project"
    assert release_wait.repository(config(str(tmp_path))) == "owner/project"
    assert release_wait.repository(config(str(tmp_path), "gitlab.com/owner/project")) == ""


def test_a_swarm_without_a_github_repository_fails_the_lookup_without_a_read(
    api: Callable[..., Api], tmp_path: Path
) -> None:
    fake = api([])
    assert release_wait.lookup(config(str(tmp_path), "")) == ([], f"{tmp_path} names no GitHub repository")
    assert fake.calls == []


def test_one_failed_lookup_is_retried_before_it_counts(api: Callable[..., Api]) -> None:
    fake = api(subprocess.CalledProcessError(1, ["gh"]), ["v1.0.1"])
    assert release_wait.lookup(config()) == (["v1.0.1"], "")
    assert len(fake.calls) == 2


def test_a_lookup_that_fails_twice_names_the_second_failure(api: Callable[..., Api]) -> None:
    fake = api(OSError("first"), OSError("second"))
    assert release_wait.lookup(config()) == ([], "second")
    assert len(fake.calls) == 2


def test_a_working_lookup_reads_once(api: Callable[..., Api]) -> None:
    fake = api([])
    assert release_wait.lookup(config()) == ([], "")
    assert len(fake.calls) == 1


def test_an_attempt_reads_the_named_repository_from_the_current_folder_on_dev(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[tuple[str, Path, str]] = []

    class Recorder:
        def __init__(self, repository: str, root: Path, base: str) -> None:
            built.append((repository, root, base))

        def tags(self, *, deadline: object) -> tuple[github.Tag, ...]:
            return (github.Tag("v1.0.0", "sha"),)

    monkeypatch.setattr(github, "GitHubSourceControl", Recorder)
    assert release_wait._attempt("owner/project") == (["v1.0.0"], "")
    assert built == [("owner/project", Path.cwd(), "dev")]


def test_the_retry_reads_the_same_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    names: list[str] = []

    def attempt(name: str) -> tuple[list[str], str]:
        names.append(name)
        return ([], "down") if len(names) == 1 else (["v1.0.0"], "")

    monkeypatch.setattr(release_wait, "_attempt", attempt)
    assert release_wait.lookup(config()) == (["v1.0.0"], "")
    assert names == ["owner/project", "owner/project"]


def test_each_attempt_gets_its_own_bounded_wait(api: Callable[..., Api]) -> None:
    fake = api(TimeoutError("tool deadline expired"))
    assert release_wait.tags(config()) == []
    assert [14 < timeout <= release_wait.SECONDS for _, _, timeout in fake.calls] == [True, True]


@pytest.mark.parametrize(
    ("error", "why"),
    [(OSError(), "OSError"), (OSError("no gh"), "no gh"), (ValueError("bad json"), "bad json")],
)
def test_lookup_names_why_it_failed(api: Callable[..., Api], error: Exception, why: str) -> None:
    api(error)
    assert release_wait.lookup(config()) == ([], why)


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


def test_checker_reads_tags_once_and_passes_tasks_without_the_field(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    swarm = config()
    monkeypatch.setattr(release_wait, "tags", lambda found: calls.append(found) or ["v1.0.1"])
    released = release_wait.checker(swarm)
    assert released({"id": "a"}) is True
    assert calls == []
    assert released({"id": "b", "after_release": "v1.0.0"}) is True
    assert released({"id": "c", "after_release": "v1.0.1"}) is False
    assert calls == [swarm]


class Ledger:
    def __init__(self):
        self.alerts = []

    def raise_alert(self, slug, source, text):
        self.alerts.append((slug, source, text))


def test_a_failed_lookup_raises_one_release_alert_while_a_task_waits_on_a_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release_wait, "lookup", lambda found: ([], "no gh"))
    ledger, swarm = Ledger(), SwarmConfig("sw", "/repo", max_eng=1, max_ci=0)
    held = [task("proof", after_release="v2.17.1"), task("late", after_release="v2.17.1")]
    assert release_wait.alert_pass("sw", swarm, ledger, {"tasks": [task("plain"), *held]}) == [
        "raised the release alert: the tag lookup for /repo failed"
    ]
    text = "The release tag lookup for /repo failed: no gh. Tasks proof, late stay held for a newer release until it works."
    assert ledger.alerts == [("sw", release_wait.SOURCE, text)]
    live = {"source": release_wait.SOURCE, "state": "open"}
    assert release_wait.alert_pass("sw", swarm, ledger, {"tasks": held, "alerts": [live]}) == []
    done = [task("plain"), {**held[0], "state": "done"}]
    assert release_wait.alert_pass("sw", swarm, ledger, {"tasks": done}) == []
    assert len(ledger.alerts) == 1
    closed = {**live, "state": "done"}
    other = {"source": "promotion", "state": "open"}
    assert release_wait.alert_pass("sw", swarm, ledger, {"tasks": held, "alerts": [closed, other]})
    assert len(ledger.alerts) == 2


def test_a_working_lookup_raises_no_release_alert(
    api: Callable[..., Api], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    api(["v2.17.1"])
    ledger = Ledger()
    held = {"tasks": [task("proof", after_release="v2.17.1")]}
    assert release_wait.alert_pass("sw", config(str(tmp_path / "gone")), ledger, held) == []
    assert ledger.alerts == []


def test_the_tick_hands_the_release_alert_pass_its_slug_config_ledger_and_state(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm import tick
    from tests.swarm.test_tick import FakeLedger, FakeRuntime

    seen = []
    monkeypatch.setattr(release_wait, "alert_pass", lambda *args: seen.append(args) or ["alerted"])
    ledger = FakeLedger([{"id": "a"}])
    actions = tick.tick("sw", store, ledger, FakeRuntime(), now_ms=4_321)
    [(slug, swarm, got_ledger, doc)] = seen
    assert (slug, swarm.slug, got_ledger, [t["id"] for t in doc["tasks"]]) == ("sw", "sw", ledger, ["a"])
    assert "alerted" in actions
