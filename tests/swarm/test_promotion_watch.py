import json
import subprocess
from types import SimpleNamespace

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import promotion_watch
from scripts.swarm.ledger_client import LedgerClient, LedgerRefused
from scripts.swarm.ledger_events import Mail
from scripts.swarm.store import PREFIX, RedisStore, SwarmConfig

RUNS = "repos/{owner}/{repo}/actions/workflows/test.yml/runs?branch=dev&event=push&status=completed&per_page=20"
JOBS_JQ = ".jobs[] | {id, name, conclusion, check_run_url, steps} | @json"
SHA = "c0ffee" + "0" * 34
RED = {"id": 900, "conclusion": "failure", "head_sha": SHA, "html_url": "https://github.com/o/r/actions/runs/900"}
GREEN = {
    "id": 901,
    "conclusion": "success",
    "head_sha": "f" * 40,
    "html_url": "https://github.com/o/r/actions/runs/901",
}
JOBS = [
    {"name": "lint", "conclusion": "success"},
    {"name": "unit (3.11, 1)", "conclusion": "failure"},
    {"name": "helm-kind", "conclusion": "timed_out"},
    {"name": "coverage-ratchet", "conclusion": "cancelled"},
    {"name": "Gate — Required", "conclusion": "failure"},
]
NOT_DUPLICATE = (
    "The promotion watch files one task per failed dev job and run, and holds a job's cause while its task is open."
)
NOW = 10_000_000
MAX_ALERT = 2000
SLUG = "sw"


class Ledger:
    def __init__(self, tasks=(), alerts=()):
        self.tasks, self.alerts = [dict(t) for t in tasks], [dict(a) for a in alerts]
        self.added, self.updates, self.comments, self.raised, self.closed = [], [], [], [], []

    def doc(self):
        return {"tasks": self.tasks, "alerts": self.alerts}

    def add_task(self, slug, fields, by):
        self.added.append((slug, fields, by))
        self.tasks.append({"id": fields["task"], "title": fields["title"], "state": "open"})

    def update_task(self, slug, task_id, fields, by=None, if_state=()):
        self.updates.append((slug, task_id, fields, by, if_state))
        task = next(t for t in self.tasks if t["id"] == task_id)
        if task["state"] in if_state:
            task.update(fields)
        return dict(task)

    def comment(self, slug, task_id, text, by):
        self.comments.append((slug, task_id, text, by))

    def raise_alert(self, slug, source, text):
        assert len(text) <= MAX_ALERT
        if any(a["source"] == source and a["state"] != "done" for a in self.alerts):
            return
        self.raised.append((slug, source, text))
        self.alerts.append({"id": f"al-{len(self.alerts)}", "source": source, "text": text, "state": "open"})

    def close_alert(self, slug, alert_id, outcome):
        self.closed.append((slug, alert_id, outcome))
        next(a for a in self.alerts if a["id"] == alert_id)["state"] = "done"


def gh(runs, jobs=(), calls=None):
    def run(argv, **kwargs):
        if calls is not None:
            calls.append((argv, kwargs))
        body = jobs if "/jobs" in argv[2] else runs
        return subprocess.CompletedProcess(argv, 0, "\n".join(json.dumps(item) for item in body), "")

    return run


@pytest.fixture
def store():
    import fakeredis

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


def refresh(store, ledger, run, now=NOW):
    config = SimpleNamespace(repo="/repo")
    return promotion_watch.refresh(SLUG, config, store, ledger, ledger.doc(), now, run)


def filed(store):
    return store.redis.hgetall(promotion_watch.key(SLUG, "jobs"))


SUPERSEDED = "Dev advanced before Sonar analysis; this unscanned revision cannot be promoted."


def sonar_run(message, extra_jobs=(), extra_steps=()):
    sonar = {
        "name": "sonar",
        "conclusion": "failure",
        "check_run_url": "https://api.github.com/repos/o/r/check-runs/123",
        "steps": [
            {"name": "Check dev still points at this commit", "conclusion": "failure"},
            {"name": "SonarQube scan", "conclusion": "skipped"},
            *extra_steps,
        ],
    }
    found = [sonar, {"name": "Gate — Required", "conclusion": "failure"}, *extra_jobs]

    def run(argv, **kwargs):
        assert kwargs["cwd"] == "/repo"
        if "/annotations" in argv[2]:
            assert argv == [
                "gh",
                "api",
                "https://api.github.com/repos/o/r/check-runs/123/annotations?per_page=100",
                "--paginate",
                "--jq",
                ".[] | {annotation_level, message} | @json",
            ]
            body = [
                {"annotation_level": "warning", "message": "Node.js 20 actions are deprecated."},
                {"annotation_level": "failure", "message": "Process completed with exit code 1."},
                {"annotation_level": "failure", "message": message},
            ]
        elif "/jobs" in argv[2]:
            assert argv == [
                "gh",
                "api",
                "repos/{owner}/{repo}/actions/runs/900/jobs?per_page=100",
                "--paginate",
                "--jq",
                JOBS_JQ,
            ]
            body = found
        else:
            body = [RED]
        return subprocess.CompletedProcess(argv, 0, "\n".join(json.dumps(item) for item in body), "")

    return run


def test_a_superseded_sonar_run_is_reported_without_fixers_or_alerts(store):
    ledger = Ledger()
    actions = refresh(store, ledger, sonar_run(SUPERSEDED))
    assert actions == ["dev push run 900 was superseded before Sonar analysis"]
    assert ledger.added == []
    assert ledger.raised == []
    assert ledger.updates == []
    assert ledger.closed == []
    assert filed(store) == {}
    assert store.redis.get(promotion_watch.key(SLUG, "seen")) == "900"


@pytest.mark.parametrize(
    "message",
    ["fatal: unable to access origin", "", SUPERSEDED + " extra error", "Process completed with exit code 1."],
)
def test_a_real_sonar_failure_still_files_a_fixer(store, message):
    ledger = Ledger()
    actions = refresh(store, ledger, sonar_run(message))
    assert [fields["title"] for _, fields, _ in ledger.added] == ["Promotion red: sonar"]
    assert len(ledger.raised) == 1
    assert actions[-2:] == [
        "raised the promotion alert for dev push run 900",
        "told the master about the new promotion causes of dev push run 900",
    ]
    assert filed(store)["promo-900-0"].startswith("sonar: Check dev still points at this commit")


def test_a_superseded_run_with_another_failed_job_still_files_real_fixers(store):
    ledger = Ledger()
    refresh(store, ledger, sonar_run(SUPERSEDED, [{"name": "coverage-ratchet", "conclusion": "failure"}]))
    assert [fields["title"] for _, fields, _ in ledger.added] == [
        "Promotion red: coverage-ratchet",
        "Promotion red: sonar",
    ]
    assert len(ledger.raised) == 1


def test_another_failed_sonar_step_is_not_superseded(store):
    ledger = Ledger()
    refresh(
        store,
        ledger,
        sonar_run(
            SUPERSEDED,
            extra_steps=[
                {"name": "Post checkout", "conclusion": "failure"},
            ],
        ),
    )
    assert [fields["title"] for _, fields, _ in ledger.added] == ["Promotion red: sonar"]
    assert len(ledger.raised) == 1


def test_keys_sit_under_the_swarm_prefix():
    assert promotion_watch.key(SLUG, "seen") == f"{PREFIX}:sw:promotion:seen"


def test_failed_jobs_names_failed_and_timed_out_jobs_and_leaves_out_the_gate_when_another_job_failed():
    assert promotion_watch.failed_jobs(JOBS) == ["helm-kind", "unit (3.11, 1)"]


def test_failed_jobs_keeps_the_gate_when_it_failed_alone():
    jobs = [{"name": "lint", "conclusion": "success"}, {"name": "Gate — Required", "conclusion": "failure"}]
    assert promotion_watch.failed_jobs(jobs) == ["Gate — Required"]


def test_jobs_reads_every_job_of_the_run():
    calls = []
    assert promotion_watch.jobs("/repo", 900, gh([], JOBS, calls)) == JOBS
    assert calls == [
        (
            ["gh", "api", "repos/{owner}/{repo}/actions/runs/900/jobs?per_page=100", "--paginate", "--jq", JOBS_JQ],
            {"cwd": "/repo", "capture_output": True, "text": True, "check": True, "timeout": 60},
        )
    ]


def test_title_names_the_job_without_dashes():
    assert promotion_watch.title("helm-kind") == "Promotion red: helm-kind"
    assert promotion_watch.title("Gate — Required") == "Promotion red: Gate Required"
    assert promotion_watch.title("ledger-load / load") == "Promotion red: ledger-load load"
    assert promotion_watch.title("queue_retry") == "Promotion red: queue retry"


def test_a_job_name_the_ledger_would_refuse_gets_a_plain_title():
    assert promotion_watch.title("check docs.md") == "Promotion red: a dev job"


def test_the_watch_reads_the_newest_finished_dev_push_run(store):
    calls = []
    refresh(store, Ledger(), gh([GREEN], calls=calls))
    assert calls[0][0] == ["gh", "api", RUNS, "--jq", ".workflow_runs[] | {id, conclusion, head_sha, html_url} | @json"]


def test_a_red_run_files_one_urgent_ci_task_per_failed_job_with_run_and_sha(store):
    calls, ledger = [], Ledger()
    actions = refresh(store, ledger, gh([RED], JOBS, calls))
    kwargs = {"cwd": "/repo", "capture_output": True, "text": True, "check": True, "timeout": 60}
    assert calls == [
        (["gh", "api", RUNS, "--jq", ".workflow_runs[] | {id, conclusion, head_sha, html_url} | @json"], kwargs),
        (
            ["gh", "api", "repos/{owner}/{repo}/actions/runs/900/jobs?per_page=100", "--paginate", "--jq", JOBS_JQ],
            kwargs,
        ),
    ]
    assert {(slug, by) for slug, _, by in ledger.added} == {(SLUG, "swarm")}
    assert [fields for _, fields, _ in ledger.added] == [
        {
            "task": "promo-900-0",
            "title": "Promotion red: helm-kind",
            "lane": "ci",
            "kind": "ci",
            "rank": "urgent",
            "description": (
                f"The dev push run 900 of Tests failed in the job helm-kind at commit {SHA}: "
                "https://github.com/o/r/actions/runs/900. Fix it on dev; the swarm closes this task when a later "
                "dev push run passes."
            ),
            "not_duplicate": NOT_DUPLICATE,
        },
        {
            "task": "promo-900-1",
            "title": "Promotion red: unit (3.11, 1)",
            "lane": "ci",
            "kind": "ci",
            "rank": "urgent",
            "description": (
                f"The dev push run 900 of Tests failed in the job unit (3.11, 1) at commit {SHA}: "
                "https://github.com/o/r/actions/runs/900. Fix it on dev; the swarm closes this task when a later "
                "dev push run passes."
            ),
            "not_duplicate": NOT_DUPLICATE,
        },
    ]
    assert {by for _, _, by in ledger.added} == {"swarm"}
    assert ledger.raised == [
        (
            SLUG,
            "promotion",
            f"Dev promotion is red: push run 900 at commit {SHA} failed in helm-kind, unit (3.11, 1). "
            "https://github.com/o/r/actions/runs/900",
        )
    ]
    assert actions == [
        "filed promo-900-0 for the failed dev job helm-kind",
        "filed promo-900-1 for the failed dev job unit (3.11, 1)",
        "raised the promotion alert for dev push run 900",
        "told the master about the new promotion causes of dev push run 900",
    ]
    assert filed(store) == {"promo-900-0": "helm-kind: ", "promo-900-1": "unit (3.11, 1): "}
    assert master_items(store) == [
        "Dev push run 900 of Tests failed on new causes, so the swarm filed promo-900-0, promo-900-1 in the ci lane "
        "and raised a promotion alert: https://github.com/o/r/actions/runs/900"
    ]
    assert store.redis.get(promotion_watch.key(SLUG, "seen")) == "900"


def test_a_job_with_an_open_promotion_task_is_not_filed_again_and_done_ones_are_forgotten(store):
    store.redis.hset(
        promotion_watch.key(SLUG, "jobs"),
        mapping={"promo-800-0": "helm-kind", "promo-700-0": "unit (3.11, 1)", "promo-600-0": "lint"},
    )
    tasks = [
        {"id": "promo-800-0", "title": "Promotion red: helm-kind", "state": "claimed"},
        {"id": "promo-700-0", "title": "Promotion red: unit (3.11, 1)", "state": "done"},
    ]
    alert = {"id": "al-3", "source": "promotion", "text": "older", "state": "claimed"}
    ledger = Ledger(tasks, [alert])
    actions = refresh(store, ledger, gh([RED], JOBS))
    assert [fields["task"] for _, fields, _ in ledger.added] == ["promo-900-1"]
    assert len(ledger.raised) == 1
    assert actions == [
        "filed promo-900-1 for the failed dev job unit (3.11, 1)",
        "raised the promotion alert for dev push run 900",
        "told the master about the new promotion causes of dev push run 900",
    ]
    assert filed(store) == {"promo-800-0": "helm-kind", "promo-900-1": "unit (3.11, 1): "}


def test_jobs_that_share_a_title_each_get_a_task(store):
    jobs = [{"name": "queue_retry", "conclusion": "failure"}, {"name": "queue retry", "conclusion": "failure"}]
    ledger = Ledger()
    refresh(store, ledger, gh([RED], jobs))
    assert [(fields["task"], fields["title"]) for _, fields, _ in ledger.added] == [
        ("promo-900-0", "Promotion red: queue retry"),
        ("promo-900-1", "Promotion red: queue retry"),
    ]


def test_a_red_run_with_no_failed_job_raises_the_alert_alone(store):
    ledger = Ledger()
    actions = refresh(store, ledger, gh([RED], [{"name": "lint", "conclusion": "cancelled"}]))
    assert ledger.added == []
    assert ledger.raised == [
        (
            SLUG,
            "promotion",
            f"Dev promotion is red: push run 900 at commit {SHA} failed. https://github.com/o/r/actions/runs/900",
        )
    ]
    assert actions == ["raised the promotion alert for dev push run 900"]


def test_a_run_retried_after_a_partial_write_files_only_the_jobs_it_missed(store):
    ledger = Ledger([{"id": "promo-900-0", "title": "Promotion red: helm-kind", "state": "done"}])
    actions = refresh(store, ledger, gh([RED], JOBS))
    assert [fields["task"] for _, fields, _ in ledger.added] == ["promo-900-1"]
    assert actions == [
        "filed promo-900-1 for the failed dev job unit (3.11, 1)",
        "raised the promotion alert for dev push run 900",
        "told the master about the new promotion causes of dev push run 900",
    ]


def test_a_run_already_handled_is_not_read_for_jobs_again(store):
    store.redis.set(promotion_watch.key(SLUG, "seen"), 900)
    calls, ledger = [], Ledger()
    assert refresh(store, ledger, gh([RED], JOBS, calls)) == []
    assert len(calls) == 1 and ledger.added == [] and ledger.raised == []


@pytest.mark.parametrize("slug", ["proof-323133-cxc9-1", "proof-ed3ec8-t12-3"])
def test_a_proof_swarm_never_reads_promotion_runs_or_files_fixers(store, slug):
    calls, ledger = [], Ledger()
    config = SimpleNamespace(repo="/repo")
    actions = promotion_watch.refresh(slug, config, store, ledger, ledger.doc(), NOW, gh([RED], JOBS, calls))
    assert (actions, calls, ledger.added, ledger.raised) == ([], [], [], [])
    assert store.redis.keys("*") == []


def test_a_slug_that_only_resembles_a_proof_swarm_still_files_fixers(store):
    calls, ledger = [], Ledger()
    config = SimpleNamespace(repo="/repo")
    promotion_watch.refresh("proof-323133-cxc9", config, store, ledger, ledger.doc(), NOW, gh([RED], JOBS, calls))
    assert calls and ledger.added


def test_the_watch_reads_at_most_once_per_refresh_window(store):
    calls = []
    refresh(store, Ledger(), gh([GREEN], calls=calls))
    refresh(store, Ledger(), gh([GREEN], calls=calls), NOW + promotion_watch.REFRESH_MS - 1)
    assert len(calls) == 1
    refresh(store, Ledger(), gh([GREEN], calls=calls), NOW + promotion_watch.REFRESH_MS)
    assert len(calls) == 2


def test_a_green_run_closes_open_and_blocked_promotion_tasks_and_the_alert(store):
    tasks = [
        {"id": "promo-900-0", "title": "Promotion red: helm-kind", "state": "open"},
        {"id": "promo-900-1", "title": "Promotion red: unit (3.11, 1)", "state": "blocked"},
        {"id": "promo-800-0", "title": "Promotion red: lint", "state": "claimed"},
        {"id": "promo-700-0", "title": "Promotion red: lint", "state": "done"},
        {"id": "t9", "title": "Other work", "state": "open"},
    ]
    alerts = [
        {"id": "al-1", "source": "promotion", "text": "red", "state": "open"},
        {"id": "al-2", "source": "size", "text": "big", "state": "open"},
        {"id": "al-0", "source": "promotion", "text": "older", "state": "done"},
    ]
    store.redis.set(promotion_watch.key(SLUG, "seen"), 900)
    ledger = Ledger(tasks, alerts)
    actions = refresh(store, ledger, gh([GREEN]))
    assert ledger.updates == [
        (SLUG, "promo-900-0", {"state": "done"}, "swarm", ("open", "blocked")),
        (SLUG, "promo-900-1", {"state": "done"}, "swarm", ("open", "blocked")),
    ]
    cleared = "A later dev push run of Tests passed, so the swarm closed this promotion task."
    assert ledger.comments == [(SLUG, "promo-900-0", cleared, "swarm"), (SLUG, "promo-900-1", cleared, "swarm")]
    assert ledger.closed == [(SLUG, "al-1", "Dev push run 901 of Tests passed.")]
    assert ledger.added == [] and ledger.raised == []
    assert actions == [
        "closed promo-900-0, dev push run 901 passed",
        "closed promo-900-1, dev push run 901 passed",
        "cleared the promotion alert, dev push run 901 passed",
    ]
    assert store.redis.get(promotion_watch.key(SLUG, "seen")) == "901"


def test_a_task_claimed_before_the_close_lands_is_not_reported_closed(store):
    ledger = Ledger([{"id": "promo-900-0", "title": "Promotion red: helm-kind", "state": "open"}])
    ledger.update_task = lambda slug, task_id, fields, by="swarm", if_state=(): {"id": task_id, "state": "claimed"}
    assert refresh(store, ledger, gh([GREEN])) == []
    assert ledger.comments == []


def test_a_green_run_releases_the_cause_of_a_fixer_the_ledger_kept_blocked(store):
    ledger = Ledger()
    refresh(store, ledger, smoke_run("Build the image", []))
    ledger.tasks[0]["state"] = "blocked"
    ledger.update_task = lambda slug, task_id, fields, by="swarm", if_state=(): {"id": task_id, "state": "blocked"}
    refresh(store, ledger, gh([GREEN]), NOW * 2)
    assert filed(store) == {}
    refresh(store, ledger, smoke_run("Build the image", [], 902), NOW * 3)
    assert [fields["task"] for _, fields, _ in ledger.added] == ["promo-900-0", "promo-902-0"]


@pytest.mark.parametrize("state", ["claimed", "pr"])
def test_a_fixer_someone_works_through_a_green_run_still_holds_its_job(store, state):
    ledger = Ledger()
    refresh(store, ledger, smoke_run("Build the image", []))
    ledger.tasks[0]["state"] = state
    refresh(store, ledger, gh([GREEN]), NOW * 2)
    assert filed(store) == {"promo-900-0": "swarm-image / smoke: Build the image"}
    refresh(store, ledger, smoke_run("Build the image", [], 902), NOW * 3)
    assert [fields["task"] for _, fields, _ in ledger.added] == ["promo-900-0"]


def test_a_failed_read_skips_the_pass_and_leaves_the_run_unhandled(store, capsys):
    def broken(argv, **kwargs):
        raise subprocess.CalledProcessError(1, argv, stderr="gh: rate limited")

    assert refresh(store, Ledger(), broken) == []
    assert store.redis.get(promotion_watch.key(SLUG, "seen")) is None
    assert capsys.readouterr().err == "promotion watch skipped, reading dev push runs failed: gh: rate limited\n"


def test_a_ledger_refusal_leaves_the_run_unhandled_for_the_next_pass(store):
    ledger = Ledger()

    def refuse(slug, fields, by):
        raise LedgerRefused("refused")

    ledger.add_task = refuse
    with pytest.raises(LedgerRefused):
        refresh(store, ledger, gh([RED], JOBS))
    assert store.redis.get(promotion_watch.key(SLUG, "seen")) is None


def test_the_tick_hands_the_watch_its_ledger_state_and_clock(store, monkeypatch):
    from scripts.swarm import tick
    from tests.swarm.test_tick import FakeLedger, FakeRuntime

    store.create(SwarmConfig(SLUG, "/repo", max_eng=0, max_ci=0))
    seen = []
    monkeypatch.setattr(promotion_watch, "refresh", lambda *args: seen.append(args) or ["watched"])
    ledger = FakeLedger([{"id": "a"}])
    actions = tick.tick(SLUG, store, ledger, FakeRuntime(), now_ms=4_321)
    [(slug, config, got_store, got_ledger, doc, now)] = seen
    assert (slug, config.slug, got_store, got_ledger, [t["id"] for t in doc["tasks"]], now) == (
        SLUG,
        SLUG,
        store,
        ledger,
        ["a"],
        4_321,
    )
    assert "watched" in actions


def test_a_ledger_without_task_or_alert_lists_still_files_and_closes(store):
    config = SimpleNamespace(repo="/repo")
    red = promotion_watch.refresh(SLUG, config, store, Ledger(), {}, NOW, gh([RED], JOBS))
    assert red[-2] == "raised the promotion alert for dev push run 900"
    green = promotion_watch.refresh(SLUG, config, store, Ledger(), {}, NOW * 2, gh([GREEN]))
    assert green == []
    assert store.redis.get(promotion_watch.key(SLUG, "seen")) == "901"


def test_the_first_window_reads_once_the_refresh_interval_has_passed_since_zero(store):
    calls = []
    refresh(store, Ledger(), gh([GREEN], calls=calls), promotion_watch.REFRESH_MS)
    assert len(calls) == 1


def test_a_run_numbered_one_is_handled_on_a_fresh_watch(store):
    ledger = Ledger()
    refresh(store, ledger, gh([{**RED, "id": 1}], JOBS))
    assert [fields["task"] for _, fields, _ in ledger.added] == ["promo-1-0", "promo-1-1"]


def test_a_read_error_without_stderr_names_the_error(store, capsys):
    def missing(argv, **kwargs):
        raise FileNotFoundError("gh is not installed")

    assert refresh(store, Ledger(), missing) == []
    assert capsys.readouterr().err == "promotion watch skipped, reading dev push runs failed: gh is not installed\n"


def test_the_ledger_client_raises_and_closes_alerts_as_the_swarm(monkeypatch):
    sent = []
    monkeypatch.setattr(LedgerClient, "_call", lambda self, slug, ops=None: sent.append((slug, ops)))
    client = LedgerClient()
    client.raise_alert(SLUG, "promotion", "Dev promotion is red")
    client.close_alert(SLUG, "al-1", "Dev passed")
    assert [(slug, [{k: v for k, v in op.items() if k != "id"} for op in ops]) for slug, ops in sent] == [
        (SLUG, [{"op": "alert_raise", "by": "swarm", "source": "promotion", "text": "Dev promotion is red"}]),
        (SLUG, [{"op": "alert_close", "by": "swarm", "target": "al-1", "outcome": "Dev passed"}]),
    ]
    assert [ops[0]["id"].split("-")[0] for _, ops in sent] == ["alert_raise", "alert_close"]


SMOKE_URL = "https://api.github.com/repos/o/r/check-runs/77"


def smoke_run(step, messages, run_id=900):
    smoke = {
        "name": "swarm-image / smoke",
        "conclusion": "failure",
        "check_run_url": SMOKE_URL,
        "steps": [{"name": "Set up job", "conclusion": "success"}, {"name": step, "conclusion": "failure"}],
    }
    found = [smoke, {"name": "Gate — Required", "conclusion": "failure"}]
    red = {**RED, "id": run_id, "html_url": f"https://github.com/o/r/actions/runs/{run_id}"}

    def run(argv, **kwargs):
        if "/annotations" in argv[2]:
            body = [
                {"annotation_level": "warning", "message": "Node.js 20 is deprecated."},
                {"annotation_level": "failure", "message": "Process completed with exit code 1."},
                *({"annotation_level": "failure", "message": m} for m in messages),
            ]
        elif "/jobs" in argv[2]:
            body = found
        else:
            body = [red]
        return subprocess.CompletedProcess(argv, 0, "\n".join(json.dumps(item) for item in body), "")

    return run


def master_items(store):
    inbox = InboxStore(store.redis)
    return [item.text for item in inbox.inbox(Mail(inbox, store, SLUG).master)]


def test_cause_names_the_failed_steps_and_failure_messages():
    job = {
        "name": "coverage-ratchet",
        "check_run_url": SMOKE_URL,
        "steps": [
            {"name": "Hold every line the base ran", "conclusion": "failure"},
            {"name": "Upload", "conclusion": "skipped"},
        ],
    }
    messages = ["", "12 lines the base ran at deadbeef1234 are no longer run by any head test"]
    found = promotion_watch.cause("/repo", job, smoke_run("x", messages))
    assert found == (
        "coverage-ratchet: Hold every line the base ran; "
        "12 lines the base ran at deadbeef1234 are no longer run by any head test"
    )


@pytest.mark.parametrize(
    ("detail", "kept"),
    [
        ("12 lines at deadbeef1234 ran", "# lines at # ran"),
        ("took 12.34s at 05:06:51Z", "took # at #"),
        ("Build e2e tests", "Build e# tests"),
        ("Build b2b tests", "Build b# tests"),
        ("in the decade", "in the decade"),
    ],
)
def test_normal_folds_numbers_and_hashes_in_the_cause_but_keeps_the_job_name(detail, kept):
    job = "unit (3.11, 1)"
    assert promotion_watch.normal(job, f"{job}: {detail}") == f"{job}: {kept}"
    assert promotion_watch.normal(job, f"{job}: ") == f"{job}: "


def test_cause_of_a_job_without_steps_or_check_run_is_its_name_and_an_empty_detail():
    assert promotion_watch.cause("/repo", {"name": "lint", "conclusion": "failure"}, gh([])) == "lint: "


def test_a_fixer_without_cause_detail_does_not_hold_a_later_cause_with_detail(store):
    ledger = Ledger()
    refresh(store, ledger, gh([RED], [{"name": "swarm-image / smoke", "conclusion": "failure"}]))
    refresh(store, ledger, smoke_run("Build the image", [], 901), NOW * 2)
    assert [fields["task"] for _, fields, _ in ledger.added] == ["promo-900-0", "promo-901-0"]


def test_a_second_red_run_with_a_new_cause_on_a_held_job_files_a_new_fixer_an_alert_and_a_master_item(store):
    ledger = Ledger()
    refresh(store, ledger, smoke_run("Build the image", []))
    assert [fields["task"] for _, fields, _ in ledger.added] == ["promo-900-0"]
    ledger.tasks[0]["state"] = "blocked"
    step = "Bring up the local stack twice with swarm init from the installed package"
    actions = refresh(store, ledger, smoke_run(step, ["swarm init refused a one task swarm."], 901), NOW * 2)
    assert [fields["task"] for _, fields, _ in ledger.added] == ["promo-900-0", "promo-901-0"]
    assert ledger.added[1][1]["lane"] == "ci"
    assert ledger.added[1][1]["description"] == (
        f"The dev push run 901 of Tests failed in the job swarm-image / smoke at commit {SHA}: "
        f"https://github.com/o/r/actions/runs/901. Its cause: {step}; swarm init refused a one task swarm. "
        "Fix it on dev; the swarm closes this task when a later dev push run passes."
    )
    replaced = "Dev push run 901 of Tests failed on a new cause, so the swarm raised a new promotion alert."
    assert ledger.closed == [(SLUG, "al-0", replaced)]
    assert len(ledger.raised) == 2
    assert ledger.raised[1][2] == (
        f"Dev promotion is red: push run 901 at commit {SHA} failed in swarm-image / smoke: {step}; "
        "swarm init refused a one task swarm. https://github.com/o/r/actions/runs/901"
    )
    assert actions == [
        "filed promo-901-0 for the failed dev job swarm-image / smoke",
        "raised the promotion alert for dev push run 901",
        "told the master about the new promotion causes of dev push run 901",
    ]
    items = InboxStore(store.redis).inbox(Mail(InboxStore(store.redis), store, SLUG).master)
    assert [(item.text, item.ref) for item in items][-1] == (
        "Dev push run 901 of Tests failed on new causes, so the swarm filed promo-901-0 in the ci lane and raised a "
        "promotion alert: https://github.com/o/r/actions/runs/901",
        "promo-901-0",
    )
    assert len(items) == 2
    assert filed(store) == {
        "promo-900-0": "swarm-image / smoke: Build the image",
        "promo-901-0": f"swarm-image / smoke: {step}; swarm init refused a one task swarm.",
    }


def test_the_same_cause_on_a_held_job_files_nothing_and_stays_quiet_while_its_alert_is_live(store):
    ledger = Ledger()
    refresh(store, ledger, smoke_run("Build the image", ["layer 3 failed"]))
    assert refresh(store, ledger, smoke_run("Build the image", ["layer 4 failed"], 901), NOW * 2) == []
    assert len(ledger.added) == 1 and len(ledger.raised) == 1
    assert len(master_items(store)) == 1


def test_a_fixer_filed_before_causes_were_kept_still_holds_its_job(store):
    store.redis.hset(promotion_watch.key(SLUG, "jobs"), "promo-800-0", "swarm-image / smoke")
    ledger = Ledger([{"id": "promo-800-0", "title": "Promotion red: swarm-image smoke", "state": "claimed"}])
    assert refresh(store, ledger, smoke_run("Build the image", ["layer failed"])) == [
        "raised the promotion alert for dev push run 900"
    ]
    assert ledger.added == [] and master_items(store) == []


def test_a_long_cause_is_cut_so_the_alert_fits_the_ledger_and_keeps_the_run_link(store):
    ledger = Ledger()
    refresh(store, ledger, smoke_run("Build the image", ["x" * 5000]))
    [(_, _, text)] = ledger.raised
    assert len(text) == MAX_ALERT
    assert text.endswith(". https://github.com/o/r/actions/runs/900")


def test_a_fixer_goes_to_the_eng_lane_when_quota_capacity_gives_the_ci_lane_no_seats(store):
    store.redis.set(store.key(SLUG, "quota-capacity"), json.dumps({"effective": {"eng": 3, "ci": 0, "plan": 0}}))
    ledger = Ledger()
    refresh(store, ledger, gh([RED], JOBS))
    assert {fields["lane"] for _, fields, _ in ledger.added} == {"eng"}
    assert {fields["kind"] for _, fields, _ in ledger.added} == {"ci"}


def test_a_fixer_stays_in_the_ci_lane_while_quota_capacity_gives_it_seats(store):
    store.redis.set(store.key(SLUG, "quota-capacity"), json.dumps({"effective": {"eng": 3, "ci": 1, "plan": 0}}))
    ledger = Ledger()
    refresh(store, ledger, gh([RED], JOBS))
    assert {fields["lane"] for _, fields, _ in ledger.added} == {"ci"}


DOWNLOAD = [
    "Failed to resolve action download info.",
    "Resource temporarily unavailable (launch.actions.githubusercontent.com:443)",
]
RAN = [{"name": "Set up job", "conclusion": "success"}, {"name": "Run tests", "conclusion": "success"}]


def outage_run(found, run_id=900):
    jobs = [
        {
            "name": name,
            "conclusion": "failure",
            "check_run_url": f"https://api.github.com/check-runs/{n}",
            "steps": steps,
        }
        for n, (name, steps, _) in enumerate(found)
    ]
    messages = {f"https://api.github.com/check-runs/{n}": m for n, (_, _, m) in enumerate(found)}

    def run(argv, **kwargs):
        if "/annotations" in argv[2]:
            body = [
                {"annotation_level": "failure", "message": m}
                for m in ["Process completed with exit code 1.", *messages[argv[2].split("/annotations")[0]]]
            ]
        elif "/jobs" in argv[2]:
            body = [*jobs, {"name": "Gate — Required", "conclusion": "failure"}]
        else:
            body = [{**RED, "id": run_id}]
        return subprocess.CompletedProcess(argv, 0, "\n".join(json.dumps(item) for item in body), "")

    return run


def test_a_run_whose_failed_jobs_all_lost_their_runner_files_one_outage_task(store):
    ledger = Ledger()
    found = [(f"unit (3.11, {n})", RAN, DOWNLOAD) for n in range(1, 18)]
    actions = refresh(store, ledger, outage_run(found))
    assert [fields["title"] for _, fields, _ in ledger.added] == ["Promotion red: runner outage"]
    _, fields, _ = ledger.added[0]
    assert (fields["task"], fields["kind"], fields["rank"]) == ("promo-900-0", "ci", "urgent")
    assert "https://github.com/o/r/actions/runs/900" in fields["description"]
    assert all(f"unit (3.11, {n})" in fields["description"] for n in range(1, 18))
    assert "Failed to resolve action download info" in fields["description"]
    assert len(ledger.raised) == 1
    assert "runner outage" in ledger.raised[0][2]
    assert actions[0] == "filed promo-900-0 for the failed dev job runner outage"


@pytest.mark.parametrize(
    ("steps", "messages"),
    [
        ([{"name": "Set up job", "conclusion": "failure"}], []),
        (RAN, ["Failed to download archive after 3 attempts."]),
        ([*RAN[:1], {"name": "Run tests", "conclusion": None}], []),
        (RAN, ["The self-hosted runner: anton-x lost communication with the server."]),
        (RAN, ["The operation was canceled."]),
    ],
)
def test_each_runner_infrastructure_signature_is_an_outage(store, steps, messages):
    ledger = Ledger()
    refresh(store, ledger, outage_run([("unit (3.12, 1)", steps, messages), ("unit (3.12, 2)", RAN, DOWNLOAD)]))
    assert [fields["title"] for _, fields, _ in ledger.added] == ["Promotion red: runner outage"]


def test_a_failed_step_that_skipped_the_tests_files_its_own_fixer_and_no_outage(store):
    ledger = Ledger()
    steps = [
        *RAN[:1],
        {"name": "Install dependencies", "conclusion": "failure"},
        {"name": "Run tests", "conclusion": "skipped"},
    ]
    refresh(store, ledger, outage_run([("unit (3.12, 1)", steps, ["No matching distribution found for fakepkg"])]))
    assert [fields["title"] for _, fields, _ in ledger.added] == ["Promotion red: unit (3.12, 1)"]


def test_a_job_whose_tests_passed_without_an_outage_sign_files_its_own_fixer(store):
    ledger = Ledger()
    refresh(store, ledger, outage_run([("unit (3.12, 1)", RAN, ["Coverage fell below the floor"])]))
    assert [fields["title"] for _, fields, _ in ledger.added] == ["Promotion red: unit (3.12, 1)"]


def test_the_outage_task_names_the_run_the_jobs_and_every_cause(store):
    ledger = Ledger()
    found = [("unit (3.11, 1)", RAN, DOWNLOAD), ("unit (3.11, 2)", RAN, ["The operation was canceled."])]
    refresh(store, ledger, outage_run(found))
    assert ledger.added[0][1]["description"] == (
        f"The dev push run 900 of Tests lost its runner infrastructure at commit {SHA}: "
        "https://github.com/o/r/actions/runs/900. Its cause: the jobs unit (3.11, 1), unit (3.11, 2) lost their "
        "runner infrastructure (Failed to resolve action download info; The operation was canceled). "
        "No code change is due; the swarm closes this task when a later dev push run passes."
    )


def test_a_real_failure_beside_an_outage_files_the_outage_task_and_its_own_fixer(store):
    ledger = Ledger()
    found = [
        ("unit (3.11, 1)", RAN, DOWNLOAD),
        ("unit (3.11, 2)", RAN, DOWNLOAD),
        ("unit (3.12, 3)", [*RAN[:1], {"name": "Run tests", "conclusion": "failure"}], ["1 failed, 900 passed"]),
    ]
    refresh(store, ledger, outage_run(found))
    assert [fields["title"] for _, fields, _ in ledger.added] == [
        "Promotion red: runner outage",
        "Promotion red: unit (3.12, 3)",
    ]
    assert len(ledger.raised) == 1


def test_an_open_outage_task_holds_a_later_outage_and_a_green_run_closes_it(store):
    ledger = Ledger()
    refresh(store, ledger, outage_run([("unit (3.11, 1)", RAN, DOWNLOAD)]))
    refresh(store, ledger, outage_run([("unit (3.12, 4)", RAN, ["The operation was canceled."])], 902), NOW * 2)
    assert [fields["task"] for _, fields, _ in ledger.added] == ["promo-900-0"]
    assert len(ledger.raised) == 1
    refresh(store, ledger, gh([{**GREEN, "id": 903}]), NOW * 3)
    assert ledger.tasks[0]["state"] == "done"


UPLOADED = [*RAN, {"name": "Upload durations", "conclusion": "failure"}]
LOST_SHARDS = [
    "unit (3.11, 1)",
    "unit (3.11, 4)",
    "unit (3.11, 5)",
    "unit (3.12, 4)",
    "unit (3.12, 9)",
]
REFRESHED = [{"name": "Refresh measured durations", "conclusion": "failure"}]
CONSUMERS = [
    (
        "sonar",
        [
            {"name": "Wait for the coverage merge", "conclusion": "failure"},
            {"name": "Hold the Delivery L2 conditions", "conclusion": "failure"},
        ],
        ["Process completed with exit code 2.", "Missing coverage for shard 1"],
    ),
    (
        "shard-check (3.12)",
        [{"name": "Check every collected test ran in exactly one shard", "conclusion": "failure"}],
        ["27833 collected tests did not run in exactly one of 3 shards."],
    ),
    (
        "shard-check (3.11)",
        [
            {"name": "Check every collected test ran in exactly one shard", "conclusion": "failure"},
            {"name": "Summarize reused, ran and flaky tests", "conclusion": "failure"},
        ],
        [
            "Process completed with exit code 2.",
            "No shard durations at shard-durations/*/durations.json, so those shards cannot be graded.",
        ],
    ),
    (
        "refresh-durations",
        REFRESHED,
        [
            "refresh durations: 3395 of 27833 tests have no stored duration; run 900 kept no samples from "
            "durations-3.11-2, durations-3.11-6"
        ],
    ),
]
FAILED_TESTS = [*RAN[:1], {"name": "Run tests", "conclusion": "failure"}]


def recorded_outage(extra=()):
    shards = [(name, UPLOADED, DOWNLOAD) for name in LOST_SHARDS]
    return [*shards, ("unit (3.12, 10)", FAILED_TESTS, []), *CONSUMERS, *extra]


def test_jobs_that_failed_for_want_of_lost_unit_shard_output_join_the_outage_task(store):
    ledger = Ledger()
    refresh(store, ledger, outage_run(recorded_outage()))
    assert [fields["title"] for _, fields, _ in ledger.added] == [
        "Promotion red: runner outage",
        "Promotion red: unit (3.12, 10)",
    ]
    description = ledger.added[0][1]["description"]
    assert f"the jobs {', '.join(LOST_SHARDS)} lost their runner infrastructure" in description
    assert (
        "the jobs refresh-durations, shard-check (3.11), shard-check (3.12), sonar failed for want of their unit shard "
        "output" in description
    )
    assert "unit (3.12, 10)" not in description
    assert len(ledger.raised) == 1


def test_a_job_outside_the_shard_output_consumers_beside_an_outage_files_its_own_fixer(store):
    ledger = Ledger()
    lint = ("lint", [{"name": "Run ruff", "conclusion": "failure"}], ["F401 unused import"])
    refresh(store, ledger, outage_run(recorded_outage([lint])))
    assert [fields["title"] for _, fields, _ in ledger.added] == [
        "Promotion red: runner outage",
        "Promotion red: lint",
        "Promotion red: unit (3.12, 10)",
    ]
    assert "lint" not in ledger.added[0][1]["description"]


def test_a_shard_output_consumer_files_its_own_fixer_when_no_unit_shard_lost_its_runner(store):
    ledger = Ledger()
    found = [("docs", RAN, DOWNLOAD), ("unit (3.12, 10)", FAILED_TESTS, []), *CONSUMERS[:1]]
    refresh(store, ledger, outage_run(found))
    assert [fields["title"] for _, fields, _ in ledger.added] == [
        "Promotion red: runner outage",
        "Promotion red: sonar",
        "Promotion red: unit (3.12, 10)",
    ]


@pytest.mark.parametrize(
    ("consumer", "messages"),
    [
        (
            "coverage-ratchet",
            [
                "The coverage ratchet cannot grade: head shard coverage missing: .coverage-shards/coverage-3.12-1/.coverage"
            ],
        ),
        (
            "coverage-baseline",
            ["Cannot record the coverage baseline: baseline shard coverage missing: coverage-3.12-1/.coverage"],
        ),
        (
            "shard-check (3.11)",
            ["No shard durations at shard-durations/1/durations.json, so those shards cannot be graded."],
        ),
        ("sonar (3.12)", ["Missing coverage for shard 1"]),
        ("flake-budget", ["No flake reports under /home/runner/flake-reports"]),
    ],
)
def test_each_shard_output_consumer_joins_the_outage_of_a_lost_unit_shard(store, consumer, messages):
    ledger = Ledger()
    found = [("unit (3.12, 1)", RAN, DOWNLOAD), (consumer, [{"name": "Grade", "conclusion": "failure"}], messages)]
    refresh(store, ledger, outage_run(found))
    assert [fields["title"] for _, fields, _ in ledger.added] == ["Promotion red: runner outage"]
    assert ledger.added[0][1]["description"].endswith(
        f"lost their runner infrastructure (Failed to resolve action download info); the jobs {consumer} failed for "
        "want of their unit shard output. No code change is due; the swarm closes this task when a later dev push run "
        "passes."
    )


@pytest.mark.parametrize(
    ("consumer", "steps", "messages"),
    [
        (
            "sonar",
            [{"name": "Hold the Delivery L2 conditions", "conclusion": "failure"}],
            ["Quality gate failed: 1 new bug on new code"],
        ),
        (
            "shard-check (3.12)",
            [{"name": "Check every collected test ran in exactly one shard", "conclusion": "failure"}],
            ["Shards split on different stored durations, so their test sets overlap or leave gaps: a: x, b: y"],
        ),
        ("stage-budget", [{"name": "Grade", "conclusion": "failure"}], []),
        (
            "stage-budget",
            [{"name": "Grade", "conclusion": "failure"}],
            ["No shard durations at shard-durations/1/durations.json, so those shards cannot be graded."],
        ),
    ],
)
def test_a_consumer_failing_on_its_own_error_beside_a_lost_unit_shard_files_its_own_fixer(
    store, consumer, steps, messages
):
    ledger = Ledger()
    refresh(store, ledger, outage_run([("unit (3.12, 1)", RAN, DOWNLOAD), (consumer, steps, messages)]))
    assert [fields["title"] for _, fields, _ in ledger.added] == [
        "Promotion red: runner outage",
        f"Promotion red: {consumer}",
    ]
    assert "unit shard output" not in ledger.added[0][1]["description"]


def test_a_job_that_only_starts_like_a_unit_shard_does_not_starve_its_consumers(store):
    ledger = Ledger()
    found = [("unit-selected (3.12)", RAN, DOWNLOAD), *CONSUMERS[:1]]
    refresh(store, ledger, outage_run(found))
    assert [fields["title"] for _, fields, _ in ledger.added] == [
        "Promotion red: runner outage",
        "Promotion red: sonar",
    ]


def test_a_consumer_that_lost_its_own_runner_is_named_once_among_the_lost_jobs(store):
    ledger = Ledger()
    refresh(store, ledger, outage_run([("sonar", RAN, DOWNLOAD), ("unit (3.12, 1)", RAN, DOWNLOAD)]))
    assert [fields["title"] for _, fields, _ in ledger.added] == ["Promotion red: runner outage"]
    description = ledger.added[0][1]["description"]
    assert "the jobs sonar, unit (3.12, 1) lost their runner infrastructure" in description
    assert "unit shard output" not in description


@pytest.mark.parametrize(("lost", "failed"), [("unit smoke (3.12)", "sonar"), ("unit (3.12, 1)", "sonar report")])
def test_only_the_job_name_before_its_matrix_marks_a_unit_shard_or_a_consumer(store, lost, failed):
    ledger = Ledger()
    refresh(
        store, ledger, outage_run([(lost, RAN, DOWNLOAD), (failed, [{"name": "Grade", "conclusion": "failure"}], [])])
    )
    assert [fields["title"] for _, fields, _ in ledger.added] == [
        "Promotion red: runner outage",
        f"Promotion red: {failed}",
    ]


def test_a_refresh_naming_lost_shards_joins_an_outage_no_unit_shard_reported(store):
    ledger = Ledger()
    refresh(store, ledger, outage_run([("worker-image / worker-image (startup)", RAN, DOWNLOAD), CONSUMERS[-1]]))
    assert [fields["title"] for _, fields, _ in ledger.added] == ["Promotion red: runner outage"]
    assert "the jobs refresh-durations failed for want of their unit shard output" in ledger.added[0][1]["description"]


def test_a_refresh_that_failed_on_its_own_cause_beside_a_lost_unit_shard_files_its_own_fixer(store):
    ledger = Ledger()
    own = ("refresh-durations", REFRESHED, ["refresh durations: malformed sample file durations.json: Expecting value"])
    refresh(store, ledger, outage_run([("unit (3.12, 1)", RAN, DOWNLOAD), own]))
    assert [fields["title"] for _, fields, _ in ledger.added] == [
        "Promotion red: runner outage",
        "Promotion red: refresh-durations",
    ]
    assert "refresh-durations" not in ledger.added[0][1]["description"]


def test_a_refresh_naming_lost_shards_files_its_own_fixer_when_nothing_lost_its_runner(store):
    ledger = Ledger()
    refresh(store, ledger, outage_run([CONSUMERS[-1]]))
    assert [fields["title"] for _, fields, _ in ledger.added] == ["Promotion red: refresh-durations"]


def recorded_test_failure(
    run_id,
    shards,
    tests=("tests/test_ci_pull_request_api.py::test_no_script_a_step_runs_calls_the_api",),
    missing=True,
    messages=None,
    steps=None,
    sonar="sonar",
):
    found = [
        {
            "id": n,
            "name": f"unit ({python}, {shard})",
            "conclusion": "failure",
            "steps": FAILED_TESTS,
        }
        for n, (python, shard) in enumerate(shards, 1)
    ]
    if missing:
        found.append(
            {
                "name": sonar,
                "conclusion": "failure",
                "check_run_url": "https://api.github.com/check-runs/sonar",
                "steps": steps
                if steps is not None
                else [{"name": "Wait for the coverage merge", "conclusion": "failure"}],
            }
        )

    def run(argv, **kwargs):
        if argv[2].endswith("/logs"):
            job_id = argv[2].split("/jobs/")[1].split("/")[0]
            assert argv == ["gh", "api", f"repos/{{owner}}/{{repo}}/actions/jobs/{job_id}/logs"]
            assert kwargs == {"cwd": "/repo", "capture_output": True, "text": True, "check": True, "timeout": 60}
            output = "\n".join(f"2026-10-20T12:47:13Z FAILED {test} - AssertionError: failure" for test in tests)
            return subprocess.CompletedProcess(argv, 0, output, "")
        if "/annotations" in argv[2]:
            body = [
                {"annotation_level": "failure", "message": message}
                for message in (messages if messages is not None else [f"Missing coverage for shard {shards[-1][1]}"])
            ]
        elif "/jobs" in argv[2]:
            body = found
        else:
            body = [{**RED, "id": run_id}]
        return subprocess.CompletedProcess(argv, 0, "\n".join(json.dumps(item) for item in body), "")

    return run


def test_the_same_failing_test_in_two_recorded_runs_files_one_fixer_across_shard_moves(store):
    ledger = Ledger()
    refresh(store, ledger, recorded_test_failure(39424239023, [("3.11", 3), ("3.12", 10)]))
    refresh(store, ledger, recorded_test_failure(39425295161, [("3.11", 5), ("3.12", 5)]), NOW * 2)
    assert len(ledger.added) == 1
    description = ledger.added[0][1]["description"]
    assert "tests/test_ci_pull_request_api.py::test_no_script_a_step_runs_calls_the_api" in description
    assert "sonar" in description
    assert "unit (3.11, 3)" in description
    assert "unit (3.12, 10)" in description
    assert (
        "failed in unit (3.11, 3), unit (3.12, 10); consequences: sonar: Wait for the coverage merge; Missing coverage for shard 10."
        in description
    )
    assert len(ledger.raised) == 1
    assert len(master_items(store)) == 1


def test_each_distinct_failing_test_keeps_its_own_fixer_across_runs(store):
    ledger = Ledger()
    refresh(
        store,
        ledger,
        recorded_test_failure(900, [("3.12", 1)], tests=("tests/test_api.py::test_rule[1]",), missing=False),
    )
    refresh(
        store,
        ledger,
        recorded_test_failure(901, [("3.12", 2)], tests=("tests/test_api.py::test_rule[2]",), missing=False),
        NOW * 2,
    )
    assert len(ledger.added) == 2
    assert "tests/test_api.py::test_rule[1]" in ledger.added[0][1]["description"]
    assert "tests/test_api.py::test_rule[2]" in ledger.added[1][1]["description"]


def test_two_tests_failing_in_the_same_shards_each_get_one_fixer(store):
    ledger = Ledger()
    refresh(
        store,
        ledger,
        recorded_test_failure(
            900, [("3.11", 1), ("3.12", 2)], tests=("tests/test_api.py::test_first", "tests/test_api.py::test_second")
        ),
    )
    assert len(ledger.added) == 2
    assert all("sonar" in fields["description"] for _, fields, _ in ledger.added)


@pytest.mark.parametrize(
    "messages",
    [
        ["Missing coverage for shard 99"],
        ["Quality gate failed"],
        ["Missing coverage for shard 2", "Quality gate failed"],
    ],
)
def test_a_sonar_failure_not_explained_by_the_named_failing_test_keeps_its_fixer(store, messages):
    ledger = Ledger()
    refresh(store, ledger, recorded_test_failure(900, [("3.12", 2)], messages=messages))
    assert len(ledger.added) == 2
    assert any(fields["title"] == "Promotion red: sonar" for _, fields, _ in ledger.added)


def test_a_failed_unit_job_without_named_tests_keeps_its_job_cause(store):
    ledger = Ledger()
    refresh(store, ledger, recorded_test_failure(900, [("3.12", 2)], tests=()))
    assert {fields["title"] for _, fields, _ in ledger.added} == {
        "Promotion red: sonar",
        "Promotion red: unit (3.12, 2)",
    }


def test_an_unreadable_unit_log_leaves_the_run_unhandled(store):
    ledger = Ledger()
    recorded = recorded_test_failure(900, [("3.12", 2)])

    def run(argv, **kwargs):
        if argv[2].endswith("/logs"):
            raise subprocess.CalledProcessError(1, argv, stderr="log is unavailable")
        return recorded(argv, **kwargs)

    assert refresh(store, ledger, run) == []
    assert ledger.added == []
    assert not store.redis.get(promotion_watch.key(SLUG, "seen"))


def test_different_tests_together_explain_all_missing_sonar_shards(store):
    ledger = Ledger()
    recorded = recorded_test_failure(
        900, [("3.12", 1), ("3.12", 2)], messages=["Missing coverage for shard 1", "Missing coverage for shard 2"]
    )

    def run(argv, **kwargs):
        if argv[2].endswith("/logs"):
            test = "first" if "/jobs/1/" in argv[2] else "second"
            return subprocess.CompletedProcess(argv, 0, f"FAILED tests/test_api.py::test_{test} - AssertionError", "")
        return recorded(argv, **kwargs)

    refresh(store, ledger, run)
    assert len(ledger.added) == 2
    assert all("sonar" in fields["description"] for _, fields, _ in ledger.added)


def test_setup_errors_and_test_failures_keep_distinct_causes_across_shard_moves(store):
    ledger = Ledger()

    def recorded(run_id, shard):
        original = recorded_test_failure(
            run_id,
            [("3.12", shard)],
            tests=("tests/test_api.py::test_setup", "tests/test_api.py::test_body"),
            missing=False,
        )

        def run(argv, **kwargs):
            result = original(argv, **kwargs)
            if argv[2].endswith("/logs"):
                result.stdout = result.stdout.replace(
                    "FAILED tests/test_api.py::test_setup", "ERROR tests/test_api.py::test_setup"
                )
            return result

        return run

    refresh(store, ledger, recorded(900, 1))
    refresh(store, ledger, recorded(901, 2), NOW * 2)
    assert len(ledger.added) == 2
    assert "tests/test_api.py::test_body" in ledger.added[0][1]["description"]
    assert "tests/test_api.py::test_setup" in ledger.added[1][1]["description"]


def test_an_independent_failed_step_beside_a_failing_test_keeps_its_own_fixer(store):
    ledger = Ledger()
    recorded = recorded_test_failure(900, [("3.12", 2)], missing=False)

    def run(argv, **kwargs):
        result = recorded(argv, **kwargs)
        if "/jobs?" in argv[2]:
            jobs = [json.loads(line) for line in result.stdout.splitlines()]
            jobs[0]["steps"] = [*jobs[0]["steps"], {"name": "Upload durations", "conclusion": "failure"}]
            result.stdout = "\n".join(json.dumps(job) for job in jobs)
        return result

    refresh(store, ledger, run)
    assert len(ledger.added) == 2
    assert (
        "tests/test_ci_pull_request_api.py::test_no_script_a_step_runs_calls_the_api"
        in ledger.added[0][1]["description"]
    )
    assert "Upload durations" in ledger.added[1][1]["description"]


def test_a_held_test_cause_ignores_changed_shard_count_and_missing_coverage_consequences(store):
    ledger = Ledger()
    refresh(store, ledger, recorded_test_failure(900, [("3.11", 1), ("3.12", 2)]))
    refresh(store, ledger, recorded_test_failure(901, [("3.12", 5)], missing=False), NOW * 2)
    assert len(ledger.added) == 1


@pytest.mark.parametrize("sonar", ["sonar", "sonar (3.12)"])
def test_both_failed_sonar_coverage_steps_fold_with_exit_annotations(store, sonar):
    ledger = Ledger()
    steps = [
        {"name": "Set up job", "conclusion": "success"},
        {"name": "Wait for the coverage merge", "conclusion": "failure"},
        {"name": "Hold the Delivery L2 conditions", "conclusion": "failure"},
    ]
    messages = ["Process completed with exit code 2.", "Missing coverage for shard 2"]
    refresh(store, ledger, recorded_test_failure(900, [("3.12", 2)], messages=messages, steps=steps, sonar=sonar))
    assert len(ledger.added) == 1
    assert (
        f"; consequences: {sonar}: Wait for the coverage merge; Hold the Delivery L2 conditions; Process completed with exit code 2.; Missing coverage for shard 2."
        in ledger.added[0][1]["description"]
    )


@pytest.mark.parametrize(
    "sonar,steps",
    [
        ("sonar", [{"name": "Upload coverage", "conclusion": "failure"}]),
        ("sonar report", [{"name": "Wait for the coverage merge", "conclusion": "failure"}]),
    ],
)
def test_missing_coverage_does_not_hide_a_different_sonar_step_or_job(store, sonar, steps):
    ledger = Ledger()
    refresh(store, ledger, recorded_test_failure(900, [("3.12", 2)], steps=steps, sonar=sonar))
    assert len(ledger.added) == 2


@pytest.mark.parametrize(
    "name,steps",
    [
        ("unit smoke (3.12, 1)", FAILED_TESTS),
        (
            "unit (3.12, 1)",
            [{"name": "Run tests", "conclusion": "success"}, {"name": "Upload durations", "conclusion": "failure"}],
        ),
    ],
)
def test_only_an_actual_failed_unit_test_step_reads_pytest_failure_logs(store, name, steps):
    ledger = Ledger()
    recorded = recorded_test_failure(900, [("3.12", 1)], missing=False)

    def run(argv, **kwargs):
        assert not argv[2].endswith("/logs")
        result = recorded(argv, **kwargs)
        if "/jobs?" in argv[2]:
            job = json.loads(result.stdout)
            job.update(name=name, steps=steps)
            result.stdout = json.dumps(job)
        return result

    refresh(store, ledger, run)
    assert len(ledger.added) == 1
    assert f"failed in the job {name}" in ledger.added[0][1]["description"]


def test_unit_progress_lines_without_a_summary_still_name_the_failed_test(store):
    ledger = Ledger()
    recorded = recorded_test_failure(900, [("3.12", 1)], missing=False)

    def run(argv, **kwargs):
        if argv[2].endswith("/logs"):
            return subprocess.CompletedProcess(
                argv, 0, "[gw0] FAILED tests/test_api.py::test_rule\nworker exited\n", ""
            )
        return recorded(argv, **kwargs)

    refresh(store, ledger, run)
    assert len(ledger.added) == 1
    assert "unit test tests/test_api.py::test_rule" in ledger.added[0][1]["description"]


def test_multiple_matching_coverage_consumers_are_named_as_consequences(store):
    ledger = Ledger()
    recorded = recorded_test_failure(900, [("3.12", 1)])

    def run(argv, **kwargs):
        result = recorded(argv, **kwargs)
        if "/jobs?" in argv[2]:
            jobs = [json.loads(line) for line in result.stdout.splitlines()]
            jobs.append({**jobs[-1], "name": "sonar (3.12)"})
            result.stdout = "\n".join(json.dumps(job) for job in jobs)
        return result

    refresh(store, ledger, run)
    assert len(ledger.added) == 1
    assert (
        "; consequences: sonar: Wait for the coverage merge; Missing coverage for shard 1; sonar (3.12): Wait for the coverage merge; Missing coverage for shard 1."
        in ledger.added[0][1]["description"]
    )
