import json
from types import SimpleNamespace

from scripts.gates import intent, intent_history
from tests.gates.test_intent import NOW, SLUG, WHO, Ledger, Mail, classifier, rows, verdicts

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


def runner(calls, pulls=PULLS):
    def run(args, **kwargs):
        calls.append(args)
        pull = pulls.get(args[3])
        if pull is None:
            return SimpleNamespace(returncode=1, stdout="")
        if args == ["gh", "pr", "view", args[3], "--json", "title,body,files,state"]:
            fields = ("title", "body", "files", "state")
            return SimpleNamespace(returncode=0, stdout=json.dumps({key: pull[key] for key in fields}))
        if args == ["gh", "pr", "diff", args[3]]:
            return SimpleNamespace(returncode=0, stdout=pull["diff"])
        return SimpleNamespace(returncode=1, stdout="")

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


def judged(tmp_path, task, usable=0.9, body=PULLS[HERE]["body"], calls=None, pulls=PULLS, seen=None):
    seen = [] if seen is None else seen
    calls = [] if calls is None else calls
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
        named=lambda url: intent.named_view(url, run=runner(calls, pulls)),
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
    assert pulls[THERE]["title"] == PULLS[THERE]["title"]
    assert pulls[THERE]["body"] == PULLS[THERE]["body"]
    assert record["verdict"] == "pass"
    assert record["reason"].endswith(f"; judged on {HERE} and {THERE}")
    assert "named_pending" not in record


def test_a_failed_verdict_also_names_every_pull_request_judged(tmp_path):
    _, record = judged(tmp_path, task(), usable=0.1)
    assert record["verdict"] == "fail"
    assert f"; judged on {HERE} and {THERE}" in record["reason"]


def test_three_judged_pull_requests_are_listed_with_commas_and_a_final_and(tmp_path):
    bridge = "https://github.com/o/bridge/pull/7"
    pulls = {**PULLS, bridge: PULLS[THERE]}
    _, record = judged(tmp_path, task(description=f"Also {bridge}."), pulls=pulls)
    assert record["reason"].endswith(f"; judged on {HERE}, {bridge} and {THERE}")


def test_a_task_naming_no_other_repository_is_judged_on_its_own_pull_request_alone(tmp_path):
    calls = []
    seen, record = judged(tmp_path, task(description="See o/agentihooks#3."), body="No other repository.", calls=calls)
    assert "pull_requests" not in seen[0][0]
    assert not calls
    assert record["verdict"] == "pass"
    assert record["reason"] == "the phase can use it as delivered at probability 0.90"


def test_an_unreadable_named_pull_request_is_named_and_judged_again_once_it_reads_merged(tmp_path):
    seen = []
    _, record = judged(tmp_path, task(), pulls={HERE: PULLS[HERE]}, seen=seen)
    assert record["reason"].endswith(f"; judged on {HERE}; could not read {THERE}")
    assert record["named_pending"] == [THERE]
    judged(tmp_path, task(), pulls={HERE: PULLS[HERE]}, seen=seen)
    assert len(seen) == 1
    _, record = judged(tmp_path, task(), seen=seen)
    assert len(seen) == 2
    assert record["reason"].endswith(f"; judged on {HERE} and {THERE}")


def test_an_open_named_pull_request_is_judged_again_when_it_merges(tmp_path):
    seen, opened = [], {**PULLS, THERE: {**PULLS[THERE], "state": "OPEN"}}
    _, record = judged(tmp_path, task(), pulls=opened, seen=seen)
    assert record["named_pending"] == [THERE]
    judged(tmp_path, task(), pulls=opened, seen=seen)
    assert len(seen) == 1
    judged(tmp_path, task(), seen=seen)
    assert len(seen) == 2
    assert {p["url"]: p["merged"] for p in seen[1][0]["pull_requests"]}[THERE] is True


def test_named_pull_requests_past_the_limit_are_named_unread():
    others = [f"https://github.com/o/r{n}/pull/1" for n in range(intent.NAMED_LIMIT + 1)]
    pulls = {url: {**PULLS[THERE]} for url in [HERE, *others]}
    check = intent.Check(
        SLUG, "enforce", NOW, None, None, None, None, named=lambda url: intent.named_view(url, run=runner([], pulls))
    )
    pr = check._with_named(task(description=" ".join(others)), {"body": ""})
    assert [p["url"] for p in pr["pull_requests"]] == [HERE, *others[: intent.NAMED_LIMIT]]
    assert pr["unread_pull_requests"] == others[intent.NAMED_LIMIT :]


def test_a_merged_pull_request_past_the_limit_never_rejudges(tmp_path):
    others = [f"https://github.com/o/r{n}/pull/1" for n in range(intent.NAMED_LIMIT + 1)]
    pulls = {**{url: {**PULLS[THERE]} for url in others}, HERE: PULLS[HERE]}
    seen = []
    for _ in range(2):
        _, record = judged(tmp_path, task(description=" ".join(others)), body="", pulls=pulls, seen=seen)
    assert len(seen) == 1
    assert "named_pending" not in record
    assert record["reason"].endswith(f"; could not read {others[-1]}")


def test_coach_mode_rejudges_the_same_head_when_a_named_pull_request_merges(tmp_path):
    seen, opened = [], {**PULLS, THERE: {**PULLS[THERE], "state": "OPEN"}}
    view = {"head": "h1", "title": "t", "body": PULLS[HERE]["body"], "files": ["a"], "draft": False}
    for pulls in (opened, opened, PULLS):
        intent.Check(
            SLUG,
            "coach",
            NOW,
            Ledger(),
            Mail(),
            lambda url: view,
            lambda state: intent.judge(state, classifier(0.9, seen=seen)),
            home=tmp_path,
            head=lambda url: "h1",
            named=lambda url, pulls=pulls: intent.named_view(url, run=runner([], pulls)),
        ).run({**DOC, "tasks": [task()]})
    assert len(seen) == 2
    assert {p["url"]: p["merged"] for p in seen[1][0]["pull_requests"]}[THERE] is True


def test_a_large_named_diff_keeps_its_url_and_merged_state_in_the_classifier_state():
    pull = {
        "url": THERE,
        "merged": True,
        "files": ["x y " * 2047 + "x" * (8189 - 8188)],
        "diff": '"' * intent.DIFF_CHARS,
    }
    [bound] = intent_history.prepare({"pull_requests": [pull]})["pull_requests"]
    assert bound["url"] == THERE and bound["merged"] is True
    assert bound["diff"]["truncated"] is True
    assert bound["files"]["truncated"] is True
    [kept] = intent_history.prepare({"pull_requests": [{**pull, "files": ["x y " * 2047 + "x" * (8188 - 8188)]}]})[
        "pull_requests"
    ]
    assert kept["files"] == ["x y " * 2047 + "x" * (8188 - 8188)]


def test_named_view_marks_a_cut_diff_and_body():
    pulls = {THERE: {**PULLS[THERE], "body": "b" * (intent.BODY_CHARS + 1), "diff": "d" * (intent.DIFF_CHARS + 1)}}
    view = intent.named_view(THERE, run=runner([], pulls))
    assert view["diff_truncated"] and view["body_truncated"]
    assert len(view["diff"]) == intent.DIFF_CHARS and len(view["body"]) == intent.BODY_CHARS
    pulls = {THERE: {**PULLS[THERE], "body": "b" * intent.BODY_CHARS, "diff": "d" * intent.DIFF_CHARS}}
    view = intent.named_view(THERE, run=runner([], pulls))
    assert not view["diff_truncated"] and not view["body_truncated"]
    view = intent.named_view(THERE, run=runner([], {THERE: {**PULLS[THERE], "body": None}}))
    assert view["body"] == "" and view["body_truncated"] is False


def test_named_pulls_reads_task_text_comments_and_the_body_and_skips_this_repository():
    named = intent.named_pulls(
        task(
            description="Gate merged in https://github.com/o/antoncore/pull/580, see o/agentihooks#4.",
            comments=[{"text": "Kernel side is O/Kernel#12, also https://github.com/o/kernel/pull/12."}],
        ),
        "Binding: o/antoncore#580 and https://github.com/o/bridge/pull/7, closes #2, see docs/a/b#3 and scripts/gates#2.",
    )
    assert named == [THERE, "https://github.com/o/bridge/pull/7", "https://github.com/o/kernel/pull/12"]


def many(count):
    return [f"https://github.com/the-cloud-clockwork/repository-{n}/pull/{n}" for n in range(count)]


def test_a_body_naming_over_a_hundred_other_pull_requests_keeps_lists_and_names_a_few(tmp_path):
    others = many(140)
    seen, record = judged(tmp_path, task(), body=" ".join(others), pulls={HERE: PULLS[HERE]})
    [(state, _, _)] = seen
    assert state["judged_pull_requests"] == [HERE]
    assert state["unread_pull_requests"] == others[: intent_history.URL_COUNT]
    assert state["unread_overflow"] == 130
    assert record["verdict"] == "pass"
    assert record["reason"].endswith(f"; judged on {HERE}; could not read {', '.join(others[:4])} and 136 more")


def test_exactly_the_count_of_unread_pull_requests_carries_no_overflow(tmp_path):
    others = many(intent_history.URL_COUNT)
    seen, record = judged(tmp_path, task(), body=" ".join(others), pulls={HERE: PULLS[HERE]})
    [(state, _, _)] = seen
    assert state["unread_pull_requests"] == others
    assert "unread_overflow" not in state
    assert record["reason"].endswith(f"; could not read {', '.join(others[:4])} and 6 more")


def test_the_reason_names_four_pull_requests_in_full_and_counts_a_fifth():
    four, five = many(4), many(5)
    assert intent._judged_on({"judged_pull_requests": four}) == f"; judged on {', '.join(four[:3])} and {four[3]}"
    assert intent._judged_on({"unread_pull_requests": five}) == f"; could not read {', '.join(five[:4])} and 1 more"
    unread = {"unread_pull_requests": four, "unread_overflow": 2}
    assert intent._judged_on(unread) == f"; could not read {', '.join(four)} and 2 more"


def test_prepare_keeps_long_url_lists_as_lists_capped_by_count():
    urls = [f"https://github.com/o/repository-with-a-long-name/pull/{n}" for n in range(400)]
    state = intent_history.prepare({"judged_pull_requests": urls, "unread_pull_requests": urls, "other": urls})
    assert state["judged_pull_requests"] == urls[:10]
    assert state["unread_pull_requests"] == urls[:10]
    assert state["other"]["truncated"] is True


def test_named_pulls_skip_links_inside_the_generated_parent_intent_section():
    inside = f"{intent.START}\n## Parent intent\n\nSee {THERE} and o/kernel#3.\n{intent.END}"
    named = intent.named_pulls(task(), f"Binding https://github.com/o/bridge/pull/7.\n\n{inside}")
    assert named == ["https://github.com/o/bridge/pull/7"]
    assert intent.named_pulls(task(), f"Binding o/bridge{inside}#7.") == ["https://github.com/o/bridge/pull/7"]
    assert intent.named_pulls(task(description=f"Gate {THERE}."), inside) == [THERE]


def test_one_task_failing_in_the_intent_check_never_ends_the_tick_run(tmp_path):
    broken = {**task(), "id": "t0", "description": "Broken."}

    def ask(state):
        if state["task_text"] == "Broken.":
            raise RuntimeError("boom")
        return intent.judge(state, classifier(0.9))

    view = {"title": PULLS[HERE]["title"], "body": "", "files": [".github/workflows/sonar.yml"]}
    actions = intent.Check(SLUG, "enforce", NOW, Ledger(), Mail(), lambda url: view, ask, home=tmp_path).run(
        {**DOC, "tasks": [broken, task()]}
    )
    assert verdicts(tmp_path).read(WHO.task)["verdict"] == "pass"
    assert actions == ["task t0 intent check failed: RuntimeError", f"task {WHO.task} intent check pass"]
    [row] = [row for row in rows(tmp_path) if row["kind"] == "fail-open"]
    assert row["gate"] == intent.NAME and row["task"] == "t0" and row["agent"] == ""
    assert row["reason"] == "RuntimeError: boom"


def test_one_task_failing_before_it_is_judged_never_ends_the_tick_run(tmp_path, monkeypatch):
    bind = intent._bind_pulls

    def bound(verdicts, task, url, doc):
        if task["id"] == "t0":
            raise OSError("disk")
        return bind(verdicts, task, url, doc)

    monkeypatch.setattr(intent, "_bind_pulls", bound)
    view = {"title": PULLS[HERE]["title"], "body": "", "files": [".github/workflows/sonar.yml"]}

    def ask(state):
        return intent.judge(state, classifier(0.9))

    actions = intent.Check(SLUG, "enforce", NOW, Ledger(), Mail(), lambda url: view, ask, home=tmp_path).run(
        {**DOC, "tasks": [{**task(), "id": "t0", "claimed_by": "eng"}, task()]}
    )
    assert actions == ["task t0 intent check failed: OSError", f"task {WHO.task} intent check pass"]
    assert verdicts(tmp_path).read("t0") is None
    [row] = [row for row in rows(tmp_path) if row["kind"] == "fail-open"]
    assert row["task"] == "t0" and row["agent"] == "eng" and row["reason"] == "OSError: disk"
