import json
from types import SimpleNamespace

from scripts.gates import intent
from tests.gates.test_intent import NOW, SLUG, WHO, Ledger, Mail, classifier, verdicts

HERE = "https://github.com/o/agentihooks/pull/9"
THERE = "https://github.com/o/antoncore/pull/580"
DOC = {
    "overview": "Gates in code.",
    "phases": [{"id": "p8", "title": "Gates", "description": "Hold the Sonar gate."}],
}
PULLS = {
    HERE: {
        "title": "Hold agentihooks on the gate",
        "body": "Gate and binding: o/antoncore#580 (merged).",
        "files": [{"path": ".github/workflows/sonar.yml"}],
        "state": "OPEN",
        "diff": "+      - name: Hold the gate conditions",
    },
    THERE: {
        "title": "Add the Delivery gate",
        "body": "Seeds the gate.",
        "files": [{"path": "stacks/sonarqube/gates.json"}],
        "state": "MERGED",
        "diff": '+  "name": "Delivery L2",',
    },
}


def runner(calls):
    def run(args, **kwargs):
        calls.append(args)
        url = args[3]
        pull = PULLS[url]
        if args[1:3] == ["pr", "view"]:
            fields = ("title", "body", "files", "state")
            return SimpleNamespace(returncode=0, stdout=json.dumps({key: pull[key] for key in fields}))
        return SimpleNamespace(returncode=0, stdout=pull["diff"])

    return run


def task(**fields):
    return {
        "id": WHO.task,
        "phase": "p8",
        "state": "pr",
        "title": "Delivery gate made required",
        "description": "One pull request in antoncore, one here.",
        "pr_url": HERE,
        **fields,
    }


def judged(tmp_path, task, usable=0.9, body=PULLS[HERE]["body"], calls=None):
    seen, calls = [], [] if calls is None else calls
    view = {"title": PULLS[HERE]["title"], "body": body, "files": [".github/workflows/sonar.yml"]}
    intent.Check(
        SLUG,
        "enforce",
        NOW,
        Ledger(),
        Mail(),
        lambda url: view,
        lambda state: intent.judge(state, classifier(usable, seen=seen)),
        home=tmp_path,
        named=lambda url: intent.named_view(url, run=runner(calls)),
    ).run({**DOC, "tasks": [task]})
    return seen, verdicts(tmp_path).read(WHO.task)


def test_a_merged_pull_request_in_a_second_repository_is_judged_with_the_open_one_here(tmp_path):
    seen, record = judged(tmp_path, task())
    [(state, _, _)] = seen
    pulls = {pull["url"]: pull for pull in state["pull_requests"]}
    assert set(pulls) == {HERE, THERE}
    assert pulls[HERE]["diff"] == PULLS[HERE]["diff"]
    assert pulls[THERE]["diff"] == PULLS[THERE]["diff"]
    assert pulls[THERE]["merged"] is True
    assert pulls[HERE]["merged"] is False
    assert pulls[THERE]["files"] == ["stacks/sonarqube/gates.json"]
    assert record["verdict"] == "pass"
    assert HERE in record["reason"] and THERE in record["reason"]


def test_a_failed_verdict_also_names_every_pull_request_judged(tmp_path):
    _, record = judged(tmp_path, task(), usable=0.1)
    assert record["verdict"] == "fail"
    assert HERE in record["reason"] and THERE in record["reason"]


def test_a_task_naming_no_other_repository_is_judged_on_its_own_pull_request_alone(tmp_path):
    calls = []
    seen, record = judged(tmp_path, task(description="See o/agentihooks#3."), body="No other repository.", calls=calls)
    assert "pull_requests" not in seen[0][0]
    assert not calls
    assert record["verdict"] == "pass"
    assert record["reason"] == "the phase can use it as delivered at probability 0.90"


def test_named_pulls_reads_task_text_comments_and_the_body_and_skips_this_repository():
    named = intent.named_pulls(
        task(
            description="Gate merged in https://github.com/o/antoncore/pull/580, see o/agentihooks#4.",
            comments=[{"text": "Kernel side is O/Kernel#12."}],
        ),
        "Binding: o/antoncore#580 and https://github.com/o/bridge/pull/7, closes #2.",
    )
    assert named == [THERE, "https://github.com/o/bridge/pull/7", "https://github.com/O/Kernel/pull/12"]
