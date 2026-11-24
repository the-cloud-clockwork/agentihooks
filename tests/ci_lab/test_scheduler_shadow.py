import logging
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from redis.exceptions import RedisError

from scripts.ci_lab import budget, scheduler, shadow
from scripts.ci_lab.digests import Toolchain
from scripts.ci_lab.objects import LocalObjects
from scripts.ci_lab.retry import Policy
from scripts.ci_lab.static_gates import StaticGate
from scripts.ci_lab.webhooks import HeadEvent, envelope, head_events
from scripts.dep_manifest import _git
from scripts.events.envelope import Envelope
from tests.ci_lab.test_scheduler import (
    ACTOR,
    BASE,
    HEAD_A,
    HEAD_B,
    REPO,
    TREE,
    Consumer,
    Delivery,
    Expander,
    FakeKube,
    Inbox,
    Lab,
    Run,
    _commit,
    _no_tests,
    _unknown,
    fake_redis,
    scrape,
)
from tests.ci_lab.test_scheduler import origin as origin

ROOT = Path(__file__).resolve().parents[2]
GITHUB = "https://github.com/the-cloud-clockwork/agentihooks"
OTHER = "https://github.com/acme/widgets"
LAB_APP = "agentihooks-lab"


def completed_check(
    name: str, conclusion: str, *, head: str = HEAD_A, app: str = "github-actions", repo: str = GITHUB
) -> Envelope:
    summary = f"Grader revision: {TREE}" if name.startswith("lab-shadow/") else None
    run = {
        "name": name,
        "head_sha": head,
        "conclusion": conclusion,
        "app": {"slug": app},
        "output": {"summary": summary},
        "pull_requests": [],
    }
    payload = {"action": "completed", "repository": {"html_url": repo}, "sender": {"login": ACTOR}, "check_run": run}
    (event,) = head_events("check_run", payload)
    return envelope(event)


def read_name(data: Mapping[str, Any]) -> str:
    return str(data["check_run"]["name"])


@pytest.mark.feature("lab-completed-check-shadow")
def test_the_bus_inbox_hands_each_completed_check_to_its_observer_in_delivery_order_and_still_reads_heads() -> None:
    first, second = completed_check("lint", "success"), completed_check("lab-shadow/lint", "failure", app=LAB_APP)
    bare = HeadEvent("pull_request", REPO, HEAD_B, BASE, ACTOR, "opened")
    deliveries = [Delivery(first), Delivery(envelope(bare)), Delivery(second)]
    seen: list[Mapping[str, Any]] = []
    inbox = scheduler.BusInbox(Consumer([*deliveries]), observe=seen.append)
    assert inbox.heads() == [scheduler.Head(bare, HEAD_B)]
    assert seen == [first.data, second.data]
    assert [delivery.acked for delivery in deliveries] == [True, True, True]
    assert [delivery.termed for delivery in deliveries] == [False, False, False]


@pytest.mark.feature("lab-completed-check-shadow")
@pytest.mark.parametrize(("data", "error"), [({"action": "completed"}, "KeyError"), ({"check_run": None}, "TypeError")])
def test_the_bus_inbox_terminates_a_completed_check_its_observer_cannot_read_with_a_logged_reason(
    data: dict[str, Any], error: str, caplog: pytest.LogCaptureFixture
) -> None:
    good = completed_check("lint", "success")
    broken = replace(good, id="broken", data=data)
    deliveries = [Delivery(broken), Delivery(good)]
    seen: list[str] = []
    inbox = scheduler.BusInbox(Consumer([*deliveries]), observe=lambda found: seen.append(read_name(found)))
    with caplog.at_level(logging.WARNING, logger=scheduler.__name__):
        assert inbox.heads() == []
    assert seen == ["lint"]
    assert [delivery.termed for delivery in deliveries] == [True, False]
    assert [delivery.acked for delivery in deliveries] == [False, True]
    assert caplog.record_tuples == [
        (scheduler.__name__, logging.WARNING, f"terminated lab check broken unreadable: {error}")
    ]


@pytest.mark.feature("lab-completed-check-shadow")
def test_a_bus_inbox_whose_observer_fails_leaves_the_batch_heads_unacknowledged_for_redelivery() -> None:
    bare = HeadEvent("pull_request", REPO, HEAD_B, BASE, ACTOR, "opened")
    deliveries = [Delivery(envelope(bare)), Delivery(completed_check("lint", "success"))]

    def refuse(data: Mapping[str, Any]) -> None:
        raise RedisError("lab redis down")

    inbox = scheduler.BusInbox(Consumer([*deliveries]), observe=refuse)
    with pytest.raises(RedisError, match="lab redis down"):
        inbox.heads()
    assert [delivery.acked for delivery in deliveries] == [False, False]
    assert [delivery.termed for delivery in deliveries] == [False, False]


@pytest.mark.feature("lab-completed-check-shadow")
def test_the_built_scheduler_counts_a_lab_verdict_that_disagrees_with_actions_on_its_metrics_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scheduler.MirrorExpander, "trusted", lambda self, repo: frozenset({ACTOR}))
    monkeypatch.setattr(scheduler.MirrorExpander, "retries", lambda self, repo: Policy())

    def static_jobs(_self: scheduler.MirrorExpander, repo: str) -> frozenset[str]:
        assert repo == f"{GITHUB}.git"
        return frozenset({"lint"})

    monkeypatch.setattr(scheduler.MirrorExpander, "static_jobs", static_jobs)
    monkeypatch.setattr(scheduler, "worker_pods", lambda found: FakeKube())
    environ = {
        scheduler.contract.GIT_MIRROR: str(tmp_path / "node"),
        scheduler.MIRROR_STORE: str(tmp_path / "stores"),
        scheduler.REPOSITORY: f"{GITHUB}.git",
        "AGENTIHOOKS_LAB_APP": LAB_APP,
    }
    checks = [
        completed_check("lint", "success"),
        completed_check("lab-shadow/lint", "failure", app=LAB_APP),
        completed_check("lint", "failure", head=HEAD_B, repo=OTHER),
        completed_check("lab-shadow/lint", "success", head=HEAD_B, app=LAB_APP, repo=OTHER),
    ]
    client, objects = fake_redis(), LocalObjects(tmp_path / "objects")
    spending = budget.Budget(client, lambda: 1000, budget.Policy(100.0, {}))
    built = scheduler.build(environ, client, objects, Consumer([Delivery(check) for check in checks]), spending)
    built.tick(1000)
    exposition = scrape(Run(Lab(client, objects, Inbox(), Expander({})), built), 1000)
    assert '\nci_lab_verdict_mismatch_total{gate="lint"} 1.0\n' in exposition
    assert client.keys(f"agentihooks-ci:shadow:lint:{HEAD_B}") == []


@pytest.mark.feature("lab-completed-check-shadow")
def test_a_scheduler_built_without_the_lab_app_acknowledges_completed_checks_unrecorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scheduler.MirrorExpander, "trusted", lambda self, repo: frozenset({ACTOR}))
    monkeypatch.setattr(scheduler.MirrorExpander, "retries", lambda self, repo: Policy())
    monkeypatch.setattr(scheduler, "worker_pods", lambda found: FakeKube())
    environ = {
        scheduler.contract.GIT_MIRROR: str(tmp_path / "node"),
        scheduler.MIRROR_STORE: str(tmp_path / "stores"),
        scheduler.REPOSITORY: f"{GITHUB}.git",
    }
    deliveries = [Delivery(completed_check("lint", "success"))]
    client = fake_redis()
    spending = budget.Budget(client, lambda: 1000, budget.Policy(100.0, {}))
    built = scheduler.build(environ, client, LocalObjects(tmp_path / "objects"), Consumer([*deliveries]), spending)
    built.tick(1000)
    assert [delivery.acked for delivery in deliveries] == [True]
    assert client.keys("agentihooks-ci:shadow*") == []


@pytest.mark.feature("lab-completed-check-shadow")
def test_the_mirror_expander_reads_the_static_gate_ids_of_dev_as_the_shadow_report_reads_a_tree(
    tmp_path: Path, origin: tuple[Path, str, str]
) -> None:
    repo, _, _ = origin
    folder = ROOT / ".github/workflows"
    found = [*folder.glob("*.yml"), *folder.glob("*.yaml")]
    _commit(repo, {f".github/workflows/{path.name}": path.read_text() for path in found})
    node = tmp_path / "node"
    _git(tmp_path, "init", "-q", "--bare", str(node))
    toolchain = Toolchain("3.12", {}, "", "", "", {})
    expander = scheduler.MirrorExpander(node, tmp_path / "stores", toolchain, _no_tests, _unknown)
    gates = expander.static_jobs(f"file://{repo}")
    assert {"lint", "semgrep"} <= gates
    assert gates == shadow.static_jobs(ROOT)


@pytest.mark.feature("lab-completed-check-shadow")
def test_a_static_gate_id_is_the_first_job_of_its_label() -> None:
    def gate(job: str) -> StaticGate:
        return StaticGate("scan", job, "run", (), (), "", (), ())

    assert shadow.gate_ids([gate("semgrep/scan/run"), gate("lint")]) == frozenset({"semgrep", "lint"})
