import subprocess

import pytest

import hooks.context.swarm_heartbeat as swarm_heartbeat
from scripts.inbox.store import InboxStore
from scripts.swarm import idle, pr_wait, waits
from scripts.swarm.ledger_events import PullRequest
from scripts.swarm.store import AgentRecord, RedisStore, SwarmError

ME = "engineer@a1b2c3-0001"
URL = "https://github.com/o/r/pull/7"
ENV = {"AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_AGENT_NAME": ME, "AGENTIHOOKS_SWARM_TASK": "t1"}
OPEN = PullRequest("OPEN", None, 1, False, head="first")
QUEUED = PullRequest("OPEN", None, 1, False, head="first", queued=True)
MERGED = PullRequest("MERGED", 2, 1, False, head="first")


@pytest.fixture
def store():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.put_agent("sw", AgentRecord(name=ME, lane="eng", task="t1", seat="eng-1@sw"))
    return store


def shell(command, cwd="/"):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}


@pytest.mark.parametrize(
    "command",
    [
        "git push -u origin HEAD",
        "cd /w && git -C /w push",
        "git -c http.postBuffer=1048576 push",
        "git --no-pager push origin HEAD",
        "gh pr create --base dev --fill",
        "gh pr ready 7",
        "agentihooks swarm sw publish --message m -- a.py",
        "agentihooks swarm sw merge queue https://github.com/o/r/pull/7",
    ],
)
def test_a_push_or_queue_command_triggers_the_wait(command):
    assert pr_wait.triggered(command)


@pytest.mark.parametrize(
    "command",
    ["", "git status", "echo git push", "git push --dry-run", "agentihooks swarm sw merge state x", "gh pr view 7"],
)
def test_other_commands_do_not_trigger(command):
    assert not pr_wait.triggered(command)


def test_the_hook_marks_a_pinned_worker_push_with_its_repository_and_branch(tmp_path, store):
    subprocess.run(["git", "init", "-q", "-b", "feature", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", "git@github.com:O/R.git"], check=True)
    assert swarm_heartbeat.push_mark(shell("git push -u origin HEAD", str(tmp_path)), ENV, store.redis, 77) is True
    assert pr_wait.read(store.redis, "sw", ME) == {"at": "77", "repo": "o/r", "branch": "feature"}


@pytest.mark.parametrize(
    "payload, env",
    [
        (shell("git status"), ENV),
        ({"tool_name": "Read", "tool_input": {"command": "git push"}}, ENV),
        (shell("git push"), {k: v for k, v in ENV.items() if k != "AGENTIHOOKS_SWARM_TASK"}),
        (shell("git push"), {k: v for k, v in ENV.items() if k != "AGENTIHOOKS_AGENT_NAME"}),
    ],
)
def test_other_calls_and_unpinned_sessions_mark_nothing(payload, env, store):
    assert swarm_heartbeat.push_mark(payload, env, store.redis, 77) is False
    assert pr_wait.read(store.redis, "sw", ME) is None


def test_a_normalized_codex_push_marks_through_the_post_tool_hook(monkeypatch, store):
    from hooks import hook_manager
    from hooks.targets.normalizer import normalize_payload

    monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("scripts.swarm.store.redis_client", lambda env=None: store.redis)
    raw = {"hook_event_name": "PostToolUse", "session_id": "s1", "tool_response": "ok"}
    hook_manager._swarm_outcome(normalize_payload({**raw, **shell("cd /w\ngit push -u origin HEAD")}))
    assert pr_wait.read(store.redis, "sw", ME)["at"]


def test_a_push_records_both_the_progress_outcome_and_the_mark(store):
    from scripts.gates.progress import Progress

    assert swarm_heartbeat.outcome(shell("git push"), ENV, store.redis, 77) is True
    assert Progress(store.redis, "sw").read(ME).outcome == "pushed"
    assert pr_wait.read(store.redis, "sw", ME)["at"] == "77"


def enforce(store, rows, pulls, now=5_000, forge=None):
    return pr_wait.enforce_pass(store, "sw", rows, pulls.get, now, forge or (lambda repo, branch: ""))


@pytest.mark.parametrize("pull, kind", [(OPEN, "checks"), (QUEUED, "merge")])
def test_the_tick_sets_a_checked_wait_on_the_pushed_task_pull_request(store, pull, kind):
    pr_wait.mark(store.redis, "sw", ME, 100)
    assert enforce(store, {"t1": {"id": "t1", "pr_url": URL}}, {URL: pull}) == [
        f"set the wait of {ME} on {kind} of {URL} from its push"
    ]
    held = idle.wait(store.redis, "sw", ME)
    assert held["on"] == {"kind": kind, "target": URL, **({"head": "first"} if kind == "checks" else {})}
    assert held["until"] == 5_000 + waits.CHECKED_MINUTES * 60_000
    assert pr_wait.read(store.redis, "sw", ME) is None
    assert enforce(store, {"t1": {"id": "t1", "pr_url": URL}}, {URL: pull}) == []


def test_the_tick_finds_the_pull_request_on_the_forge_and_throttles_the_lookup(store):
    pr_wait.mark(store.redis, "sw", ME, 100, "o/r", "pushed")
    asked = []

    def forge(repo, branch):
        asked.append((repo, branch))
        return ""

    rows = {"t1": {"id": "t1", "branch": "task", "branch_repo": "https://github.com/o/r"}}
    assert enforce(store, rows, {}, 5_000, forge) == []
    assert enforce(store, rows, {}, 5_000 + pr_wait.LOOKUP_MS - 1, forge) == []
    assert asked == [("https://github.com/o/r", "task")]
    assert enforce(store, rows, {URL: OPEN}, 5_000 + pr_wait.LOOKUP_MS, lambda repo, branch: URL) == [
        f"set the wait of {ME} on checks of {URL} from its push"
    ]


def test_the_tick_falls_back_to_the_pushed_branch_when_the_task_names_none(store):
    pr_wait.mark(store.redis, "sw", ME, 100, "o/r", "pushed")
    asked = []
    enforce(store, {"t1": {"id": "t1"}}, {}, 5_000, lambda repo, branch: asked.append((repo, branch)) or "")
    assert asked == [("o/r", "pushed")]


def test_a_live_checked_wait_covers_the_push(store):
    idle.declare_wait(store.redis, "sw", ME, 10_000_000, "", 1, on={"kind": "merge", "target": URL})
    pr_wait.mark(store.redis, "sw", ME, 100)
    assert enforce(store, {"t1": {"id": "t1", "pr_url": URL}}, {URL: OPEN}) == []
    assert idle.wait(store.redis, "sw", ME)["on"] == {"kind": "merge", "target": URL}
    assert pr_wait.read(store.redis, "sw", ME) is None


@pytest.mark.parametrize(
    "on",
    [
        {"kind": "reply", "target": "abc"},
        {"kind": "checks", "target": "https://github.com/o/r/pull/8", "head": "x"},
    ],
)
def test_a_wait_on_anything_but_this_pull_request_gives_way(store, on):
    idle.declare_wait(store.redis, "sw", ME, 10_000_000, "", 1, on=on)
    pr_wait.mark(store.redis, "sw", ME, 100)
    assert enforce(store, {"t1": {"id": "t1", "pr_url": URL}}, {URL: OPEN})
    assert idle.wait(store.redis, "sw", ME)["on"] == {"kind": "checks", "target": URL, "head": "first"}


def test_a_bare_wait_gives_way_to_the_checked_wait(store):
    idle.declare_wait(store.redis, "sw", ME, 10_000_000, "reading", 1)
    pr_wait.mark(store.redis, "sw", ME, 100)
    assert enforce(store, {"t1": {"id": "t1", "pr_url": URL}}, {URL: OPEN})
    assert idle.wait(store.redis, "sw", ME)["on"]["kind"] == "checks"


def test_a_closed_pull_request_clears_the_mark_without_a_wait(store):
    pr_wait.mark(store.redis, "sw", ME, 100)
    assert enforce(store, {"t1": {"id": "t1", "pr_url": URL}}, {URL: MERGED}) == []
    assert idle.wait(store.redis, "sw", ME) is None
    assert pr_wait.read(store.redis, "sw", ME) is None


def test_an_unreadable_pull_request_keeps_the_mark(store):
    pr_wait.mark(store.redis, "sw", ME, 100)
    assert enforce(store, {"t1": {"id": "t1", "pr_url": URL}}, {}) == []
    assert pr_wait.read(store.redis, "sw", ME)["at"] == "100"


@pytest.mark.parametrize("state, rows", [("finished", {"t1": {"id": "t1", "pr_url": URL}}), ("working", {})])
def test_a_finished_agent_or_unknown_task_gets_no_wait(store, state, rows):
    store.put_agent("sw", AgentRecord(name=ME, lane="eng", task="t1", state=state))
    pr_wait.mark(store.redis, "sw", ME, 100)
    assert enforce(store, rows, {URL: OPEN}) == []
    assert idle.wait(store.redis, "sw", ME) is None


def test_an_agent_that_never_runs_swarm_wait_is_woken_on_merge(store):
    inbox = InboxStore(store.redis)
    rows = {"t1": {"id": "t1", "pr_url": URL}}
    assert swarm_heartbeat.outcome(shell("git push -u origin HEAD"), ENV, store.redis, 100) is True
    queued = {URL: QUEUED}
    assert waits.end_pass(store, "sw", rows, inbox, queued.get, 5_000, queued.get) == [
        f"set the wait of {ME} on merge of {URL} from its push"
    ]
    assert idle.wait(store.redis, "sw", ME)["on"] == {"kind": "merge", "target": URL}
    pulls = {URL: MERGED}
    assert waits.end_pass(store, "sw", rows, inbox, pulls.get, 6_000, pulls.get) == [
        f"ended the wait of {ME}: pull request {URL}, now merged"
    ]
    assert any(f"pull request {URL}, now merged has ended" in item.text for item in inbox.inbox("eng-1@sw"))


@pytest.mark.parametrize(
    "repo, variables",
    [
        ("o/r", {"owner": "o", "name": "r", "branch": "b"}),
        ("https://github.com/o/r", {"owner": "o", "name": "r", "branch": "b"}),
        ("git@github.com:o/r.git", {"owner": "o", "name": "r", "branch": "b"}),
    ],
)
def test_the_forge_lookup_asks_for_the_open_pull_request_of_the_branch(monkeypatch, repo, variables):
    seen = []

    def graphql(query, given):
        seen.append(given)
        return {"repository": {"pullRequests": {"nodes": [{"url": URL}]}}}

    monkeypatch.setattr(pr_wait.merge_queue, "graphql", graphql)
    assert pr_wait.forge_pull(repo, "b") == URL
    assert seen == [variables]


@pytest.mark.parametrize("repo, branch", [("", "b"), ("o/r", ""), ("https://gitlab.com/o/r", "b")])
def test_the_forge_lookup_skips_what_it_cannot_name(monkeypatch, repo, branch):
    monkeypatch.setattr(pr_wait.merge_queue, "graphql", lambda query, given: pytest.fail("asked the forge"))
    assert pr_wait.forge_pull(repo, branch) == ""


@pytest.mark.parametrize(
    "answer", [{"repository": {"pullRequests": {"nodes": []}}}, {"repository": None}, SwarmError("down")]
)
def test_the_forge_lookup_answers_empty_when_no_pull_request_is_found(monkeypatch, answer):
    def graphql(query, given):
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(pr_wait.merge_queue, "graphql", graphql)
    assert pr_wait.forge_pull("o/r", "b") == ""
