import json
import subprocess
from types import SimpleNamespace

import pytest

from scripts.swarm import promotion_watch
from scripts.swarm.ledger_client import LedgerClient, LedgerRefused
from scripts.swarm.store import PREFIX, RedisStore, SwarmConfig

pytestmark = pytest.mark.xdist_group("fakeredis")

RUNS = "repos/{owner}/{repo}/actions/workflows/test.yml/runs?branch=dev&event=push&status=completed&per_page=20"
JOBS_JQ = ".jobs[] | {name, conclusion} | @json"
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
NOT_DUPLICATE = "The promotion watch files one task per failed dev job and run, and holds a job while its task is open."
NOW = 10_000_000
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
        self.raised.append((slug, source, text))
        self.alerts.append({"id": f"al-{len(self.alerts)}", "source": source, "text": text, "state": "open"})

    def close_alert(self, slug, alert_id, outcome):
        self.closed.append((slug, alert_id, outcome))


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
    ]
    assert filed(store) == {"promo-900-0": "helm-kind", "promo-900-1": "unit (3.11, 1)"}
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
    assert ledger.raised == []
    assert actions == ["filed promo-900-1 for the failed dev job unit (3.11, 1)"]
    assert filed(store) == {"promo-800-0": "helm-kind", "promo-900-1": "unit (3.11, 1)"}


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
    ]


def test_a_run_already_handled_is_not_read_for_jobs_again(store):
    store.redis.set(promotion_watch.key(SLUG, "seen"), 900)
    calls, ledger = [], Ledger()
    assert refresh(store, ledger, gh([RED], JOBS, calls)) == []
    assert len(calls) == 1 and ledger.added == [] and ledger.raised == []


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
    assert red[-1] == "raised the promotion alert for dev push run 900"
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
