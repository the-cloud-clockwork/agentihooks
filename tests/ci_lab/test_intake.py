import hashlib
import json
import time
from pathlib import Path
from typing import Any

import pytest

from scripts.ci_lab import intake, webhooks
from scripts.ci_lab.intake import GradeRequest
from scripts.ci_lab.units import Priority
from scripts.ci_lab.webhooks import HeadEvent
from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.events.memory import open_bus

REPO = "https://github.com/acme/widgets"
ROOT = Path(__file__).resolve().parents[2]
HEAD = "a" * 40
BASE = "b" * 40
PLANT_REF = "refs/heads/diffcheck/lab-plant-size-green"


def push(ref: str = "refs/heads/dev", deleted: bool = False) -> dict[str, Any]:
    return {
        "ref": ref,
        "before": BASE,
        "after": "0" * 40 if deleted else HEAD,
        "deleted": deleted,
        "repository": {"html_url": REPO},
        "sender": {"login": "nestorcolt"},
    }


def pull_request(
    action: str = "opened", author: str = "agenticore-app[bot]", sender: str = "nestorcolt"
) -> dict[str, Any]:
    return {
        "action": action,
        "repository": {"html_url": REPO},
        "sender": {"login": sender},
        "pull_request": {
            "head": {"sha": HEAD, "ref": "feature", "label": "acme:feature"},
            "base": {"sha": BASE, "label": "acme:dev"},
            "user": {"login": author},
        },
    }


def merge_group(action: str = "checks_requested") -> dict[str, Any]:
    return {
        "action": action,
        "repository": {"html_url": REPO},
        "sender": {"login": "github-merge-queue[bot]"},
        "merge_group": {"head_sha": HEAD, "base_sha": BASE, "head_ref": "refs/heads/gh-readonly-queue/dev/pr-7"},
    }


def check_run(action: str = "rerequested", name: str = "lab / unit", pulls: bool = True) -> dict[str, Any]:
    return {
        "action": action,
        "repository": {"html_url": REPO},
        "sender": {"login": "nestorcolt"},
        "check_run": {
            "name": name,
            "head_sha": HEAD,
            "pull_requests": [{"number": 7, "base": {"sha": BASE}}] if pulls else [],
        },
    }


def head(event: str, actor: str, action: str, base: str = BASE, lane: str = "") -> HeadEvent:
    return HeadEvent(event=event, repo=REPO, head=HEAD, base=base, actor=actor, action=action, lane=lane)


@pytest.mark.feature("lab-promotion-grade-only-for-dev-push")
def test_a_push_to_dev_is_a_head_event_on_the_dev_lane() -> None:
    assert webhooks.head_events("push", push()) == [head("push", "nestorcolt", "pushed", lane="dev")]


@pytest.mark.feature("lab-promotion-grade-only-for-dev-push")
@pytest.mark.parametrize(
    "ref",
    ["refs/heads/feature", "refs/heads/main", "refs/heads/devel", "refs/heads/feature/dev", "refs/heads/a-dev"],
)
def test_a_push_to_a_branch_other_than_dev_names_no_head(ref: str) -> None:
    assert webhooks.head_events("push", push(ref=ref)) == []


@pytest.mark.feature("lab-plant-push-intake")
@pytest.mark.parametrize("before", ["0" * 40, BASE])
def test_a_push_to_a_plant_branch_names_a_plant_head_with_no_base_whatever_its_before(before: str) -> None:
    (event,) = webhooks.head_events("push", {**push(ref=PLANT_REF), "before": before})
    assert event == HeadEvent("plant", REPO, HEAD, "", "nestorcolt", "pushed")
    assert webhooks.envelope(event).type == "ci.lab.head.plant"


@pytest.mark.feature("lab-plant-push-intake")
@pytest.mark.parametrize(
    "ref",
    [
        "refs/heads/diffcheck/lab-plant",
        "refs/heads/diffcheck/ci-lab-size",
        "refs/heads/feature/diffcheck/lab-plant-size",
    ],
)
def test_a_push_to_a_branch_outside_the_plant_prefix_stays_an_ordinary_push(ref: str) -> None:
    assert webhooks.head_events("push", push(ref=ref)) == []


@pytest.mark.feature("lab-zero-base-push")
def test_a_new_branch_push_with_a_zero_before_names_no_head() -> None:
    payload = push()
    payload["before"] = "0" * 40
    assert webhooks.head_events("push", payload) == []


@pytest.mark.parametrize(
    "payload",
    [
        push(deleted=True),
        push(ref="refs/tags/v1.2.3"),
        push(ref=PLANT_REF, deleted=True),
        push(ref="refs/tags/diffcheck/lab-plant-size-green"),
    ],
)
def test_a_branch_delete_or_a_tag_push_names_no_head(payload: dict[str, Any]) -> None:
    assert webhooks.head_events("push", payload) == []


@pytest.mark.parametrize("action", ["opened", "synchronize", "reopened"])
def test_a_pull_request_that_moves_its_head_is_a_head_event(action: str) -> None:
    assert webhooks.head_events("pull_request", pull_request(action)) == [
        head("pull_request", "agenticore-app[bot]", action, lane="feature")
    ]


@pytest.mark.parametrize("action", ["enqueued", "dequeued", "edited", "labeled"])
def test_other_pull_request_actions_name_no_head(action: str) -> None:
    assert webhooks.head_events("pull_request", pull_request(action)) == []


def test_a_merge_group_asking_for_checks_is_a_head_event() -> None:
    assert webhooks.head_events("merge_group", merge_group()) == [
        head("merge_group", "github-merge-queue[bot]", "checks_requested")
    ]
    assert webhooks.head_events("merge_group", merge_group("destroyed")) == []


def test_a_rerequested_lab_check_is_a_head_event() -> None:
    assert webhooks.head_events("check_run", check_run()) == [head("check_run", "nestorcolt", "rerequested")]
    assert webhooks.head_events("check_run", check_run(pulls=False)) == [
        head("check_run", "nestorcolt", "rerequested", base="")
    ]


@pytest.mark.parametrize("payload", [check_run("created"), check_run(name="unit (3.12)")])
def test_other_check_runs_name_no_head(payload: dict[str, Any]) -> None:
    assert webhooks.head_events("check_run", payload) == []


def completed(name: str = "lint", conclusion: str | None = "success", summary: str | None = None) -> dict[str, Any]:
    payload = check_run("completed", name)
    run = {
        **payload["check_run"],
        "id": 11,
        "conclusion": conclusion,
        "app": {"slug": "github-actions", "name": "GitHub Actions"},
        "output": {"title": "done", "summary": summary, "text": "log"},
    }
    return {**payload, "check_run": run}


@pytest.mark.feature("lab-completed-check-shadow")
def test_a_completed_check_run_rides_its_own_subject_with_the_fields_the_shadow_comparison_reads() -> None:
    (event,) = webhooks.head_events("check_run", completed(summary="Grader revision: g2"))
    before = time.time_ns() // 1_000_000
    envelope = webhooks.envelope(event)
    after = time.time_ns() // 1_000_000
    assert envelope.type == "ci.lab.head.check_completed"
    assert (envelope.swarm, envelope.entity, envelope.key, envelope.author, envelope.version) == (
        "ci-lab",
        "head",
        f"{REPO}@{HEAD}",
        "nestorcolt",
        0,
    )
    assert before <= envelope.time <= after
    assert envelope.data == {
        "action": "completed",
        "repository": {"html_url": REPO},
        "check_run": {
            "name": "lint",
            "head_sha": HEAD,
            "conclusion": "success",
            "app": {"slug": "github-actions"},
            "output": {"summary": "Grader revision: g2"},
        },
    }
    assert envelope.id == hashlib.sha256(json.dumps(envelope.data).encode()).hexdigest()
    (failed,) = webhooks.head_events("check_run", completed(conclusion="failure"))
    assert webhooks.envelope(failed).id != envelope.id


@pytest.mark.parametrize("name", [None, "ping", "check_suite", "issues"])
def test_other_events_name_no_head(name: str | None) -> None:
    assert webhooks.head_events(name, {"zen": "x"}) == []


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("push", {"ref": "refs/heads/dev"}),
        ("pull_request", {"action": "opened"}),
        ("merge_group", []),
        ("check_run", {"action": "completed", "check_run": {"name": "lint", "head_sha": HEAD}}),
    ],
)
def test_a_malformed_head_payload_raises(name: str, payload: Any) -> None:
    with pytest.raises((KeyError, TypeError)):
        webhooks.head_events(name, payload)


def test_a_head_envelope_carries_the_event_on_its_own_subject() -> None:
    event = head("merge_group", "github-merge-queue[bot]", "checks_requested")
    before = time.time_ns() // 1_000_000
    envelope = webhooks.envelope(event)
    after = time.time_ns() // 1_000_000
    assert envelope.type == "ci.lab.head.merge_group"
    assert (envelope.swarm, envelope.entity, envelope.key, envelope.author, envelope.version) == (
        "ci-lab",
        "head",
        f"{REPO}@{HEAD}",
        "github-merge-queue[bot]",
        0,
    )
    assert before <= envelope.time <= after
    assert envelope.data == {
        "event": "merge_group",
        "repo": REPO,
        "head": HEAD,
        "base": BASE,
        "actor": "github-merge-queue[bot]",
        "action": "checks_requested",
    }
    assert envelope.id == hashlib.sha256(json.dumps(envelope.data).encode()).hexdigest()


class Published:
    def __init__(self) -> None:
        self.entries: list[tuple[str, Envelope]] = []

    def publish(self, subject: str, event: Envelope) -> bool:
        self.entries.append((subject, event))
        return True


def test_the_lab_intake_publishes_each_envelope_on_its_type() -> None:
    envelopes = [
        webhooks.envelope(head("push", "nestorcolt", "pushed")),
        webhooks.envelope(head("notice", "x", "pushed")),
    ]
    published = Published()
    webhooks.LabIntake(published, b"notice-secret").publish(envelopes)
    assert published.entries == [("ci.lab.head.push", envelopes[0]), ("ci.lab.head.notice", envelopes[1])]
    bus = open_bus(BusConfig())
    consumer = bus.lab_heads.consumer("lab", "ci.lab.head.>")
    webhooks.LabIntake(bus.lab_heads, b"notice-secret").publish(envelopes)
    assert [delivery.event for delivery in consumer.fetch(10, 0)] == envelopes


def test_a_resent_head_keeps_its_envelope_id_so_the_bus_drops_it() -> None:
    event = head("pull_request", "nestorcolt", "synchronize")
    first, again = webhooks.envelope(event), webhooks.envelope(event)
    assert first.id == again.id
    assert webhooks.envelope(head("pull_request", "nestorcolt", "reopened")).id != first.id
    bus = open_bus(BusConfig())
    consumer = bus.lab_heads.consumer("lab", "ci.lab.head.>")
    webhooks.LabIntake(bus.lab_heads, b"notice-secret").publish([first, again])
    assert [delivery.event for delivery in consumer.fetch(10, 0)] == [first]


def test_the_lab_intake_refuses_to_run_without_a_notice_secret() -> None:
    with pytest.raises(ValueError, match=webhooks.NOTICE_SECRET_ENV):
        webhooks.LabIntake(open_bus(BusConfig()).events, b"")


def test_a_head_notice_names_its_repository_head_base_and_actor() -> None:
    notice = {"repo": REPO, "head": HEAD, "base": BASE, "actor": "agenticore-app[bot]"}
    assert webhooks.notice_event(notice) == head("notice", "agenticore-app[bot]", "pushed")


@pytest.mark.parametrize(
    "notice",
    [
        {"repo": REPO, "head": HEAD, "base": BASE, "actor": ""},
        {"repo": REPO, "head": HEAD, "base": 3, "actor": "nestorcolt"},
    ],
)
def test_a_head_notice_with_an_empty_or_non_text_field_is_refused(notice: dict[str, Any]) -> None:
    with pytest.raises(ValueError) as refused:
        webhooks.notice_event(notice)
    assert str(refused.value) == "a lab head notice needs non empty repo, head, base, actor"


def test_a_head_notice_missing_a_field_is_refused() -> None:
    with pytest.raises(KeyError, match="actor"):
        webhooks.notice_event({"repo": REPO, "head": HEAD, "base": BASE})


@pytest.mark.parametrize(
    ("event", "tier", "bypass"),
    [
        (head("push", "nestorcolt", "pushed"), Priority.PROMOTION, False),
        (head("pull_request", "nestorcolt", "synchronize"), Priority.PULL_REQUEST, False),
        (head("merge_group", "github-merge-queue[bot]", "checks_requested"), Priority.QUEUE, False),
        (head("check_run", "nestorcolt", "rerequested"), Priority.PULL_REQUEST, True),
        (head("notice", "nestorcolt", "pushed"), Priority.PULL_REQUEST, False),
    ],
)
def test_a_grade_request_carries_the_head_its_tier_and_whether_it_bypasses_a_negative(
    event: HeadEvent, tier: Priority, bypass: bool
) -> None:
    assert intake.request_from(event) == GradeRequest(
        repo=REPO, head=HEAD, base=BASE, tier=tier, actor=event.actor, bypass_negative=bypass
    )


@pytest.mark.feature("lab-plant-push-intake")
def test_a_plant_head_asks_for_a_pull_request_grade_before_its_base_is_resolved() -> None:
    request = intake.request_from(head("plant", "nestorcolt", "pushed", base=""))
    assert request == GradeRequest(
        repo=REPO, head=HEAD, base="", tier=Priority.PULL_REQUEST, actor="nestorcolt", bypass_negative=False, plant=True
    )


def test_an_unknown_event_has_no_tier() -> None:
    with pytest.raises(ValueError, match="Unknown lab event"):
        intake.request_from(head("issues", "nestorcolt", "opened"))


def test_the_trust_list_is_the_runner_trust_list() -> None:
    trusted = intake.trusted_actors(ROOT)
    assert {"nestorcolt", "agenticore-app[bot]", "tcc-main-ci[bot]", "github-merge-queue[bot]"} == trusted


def test_the_trust_list_refuses_a_malformed_file(tmp_path: Path) -> None:
    actors = tmp_path / ".github" / "workflows" / "trusted-actors.json"
    actors.parent.mkdir(parents=True)
    actors.write_text('{"actors": ["nestorcolt"], "queue_actor": "github-merge-queue[bot]"}')
    with pytest.raises(ValueError, match="one operator and two distinct app bots"):
        intake.trusted_actors(tmp_path)


def test_only_a_trusted_actors_head_becomes_a_grade_request() -> None:
    trusted = intake.trusted_actors(ROOT)
    trusted_head = head("pull_request", "agenticore-app[bot]", "opened")
    assert intake.admit(trusted_head, trusted) == intake.request_from(trusted_head)
    assert intake.admit(head("pull_request", "stranger", "opened"), trusted) is None
    assert intake.admit(head("push", "stranger[bot]", "pushed"), trusted) is None


def test_a_trusted_sender_reopening_an_untrusted_authors_pull_request_admits_nothing() -> None:
    (event,) = webhooks.head_events("pull_request", pull_request("reopened", author="stranger", sender="nestorcolt"))
    assert isinstance(event, HeadEvent)
    assert event.actor == "stranger"
    assert intake.admit(event, intake.trusted_actors(ROOT)) is None


def test_a_cached_negative_answers_a_request_unless_it_is_a_rerequested_check() -> None:
    seen: list[tuple[str, str]] = []

    def negative(repo: str, sha: str) -> bool:
        seen.append((repo, sha))
        return True

    pushed = intake.request_from(head("pull_request", "nestorcolt", "synchronize"))
    rerun = intake.request_from(head("check_run", "nestorcolt", "rerequested"))
    assert intake.answered(pushed, negative) is True
    assert intake.answered(rerun, negative) is False
    assert intake.answered(pushed, lambda repo, sha: False) is False
    assert seen == [(REPO, HEAD)]


def open_pull(sha: str, author: str = "agenticore-app[bot]") -> dict[str, Any]:
    return {
        "head": {"sha": sha, "ref": "feature", "label": "acme:feature"},
        "base": {"sha": BASE, "label": "acme:dev", "repo": {"html_url": REPO}},
        "user": {"login": author},
    }


def test_reconcile_lists_the_open_heads_without_a_grade() -> None:
    graded = {(REPO, "c" * 40)}
    seen: list[tuple[str, str]] = []

    def has_grade(repo: str, sha: str) -> bool:
        seen.append((repo, sha))
        return (repo, sha) in graded

    pulls = [open_pull(HEAD), open_pull("c" * 40), open_pull("d" * 40, "nestorcolt")]
    assert intake.reconcile(pulls, has_grade) == [
        HeadEvent("pull_request", REPO, HEAD, BASE, "agenticore-app[bot]", "reconcile", "feature"),
        HeadEvent("pull_request", REPO, "d" * 40, BASE, "nestorcolt", "reconcile", "feature"),
    ]
    assert seen == [(REPO, HEAD), (REPO, "c" * 40), (REPO, "d" * 40)]
    assert intake.reconcile([], has_grade) == []


def test_a_reconciled_head_is_graded_in_the_pull_request_tier() -> None:
    (event,) = intake.reconcile([open_pull(HEAD)], lambda repo, sha: False)
    assert intake.request_from(event).tier is Priority.PULL_REQUEST
    assert intake.request_from(event).bypass_negative is False
