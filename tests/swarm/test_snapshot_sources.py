import json
import subprocess

import pytest

from scripts.swarm import snapshot
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_v2.checkpoints import git_capture
from tests import sv2_ckp02_cases as cases

pytestmark = pytest.mark.xdist_group("fakeredis")

AGENT = "engineer@a1b2c3-0001"


@pytest.fixture
def store():
    import fakeredis

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


def _objects(slug: str) -> dict[str, bytes]:
    return {file.name: file.read_bytes() for file in sorted(snapshot.sources_dir(slug).iterdir())}


def test_each_worktree_is_captured_and_its_materials_kept_by_hash(tmp_path):
    work = cases.worktree(tmp_path)
    expected = git_capture.capture(work, "origin/dev")
    found = snapshot.sources("sw", {"master@a1b2c3-0001": "", AGENT: str(work)}, {})
    assert found == {AGENT: json.loads(json.dumps(expected.fields()))}
    objects = _objects("sw")
    for role, ref in found[AGENT]["inventory"].items():
        assert objects[ref["sha256"]] == expected.materials[role]
    assert len(objects) == len(expected.materials)
    assert {(snapshot.sources_dir("sw") / name).stat().st_mode & 0o777 for name in objects} == {0o600}
    assert list(snapshot.sources_dir("sw").glob("*.partial")) == []


def test_the_switch_at_zero_keeps_paths_only(tmp_path):
    work = cases.worktree(tmp_path)
    assert snapshot.sources("sw", {AGENT: str(work)}, {"AGENTIHOOKS_SWARM_SOURCE_CAPTURE": "0"}) == {}
    assert not snapshot.sources_dir("sw").exists()
    assert snapshot.sources("sw", {AGENT: str(work)}, {"AGENTIHOOKS_SWARM_SOURCE_CAPTURE": "1"})[AGENT]["head_commit"]


def test_the_remote_ref_comes_from_the_environment(tmp_path):
    work = cases.worktree(tmp_path)
    assert snapshot.sources("sw", {AGENT: str(work)}, {})[AGENT]["branch_state"] == "bundled"
    pushed = snapshot.sources("sw", {AGENT: str(work)}, {"AGENTIHOOKS_SWARM_SOURCE_REF": "HEAD"})
    assert pushed[AGENT]["branch_state"] == "pushed"


def test_a_worktree_git_cannot_read_is_recorded_without_stopping_the_others(tmp_path):
    work = cases.worktree(tmp_path)
    found = snapshot.sources("sw", {"gone@a1b2c3-0002": str(tmp_path / "gone"), AGENT: str(work)}, {})
    assert found["gone@a1b2c3-0002"] == {"error": "git rev-parse failed with exit 128"}
    assert found[AGENT]["branch_name"] == cases.BRANCH


def _document(target, shas):
    target.parent.mkdir(parents=True, exist_ok=True)
    inventory = {f"role_{n}": {"sha256": sha, "size": 1} for n, sha in enumerate(shas)}
    target.write_text(json.dumps({"taken_at": 0, "sources": {AGENT: {"inventory": inventory}, "x": {"error": "e"}}}))


def test_pruning_keeps_only_objects_a_kept_snapshot_names():
    folder = snapshot.sources_dir("sw")
    for data in (b"a", b"b", b"c"):
        snapshot._keep(folder, data)
    names = sorted(file.name for file in folder.iterdir())
    _document(snapshot.path("sw"), names[:1])
    _document(snapshot.auto_dir("sw") / "auto-5.json", names[1:2])
    (snapshot.auto_dir("sw") / "auto-6.json").write_text(json.dumps({"taken_at": 1, "worktrees": {}}))
    assert snapshot.referenced("sw") == set(names[:2])
    snapshot.prune_sources("sw")
    assert sorted(file.name for file in folder.iterdir()) == names[:2]


def test_sources_live_in_the_swarm_folder_beside_its_snapshot():
    assert snapshot.sources_dir("sw") == snapshot.path("sw").parent / "sources"


def test_a_torn_object_is_written_again():
    folder = snapshot.sources_dir("sw")
    snapshot._keep(folder, b"whole")
    (stored,) = folder.iterdir()
    stored.write_bytes(b"who")
    snapshot._keep(folder, b"whole")
    assert stored.read_bytes() == b"whole"


def test_keeping_the_same_bytes_twice_writes_once():
    folder = snapshot.sources_dir("sw")
    snapshot._keep(folder, b"same")
    (stored,) = folder.iterdir()
    stored.chmod(0o400)
    snapshot._keep(folder, b"same")
    assert stored.stat().st_mode & 0o777 == 0o400


def _listing(work):
    listing = f"worktree {work}\nbranch refs/heads/engineer-a1b2c3-0001\n"

    def git(argv, **kw):
        assert argv[:3] == ["git", "-C", "/repo"]
        return subprocess.CompletedProcess(argv, 0, listing, "")

    return git


def _seed(store, tmp_path):
    work = cases.worktree(tmp_path)
    store.create(SwarmConfig("sw", "/repo", 2, 1, snapshot_minutes=1))
    store.update("sw", state="running")
    store.put_agent("sw", AgentRecord(AGENT, "eng", "t1", pane_id="w1:p1", started_at=5))
    return work


def test_a_snapshot_keeps_each_agent_source_beside_its_worktree_path(store, tmp_path):
    work = _seed(store, tmp_path)
    doc = json.loads(snapshot.take(store, "sw", 99, run=_listing(work), environ={}).read_text())
    assert doc["worktrees"] == {AGENT: str(work)}
    assert doc["sources"] == {AGENT: json.loads(json.dumps(git_capture.capture(work, "origin/dev").fields()))}


def test_an_automatic_snapshot_follows_the_switch_and_prunes_unnamed_objects(store, tmp_path):
    work = _seed(store, tmp_path)
    snapshot._keep(snapshot.sources_dir("sw"), b"orphan")
    off = snapshot.auto(store, "sw", 0, {"AGENTIHOOKS_SWARM_SOURCE_CAPTURE": "0"}, run=_listing(work))
    assert json.loads(off.read_text())["sources"] == {}
    assert list(snapshot.sources_dir("sw").iterdir()) == []
    on = snapshot.auto(store, "sw", 60_000, {}, run=_listing(work))
    named = snapshot.referenced("sw")
    assert {file.name for file in snapshot.sources_dir("sw").iterdir()} == named
    assert json.loads(on.read_text())["sources"][AGENT]["base_commit"]
