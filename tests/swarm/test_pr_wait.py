import subprocess

import pytest

import hooks.context.swarm_heartbeat as swarm_heartbeat
from scripts.inbox.store import InboxStore
from scripts.swarm import idle, pr_wait, waits
from scripts.swarm.ledger_events import PullRequest
from scripts.swarm.store import AgentRecord, RedisStore, SwarmError

ME = "engineer@a1b2c3-0001"
OTHER = "engineer@a1b2c3-0000"
URL = "https://github.com/o/r/pull/7"
ENV = {"AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_AGENT_NAME": ME, "AGENTIHOOKS_SWARM_TASK": "t1"}
UNTASKED = {k: v for k, v in ENV.items() if k != "AGENTIHOOKS_SWARM_TASK"}
PUBLISH = "agentihooks swarm sw publish --message m -- a.py"
OPEN = PullRequest("OPEN", None, 1, False, head="first")
QUEUED = PullRequest("OPEN", None, 1, False, head="first", queued=True)
MERGED = PullRequest("MERGED", 2, 1, False, head="first")
ROWS = {"t1": {"id": "t1", "pr_url": URL}}


@pytest.fixture
def store():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.put_agent("sw", AgentRecord(name=ME, lane="eng", task="t1", seat="eng-1@sw"))
    return store


def shell(command, cwd="/"):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}


def pushed(store, name=ME):
    found = pr_wait.read(store.redis, "sw", name)
    return found and {key: found[key] for key in pr_wait.FIELDS if key in found}


def git_repo(path, branch="feature"):
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", "git@github.com:O/R.git"], check=True)
    return path


@pytest.mark.parametrize(
    "command",
    [
        "git push -u origin HEAD",
        "cd /w && git -C /w push",
        "git -c http.postBuffer=1048576 push",
        "git --no-pager push origin HEAD",
        "gh pr create --base dev --fill",
        "gh pr ready 7",
        PUBLISH,
        "agentihooks swarm sw merge queue https://github.com/o/r/pull/7",
    ],
)
def test_a_push_or_queue_command_triggers_the_wait(command):
    assert pr_wait.triggered(command)


@pytest.mark.parametrize(
    "command",
    [None, "", "git status", "echo git push", "git push --dry-run", "agentihooks swarm sw merge state x", "gh pr view"],
)
def test_other_commands_do_not_trigger(command):
    assert not pr_wait.triggered(command)


def test_the_hook_marks_a_pinned_worker_push_with_its_repository_and_branch(tmp_path, store):
    path = git_repo(tmp_path)
    assert swarm_heartbeat.push_mark(shell("git push -u origin HEAD", str(path)), ENV, store.redis, 77) is True
    assert pushed(store) == {"pushed_at": "77", "pushed_repo": "o/r", "pushed_branch": "feature"}


def test_the_hook_reads_the_push_in_the_working_directory_when_the_payload_names_none(tmp_path, store, monkeypatch):
    monkeypatch.chdir(git_repo(tmp_path))
    swarm_heartbeat.push_mark({"tool_name": "Bash", "tool_input": {"command": "git push"}}, ENV, store.redis, 77)
    assert pushed(store)["pushed_branch"] == "feature"


def test_the_hook_stamps_the_mark_with_the_clock_and_connects_with_the_session_environment(store, monkeypatch):
    seen = []
    monkeypatch.setattr(swarm_heartbeat.time, "time", lambda: 5.0)
    monkeypatch.setattr("scripts.swarm.store.redis_client", lambda env: seen.append(env) or store.redis)
    assert swarm_heartbeat.push_mark(shell(PUBLISH), ENV) is True
    assert seen == [ENV]
    assert pushed(store) == {"pushed_at": "5000", "pushed_repo": "", "pushed_branch": ""}


@pytest.mark.parametrize(
    "payload, env",
    [
        (shell("git status"), ENV),
        ({"tool_name": "Read", "tool_input": {"command": "git push"}}, ENV),
        (shell("git push"), UNTASKED),
        (shell("git push"), {k: v for k, v in ENV.items() if k != "AGENTIHOOKS_AGENT_NAME"}),
    ],
)
def test_other_calls_and_unpinned_sessions_mark_nothing(payload, env, store):
    assert swarm_heartbeat.push_mark(payload, env, store.redis, 77) is False
    assert pr_wait.read(store.redis, "sw", ME) is None


def test_the_outcome_marks_a_publish_without_a_progress_outcome(store):
    from scripts.gates.progress import Progress

    assert swarm_heartbeat.outcome(shell(PUBLISH), ENV, store.redis, 77) is False
    assert pushed(store)["pushed_at"] == "77"
    assert Progress(store.redis, "sw").read(ME).outcome == ""


def test_the_outcome_opens_no_connection_for_a_publish_outside_a_task(monkeypatch):
    monkeypatch.setattr("scripts.swarm.store.redis_client", lambda env: pytest.fail("connected"))
    assert swarm_heartbeat.outcome(shell(PUBLISH), UNTASKED) is False


def test_a_normalized_codex_push_marks_through_the_post_tool_hook(monkeypatch, store):
    from hooks import hook_manager
    from hooks.targets.normalizer import normalize_payload

    monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("scripts.swarm.store.redis_client", lambda env=None: store.redis)
    raw = {"hook_event_name": "PostToolUse", "session_id": "s1", "tool_response": "ok"}
    hook_manager._swarm_outcome(normalize_payload({**raw, **shell("cd /w\ngit push -u origin HEAD")}))
    assert pushed(store)["pushed_at"]


def test_a_push_records_both_the_progress_outcome_and_the_mark(store):
    from scripts.gates.progress import Progress

    assert swarm_heartbeat.outcome(shell("git push"), ENV, store.redis, 77) is True
    assert Progress(store.redis, "sw").read(ME).outcome == "pushed"
    assert pushed(store)["pushed_at"] == "77"


@pytest.mark.parametrize(
    "command, where",
    [
        (PUBLISH, ("", "")),
        ("git push origin a b", ("o/r", "")),
        ("git push origin HEAD:refs/heads/task", ("o/r", "task")),
    ],
)
def test_the_push_names_its_repository_and_one_branch(tmp_path, command, where):
    assert pr_wait.pushed_where(command, str(git_repo(tmp_path))) == where


def mark(store, name=ME, repo="", branch=""):
    pr_wait.mark(store.redis, "sw", name, 100, repo, branch)


def enforce(store, rows, pulls, now=5_000, forge=None):
    return pr_wait.enforce_pass(store, "sw", rows, pulls.get, now, forge or (lambda repo, branch: ""))


@pytest.mark.parametrize("pull, kind", [(OPEN, "checks"), (QUEUED, "merge")])
def test_the_tick_sets_a_checked_wait_on_the_pushed_task_pull_request(store, pull, kind):
    mark(store)
    assert enforce(store, ROWS, {URL: pull}) == [f"set the wait of {ME} on {kind} of {URL} from its push"]
    held = idle.wait(store.redis, "sw", ME)
    assert held["on"] == {"kind": kind, "target": URL, **({"head": "first"} if kind == "checks" else {})}
    assert held["until"] == 5_000 + waits.CHECKED_MINUTES * 60_000
    assert held["reason"] == pr_wait.REASON
    assert pr_wait.read(store.redis, "sw", ME) is None
    assert enforce(store, ROWS, {URL: pull}) == []


def test_the_tick_finds_the_pull_request_on_the_forge_and_throttles_the_lookup(store):
    mark(store, repo="o/r", branch="pushed")
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


def test_a_new_push_lifts_the_lookup_throttle(store):
    asked = []
    for _ in range(2):
        mark(store, repo="o/r", branch="b")
        enforce(store, {"t1": {"id": "t1"}}, {}, 5_000, lambda repo, branch: asked.append(branch) or "")
    assert asked == ["b", "b"]


def test_the_tick_falls_back_to_the_pushed_branch_when_the_task_names_none(store):
    mark(store, repo="o/r", branch="pushed")
    asked = []
    enforce(store, {"t1": {"id": "t1"}}, {}, 5_000, lambda repo, branch: asked.append((repo, branch)) or "")
    assert asked == [("o/r", "pushed")]


def test_a_pull_request_off_github_gets_no_wait(store):
    mark(store)
    rows = {"t1": {"id": "t1", "pr_url": "https://gitlab.com/o/r/-/merge_requests/7"}}
    assert pr_wait.enforce_pass(store, "sw", rows, lambda url: OPEN, 5_000, lambda repo, branch: "") == []
    assert idle.wait(store.redis, "sw", ME) is None


def test_a_live_checked_wait_covers_the_push(store):
    idle.declare_wait(store.redis, "sw", ME, 10_000_000, "", 1, on={"kind": "merge", "target": URL})
    mark(store)
    assert enforce(store, ROWS, {URL: OPEN}) == []
    assert idle.wait(store.redis, "sw", ME)["on"] == {"kind": "merge", "target": URL}
    assert pr_wait.read(store.redis, "sw", ME) is None


def test_a_wait_ending_now_does_not_cover_the_push(store):
    idle.declare_wait(store.redis, "sw", ME, 5_000, "", 1, on={"kind": "merge", "target": URL})
    mark(store)
    assert enforce(store, ROWS, {URL: OPEN})
    assert idle.wait(store.redis, "sw", ME)["on"]["kind"] == "checks"


@pytest.mark.parametrize(
    "on",
    [
        {"kind": "reply", "target": "abc"},
        {"kind": "checks", "target": "https://github.com/o/r/pull/8", "head": "x"},
    ],
)
def test_a_wait_on_anything_but_this_pull_request_gives_way(store, on):
    idle.declare_wait(store.redis, "sw", ME, 10_000_000, "", 1, on=on)
    mark(store)
    assert enforce(store, ROWS, {URL: OPEN})
    assert idle.wait(store.redis, "sw", ME)["on"] == {"kind": "checks", "target": URL, "head": "first"}


def test_a_bare_wait_gives_way_to_the_checked_wait(store):
    idle.declare_wait(store.redis, "sw", ME, 10_000_000, "reading", 1)
    mark(store)
    assert enforce(store, ROWS, {URL: OPEN})
    assert idle.wait(store.redis, "sw", ME)["on"]["kind"] == "checks"


def test_a_closed_pull_request_clears_the_mark_without_a_wait(store):
    mark(store)
    assert enforce(store, ROWS, {URL: MERGED}) == []
    assert idle.wait(store.redis, "sw", ME) is None
    assert pr_wait.read(store.redis, "sw", ME) is None


def test_an_unreadable_pull_request_keeps_the_mark(store):
    mark(store)
    assert enforce(store, ROWS, {}) == []
    assert pushed(store)["pushed_at"] == "100"


@pytest.mark.parametrize("state, rows", [("finished", ROWS), ("working", {})])
def test_a_finished_agent_or_unknown_task_gets_no_wait(store, state, rows):
    store.put_agent("sw", AgentRecord(name=ME, lane="eng", task="t1", state=state))
    mark(store)
    assert enforce(store, rows, {URL: OPEN}) == []
    assert idle.wait(store.redis, "sw", ME) is None


def covered(store):
    mark(store, OTHER)
    idle.declare_wait(store.redis, "sw", OTHER, 10_000_000, "", 1, on={"kind": "merge", "target": URL})


SKIPS = {
    "finished": (lambda store: mark(store, OTHER), "finished", URL),
    "unread": (lambda store: mark(store, OTHER), "working", "https://github.com/o/r/pull/9"),
    "covered": (covered, "working", URL),
}


@pytest.mark.parametrize("skip", sorted(SKIPS))
def test_an_agent_the_pass_skips_never_stops_it_for_the_next(skip):
    import fakeredis

    prepare, state, url = SKIPS[skip]
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.put_agent("sw", AgentRecord(name=OTHER, lane="eng", task="t2", state=state))
    store.put_agent("sw", AgentRecord(name=ME, lane="eng", task="t1"))
    prepare(store)
    mark(store)
    assert enforce(store, {**ROWS, "t2": {"id": "t2", "pr_url": url}}, {URL: OPEN}) == [
        f"set the wait of {ME} on checks of {URL} from its push"
    ]


def test_an_agent_that_never_runs_swarm_wait_is_woken_on_merge(store):
    inbox = InboxStore(store.redis)
    assert swarm_heartbeat.outcome(shell("git push -u origin HEAD"), ENV, store.redis, 100) is True
    queued = {URL: QUEUED}
    assert waits.end_pass(store, "sw", ROWS, inbox, queued.get, 5_000, queued.get) == [
        f"set the wait of {ME} on merge of {URL} from its push"
    ]
    assert idle.wait(store.redis, "sw", ME)["on"] == {"kind": "merge", "target": URL, "queued": True}
    pulls = {URL: MERGED}
    assert waits.end_pass(store, "sw", ROWS, inbox, pulls.get, 6_000, pulls.get) == [
        f"ended the wait of {ME}: pull request {URL}, now merged"
    ]
    assert any(f"pull request {URL}, now merged has ended" in item.text for item in inbox.inbox("eng-1@sw"))


@pytest.mark.parametrize("repo", ["o/r", "https://github.com/o/r", "git@github.com:o/r.git"])
def test_the_forge_lookup_asks_for_the_open_pull_request_of_the_branch(monkeypatch, repo):
    seen = []

    def graphql(query, given):
        seen.append((query, given))
        return {"repository": {"pullRequests": {"nodes": [{"url": URL}]}}}

    monkeypatch.setattr(pr_wait.merge_queue, "graphql", graphql)
    assert pr_wait.forge_pull(repo, "b") == URL
    assert seen == [(pr_wait.QUERY, {"owner": "o", "name": "r", "branch": "b"})]


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
