import json

import pytest

from scripts import ci_watcher_drops as drops

pytestmark = pytest.mark.unit
REPO = "org/repo"
REBUILT = (
    "queue group is no longer current (record queue-rebuild-9-1: removal of x ahead by bot (FAILED_CHECKS)) failed, "
    "so Gate Required cannot pass; cancelling this queue run."
)


def job(id, name, conclusion):
    return {"id": id, "name": name, "conclusion": conclusion}


def watcher_only(watcher_id, conclusion="cancelled"):
    return [
        job(1, "unit (3.12, 1)", "success"),
        job(2, "mutation", "skipped"),
        job(watcher_id, drops.WATCHER, conclusion),
        job(4, drops.GATE, "failure"),
    ]


class Api:
    def __init__(self, runs, jobs, notes):
        self.runs, self.jobs, self.notes = runs, jobs, notes
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        path = args[1] if args[0] == "--paginate" else args[0]
        if "/actions/workflows/test.yml/runs?" in path:
            return "".join(f"{run}\n" for run in self.runs)
        if "/actions/runs/" in path:
            run = int(path.split("/")[5])
            return "".join(json.dumps(item) + "\n" for item in self.jobs[run])
        job_id = int(path.split("/")[4])
        return json.dumps([{"message": text} for text in self.notes.get(job_id, [])])


def test_a_run_whose_only_red_jobs_are_the_watcher_and_the_gate_is_a_drop():
    assert drops.classify(watcher_only(3), lambda job_id: []) == "drop"


def test_a_run_where_the_watcher_reported_a_rebuild_is_not_a_drop():
    seen = []
    assert drops.classify(watcher_only(3), lambda job_id: seen.append(job_id) or [REBUILT]) == "rebuild"
    assert seen == [3]


@pytest.mark.parametrize(
    "jobs",
    [
        [job(3, drops.WATCHER, "cancelled")],
        [job(4, drops.GATE, "failure")],
        [*watcher_only(3), job(5, "unit (3.12, 3)", "failure")],
        [job(1, "unit (3.12, 1)", "failure"), job(4, drops.GATE, "failure")],
    ],
)
def test_runs_with_other_red_jobs_or_without_both_are_not_counted(jobs):
    assert drops.classify(jobs, lambda job_id: pytest.fail("notes read")) is None


def test_a_job_with_no_conclusion_counts_as_red():
    assert drops.classify([*watcher_only(3), job(6, "lint", None)], lambda job_id: []) is None


@pytest.mark.parametrize("messages,expected", [([], "drop"), (["queue group is no longer current"], "rebuild")])
def test_the_early_required_context_and_late_summary_preserve_watcher_drop_classification(messages, expected):
    jobs = [
        job(11, "Gate — Required", "failure"),
        job(12, "Queue gate summary", "cancelled"),
        job(13, "unit (3.12, 1)", "success"),
    ]
    seen = []
    assert drops.classify(jobs, lambda job_id: seen.append(job_id) or messages) == expected
    assert seen == [11]
    assert drops.classify(jobs[:1] + jobs[2:], lambda job_id: []) is None
    jobs[-1]["conclusion"] = "failure"
    assert drops.classify(jobs, lambda job_id: []) is None


def test_count_reads_each_failed_merge_group_run_of_the_day_and_splits_drops_from_rebuilds():
    api = Api(
        runs=[11, 12, 13],
        jobs={11: watcher_only(31), 12: watcher_only(32), 13: [job(33, "lint", "failure")]},
        notes={31: ["The operation was canceled."], 32: ["first note", REBUILT]},
    )
    assert drops.count(REPO, "2026-10-19", api) == {"day": "2026-10-19", "drops": [11], "rebuilds": [12]}
    assert api.calls[0] == (
        "--paginate",
        f"repos/{REPO}/actions/workflows/test.yml/runs?event=merge_group&created=2026-10-19&per_page=100",
        "--jq",
        drops.FAILED_RUNS,
    )
    assert api.calls[1] == ("--paginate", f"repos/{REPO}/actions/runs/11/jobs?per_page=100", "--jq", ".jobs[]")
    assert api.calls[2] == (f"repos/{REPO}/check-runs/31/annotations",)
    assert len(api.calls) == 6


def test_the_failed_runs_filter_keeps_every_run_that_did_not_succeed():
    assert drops.FAILED_RUNS == '.workflow_runs[] | select(.conclusion != "success") | .id'


def test_main_prints_the_count_as_json(monkeypatch, capsys):
    monkeypatch.setattr(drops, "_api", Api(runs=[11], jobs={11: watcher_only(31)}, notes={}))
    assert drops.main(["2026-10-19"]) == 0
    assert json.loads(capsys.readouterr().out) == {"day": "2026-10-19", "drops": [11], "rebuilds": []}


def test_main_defaults_to_this_repository(monkeypatch, capsys):
    api = Api(runs=[], jobs={}, notes={})
    monkeypatch.setattr(drops, "_api", api)
    assert drops.main(["2026-10-19"]) == 0
    assert api.calls[0][1].startswith("repos/the-cloud-clockwork/agentihooks/actions/workflows/test.yml/runs?")
