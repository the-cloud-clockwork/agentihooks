import json
import subprocess
from pathlib import Path

import pytest

from scripts.swarm import snapshot
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_v2.checkpoints import native
from tests import sv2_ckp03_cases as cases

pytestmark = pytest.mark.xdist_group("fakeredis")

CLAUDE = "engineer@a1b2c3-0001"
CODEX = "engineer@a1b2c3-0002"


@pytest.fixture
def store():
    import fakeredis

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


class Cli:
    def __init__(self, versions=None, listing=""):
        self.versions = cases.VERSIONS if versions is None else versions
        self.listing = listing
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if argv[0] == "git":
            return subprocess.CompletedProcess(argv, 0, self.listing, "")
        if argv[0] not in self.versions:
            raise FileNotFoundError(argv[0])
        return subprocess.CompletedProcess(argv, 0, self.versions[argv[0]] + "\n", "")


def _agent(name, harness, home=None, **fields):
    decision = {"validation": {"home": str(home)}} if home else {}
    session = cases.SESSIONS.get(harness, "")
    return AgentRecord(name, "eng", "t1", harness=harness, conversation_id=session, profile_decision=decision, **fields)


def _kept(slug):
    folder = snapshot.sources_dir(slug)
    return {file.name: file.read_bytes() for file in folder.iterdir()} if folder.exists() else {}


def test_a_claude_conversation_keyed_by_the_swarm_repo_is_captured_from_the_validated_home(tmp_path):
    repo = tmp_path / "repo"
    home = cases.claude_home(tmp_path, repo)
    agent = _agent(CLAUDE, "claude", home)
    found = snapshot.natives("sw", [agent], str(repo), {CLAUDE: str(tmp_path / "wt")}, {}, Cli())
    expected = native.capture("claude", home, cases.SESSIONS["claude"], repo, cases.VERSIONS["claude"], False)
    assert found == {CLAUDE: json.loads(json.dumps(expected.fields()))}
    assert found[CLAUDE]["native_resume"]["outcome"] == native.CAPTURED
    ref = found[CLAUDE]["inventory"][native.model.NATIVE_SESSION]
    assert _kept("sw") == {ref["sha256"]: expected.material()}
    assert (snapshot.sources_dir("sw") / ref["sha256"]).stat().st_mode & 0o777 == 0o600


def test_a_claude_conversation_keyed_by_the_worktree_is_found_there_first(tmp_path):
    work = tmp_path / "wt"
    home = cases.claude_home(tmp_path, work)
    found = snapshot.natives(
        "sw", [_agent(CLAUDE, "claude", home)], str(tmp_path / "repo"), {CLAUDE: str(work)}, {}, Cli()
    )
    assert found[CLAUDE]["native_session_id"] == cases.SESSIONS["claude"]


def test_a_codex_home_comes_from_the_environment_when_the_agent_recorded_none(tmp_path):
    home = cases.codex_home(tmp_path)
    found = snapshot.natives("sw", [_agent(CODEX, "codex")], "/repo", {}, {"CODEX_HOME": str(home)}, Cli())
    assert found[CODEX]["native_resume"] == {"outcome": native.CAPTURED, "harness": "codex"}
    assert found[CODEX]["native_harness_version"] == "0.162.1"


def test_account_state_never_reaches_the_kept_objects(tmp_path):
    repo = tmp_path / "repo"
    agents = [
        _agent(CLAUDE, "claude", cases.claude_home(tmp_path, repo)),
        _agent(CODEX, "codex", cases.codex_home(tmp_path)),
    ]
    found = snapshot.natives("sw", agents, str(repo), {}, {}, Cli())
    assert {name: fields["native_resume"]["outcome"] for name, fields in found.items()} == {
        CLAUDE: native.CAPTURED,
        CODEX: native.CAPTURED,
    }
    assert len(_kept("sw")) == 2
    assert all(cases.AUTH_VALUE.encode() not in data for data in _kept("sw").values())


def test_each_cli_version_is_read_once_per_snapshot(tmp_path):
    repo = tmp_path / "repo"
    home = cases.claude_home(tmp_path, repo)
    cli = Cli()
    agents = [_agent(CLAUDE, "claude", home), _agent("engineer@a1b2c3-0003", "claude", home)]
    snapshot.natives("sw", agents, str(repo), {}, {}, cli)
    assert cli.calls == [["claude", "--version"]]


@pytest.mark.parametrize(
    ("versions", "cause"),
    [({"claude": "9.9.0 (Claude Code)"}, "unqualified_version"), ({}, "unqualified_version")],
)
def test_an_unqualified_or_missing_cli_records_native_resume_unavailable_and_keeps_nothing(tmp_path, versions, cause):
    repo = tmp_path / "repo"
    found = snapshot.natives(
        "sw", [_agent(CLAUDE, "claude", cases.claude_home(tmp_path, repo))], str(repo), {}, {}, Cli(versions)
    )
    assert found[CLAUDE]["native_resume"] == {
        "outcome": native.UNAVAILABLE,
        "harness": "claude",
        "cause": cause,
        "decision": list(native.DECISION),
    }
    assert found[CLAUDE]["continuation"] == native.model.CONTEXT
    assert _kept("sw") == {}


def test_a_conversation_in_neither_place_is_recorded_absent(tmp_path):
    home = cases.claude_home(tmp_path, tmp_path / "elsewhere")
    found = snapshot.natives("sw", [_agent(CLAUDE, "claude", home)], str(tmp_path / "repo"), {}, {}, Cli())
    assert found[CLAUDE]["native_resume"]["cause"] == "conversation_absent"


def test_agents_without_a_conversation_or_a_native_harness_are_skipped(tmp_path):
    cli = Cli()
    agents = [
        AgentRecord("engineer@a1b2c3-0004", "eng", "t1", harness="claude"),
        AgentRecord("engineer@a1b2c3-0005", "eng", "t1", harness="copilot", conversation_id="x"),
    ]
    assert snapshot.natives("sw", agents, "/repo", {}, {}, cli) == {}
    assert cli.calls == []


def test_the_switch_at_zero_captures_no_native_conversation(tmp_path):
    repo = tmp_path / "repo"
    cli = Cli()
    agents = [_agent(CLAUDE, "claude", cases.claude_home(tmp_path, repo))]
    assert snapshot.natives("sw", agents, str(repo), {}, {"AGENTIHOOKS_SWARM_NATIVE_CAPTURE": "0"}, cli) == {}
    assert cli.calls == []
    assert _kept("sw") == {}


def test_pruning_keeps_native_material_a_kept_snapshot_names(tmp_path):
    folder = snapshot.sources_dir("sw")
    for data in (b"native", b"orphan"):
        snapshot._keep(folder, data)
    kept = next(name for name, data in _kept("sw").items() if data == b"native")
    doc = {"taken_at": 0, "natives": {CLAUDE: {"inventory": {"native_session": {"sha256": kept, "size": 6}}}}}
    snapshot.path("sw").parent.mkdir(parents=True, exist_ok=True)
    snapshot.path("sw").write_text(json.dumps(doc))
    snapshot.prune_sources("sw")
    assert list(_kept("sw")) == [kept]


def test_a_snapshot_carries_each_agent_native_capture(store, tmp_path):
    repo = tmp_path / "repo"
    home = cases.claude_home(tmp_path, repo)
    store.create(SwarmConfig("sw", str(repo), 2, 1))
    store.put_agent("sw", _agent(CLAUDE, "claude", home))
    doc = json.loads(snapshot.take(store, "sw", 99, run=Cli(), environ={}).read_text())
    assert doc["natives"][CLAUDE]["native_session_id"] == cases.SESSIONS["claude"]
    assert set(_kept("sw")) <= snapshot.referenced("sw")
    assert Path(home, "projects").is_dir()


def test_an_unreadable_conversation_is_recorded_and_the_snapshot_goes_on(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    agents = [
        _agent(CLAUDE, "claude", cases.claude_home(tmp_path, repo)),
        _agent(CODEX, "codex", cases.codex_home(tmp_path)),
    ]
    real = native._regular

    def unreadable(path):
        if path.name == f"{cases.SESSIONS['claude']}.jsonl":
            raise PermissionError(path)
        return real(path)

    monkeypatch.setattr(native, "_regular", unreadable)
    found = snapshot.natives("sw", agents, str(repo), {}, {}, Cli())
    assert found[CLAUDE]["native_resume"]["cause"] == "native_unreadable"
    assert found[CODEX]["native_resume"]["outcome"] == native.CAPTURED


def test_an_agent_with_no_folder_to_look_in_is_recorded_absent(tmp_path):
    home = cases.claude_home(tmp_path, tmp_path / "repo")
    found = snapshot.natives("sw", [_agent(CLAUDE, "claude", home)], "", {}, {}, Cli())
    assert found[CLAUDE]["native_resume"]["cause"] == "conversation_absent"
    assert found[CLAUDE]["native_resume"]["harness"] == "claude"


def test_the_version_is_read_with_captured_decoded_output_and_a_bounded_wait():
    seen = []

    def run(argv, **kw):
        seen.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, "2.1.296 (Claude Code)\n", "")

    assert snapshot.cli_version("claude", run) == "2.1.296 (Claude Code)\n"
    assert seen == [
        (["claude", "--version"], {"capture_output": True, "text": True, "errors": "replace", "timeout": 30})
    ]


@pytest.mark.parametrize(
    "outcome",
    [
        subprocess.CompletedProcess(["claude"], 1, "2.1.296 (Claude Code)\n", ""),
        FileNotFoundError("claude"),
        subprocess.TimeoutExpired("claude", 30),
    ],
)
def test_a_failed_version_read_is_empty(outcome):
    def run(argv, **kw):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    assert snapshot.cli_version("claude", run) == ""


def test_the_snapshot_captures_running_conversations_as_a_consistent_export(tmp_path, monkeypatch):
    calls = []
    real = native.capture

    def spy(*args, **kw):
        calls.append(kw)
        return real(*args, **kw)

    monkeypatch.setattr(native, "capture", spy)
    repo = tmp_path / "repo"
    found = snapshot.natives(
        "sw", [_agent(CLAUDE, "claude", cases.claude_home(tmp_path, repo))], str(repo), {}, {}, Cli()
    )
    assert [kw["stopped"] is False for kw in calls] == [True]
    assert found[CLAUDE]["quiescence_method"] == "consistent_export"


def test_a_skipped_agent_does_not_stop_the_agents_after_it(tmp_path):
    repo = tmp_path / "repo"
    agents = [
        AgentRecord("engineer@a1b2c3-0004", "eng", "t1", harness="claude"),
        _agent(CLAUDE, "claude", cases.claude_home(tmp_path, repo)),
    ]
    assert list(snapshot.natives("sw", agents, str(repo), {}, {}, Cli())) == [CLAUDE]
