from pathlib import Path

import pytest

from scripts.swarm_v2.checkpoints import git_capture, model, native, restore
from scripts.swarm_v2.checkpoints.restore import Directive, Restored, RestoreRefused
from tests import sv2_ckp02_cases as source_cases
from tests import sv2_ckp03_cases as native_cases

pytestmark = pytest.mark.unit

CLAUDE = native_cases.SESSIONS["claude"]
VERSION = native_cases.VERSIONS["claude"]
WATERMARK = 41


class SourceSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Path]] = []

    def __call__(self, validator: model.Validator, checkpoint_id: str, target: Path) -> model.Verdict:
        self.calls.append((checkpoint_id, target))
        return model.Verdict(model.COMPLETE, model.CONTEXT, model.CONTEXT)


def _reader(root: Path, cases=native_cases) -> native.Qualifier:
    return native.Qualifier(model.Validator(cases.store(root / "durable", execution="e2", generation=3)))


def _native(root: Path, version: str = VERSION) -> str:
    cwd = root / "repo"
    home = native_cases.home_for("claude", root, cwd)
    writer = native.Qualifier(model.Validator(native_cases.store(root / "durable")))
    captured = writer.capture("claude", home, CLAUDE, cwd, version, stopped=True)
    native_cases.commit(writer, captured, "ckpt-1", "claude")
    return "ckpt-1"


def _paths(root: Path) -> tuple[Path, Path, Path]:
    return root / "worktree", root / "fresh" / "home", root / "fresh" / "repo"


def _restore(restorer: restore.Restorer, root: Path, directive: Directive, version: str = VERSION) -> Restored:
    return restorer.restore(directive, *_paths(root), version)


def _refused(restorer: restore.Restorer, root: Path, directive: Directive, version: str = VERSION) -> str:
    with pytest.raises(RestoreRefused) as raised:
        _restore(restorer, root, directive, version)
    return str(raised.value)


def test_names_and_types_are_fixed():
    assert restore.METRIC == "restore_outcomes_by_type"
    assert restore.TYPES == ("native", "reconstructed", "fresh")
    assert restore.RESTORING == ("native", "reconstructed")
    assert restore.REFUSED == "refused"
    assert restore.STORE_MISSING == "no checkpoint store is configured for this worker"
    assert issubclass(RestoreRefused, ValueError)


@pytest.mark.parametrize(
    "value",
    [
        {"checkpoint_id": "c", "recovery": "native", "session_id": "s", "watermark": 4},
        {"checkpoint_id": "c", "recovery": "reconstructed", "session_id": "", "watermark": 0},
        {"checkpoint_id": "", "recovery": "fresh", "session_id": "", "watermark": 9},
    ],
)
def test_a_well_formed_directive_parses_and_records_back(value):
    directive = Directive.parse(value)
    assert directive.record() == value
    assert Directive(**value) == directive


@pytest.mark.parametrize(
    "value",
    [
        None,
        "native",
        {"checkpoint_id": "c", "recovery": "native", "session_id": "s"},
        {"checkpoint_id": "c", "recovery": "native", "session_id": "s", "watermark": 4, "extra": 1},
        {"checkpoint_id": "c", "recovery": "resumed", "session_id": "s", "watermark": 4},
        {"checkpoint_id": "c", "recovery": "native", "session_id": "s", "watermark": True},
        {"checkpoint_id": "c", "recovery": "native", "session_id": "s", "watermark": "4"},
        {"checkpoint_id": "c", "recovery": "native", "session_id": "s", "watermark": -1},
        {"checkpoint_id": 7, "recovery": "native", "session_id": "s", "watermark": 4},
        {"checkpoint_id": "c", "recovery": "native", "session_id": 7, "watermark": 4},
        {"checkpoint_id": "c", "recovery": "fresh", "session_id": "", "watermark": 4},
        {"checkpoint_id": "", "recovery": "native", "session_id": "s", "watermark": 4},
        {"checkpoint_id": "", "recovery": "reconstructed", "session_id": "", "watermark": 4},
        {"checkpoint_id": "c", "recovery": "native", "session_id": "", "watermark": 4},
        {"checkpoint_id": "c", "recovery": "reconstructed", "session_id": "s", "watermark": 4},
    ],
)
def test_a_malformed_directive_is_refused(value):
    with pytest.raises(RestoreRefused) as raised:
        Directive.parse(value)
    assert str(raised.value) == "malformed restore directive"


def test_a_native_checkpoint_restores_the_expected_conversation_in_a_fresh_home(tmp_path):
    checkpoint = _native(tmp_path)
    spy = SourceSpy()
    restorer = restore.Restorer(_reader(tmp_path), spy)
    restored = _restore(restorer, tmp_path, Directive(checkpoint, "native", CLAUDE, 4096))
    command = native.HARNESSES["claude"].command(CLAUDE)
    assert restored == Restored("native", checkpoint, WATERMARK, CLAUDE, command)
    assert spy.calls == []
    _, _, cwd = _paths(tmp_path)
    restored_session = tmp_path / "fresh" / "home" / "projects" / native.encoded(cwd) / f"{CLAUDE}.jsonl"
    assert native_cases.CODEWORD in restored_session.read_text()
    assert restorer.metrics() == {"restore_outcomes_by_type": {"native": 1}}
    assert restorer.validator is restorer.qualifier.validator


def test_a_second_independent_fixture_restores_the_same_way(tmp_path):
    first, second = tmp_path / "one", tmp_path / "two"
    seen = []
    for root in (first, second):
        restorer = restore.Restorer(_reader(root), SourceSpy())
        seen.append(_restore(restorer, root, Directive(_native(root), "native", CLAUDE, 0)))
    assert seen[0] == seen[1]


def test_a_corrupt_object_is_refused_before_the_home_changes(tmp_path):
    checkpoint = _native(tmp_path)
    durable = tmp_path / "durable"
    for path in durable.rglob("*"):
        if path.is_file() and "/objects/" in path.as_posix():
            path.write_bytes(b"corrupted")
    restorer = restore.Restorer(_reader(tmp_path), SourceSpy())
    assert _refused(restorer, tmp_path, Directive(checkpoint, "native", CLAUDE, 0)) == "corrupt_manifest"
    assert not (tmp_path / "fresh").exists()
    assert restorer.metrics() == {"restore_outcomes_by_type": {"refused/corrupt_manifest": 1}}


def test_a_corrupt_material_object_is_refused(tmp_path):
    checkpoint = _native(tmp_path)
    store = native_cases.store(tmp_path / "durable")
    manifest = store.recorded(checkpoint)
    for path in (tmp_path / "durable").rglob("*"):
        if path.is_file() and "/objects/" in path.as_posix() and path.name != manifest.sha256:
            path.write_bytes(b"corrupted")
    restorer = restore.Restorer(_reader(tmp_path), SourceSpy())
    assert _refused(restorer, tmp_path, Directive(checkpoint, "native", CLAUDE, 0)) == "corrupt_object"
    assert not (tmp_path / "fresh").exists()


def test_an_absent_checkpoint_is_refused(tmp_path):
    restorer = restore.Restorer(_reader(tmp_path), SourceSpy())
    assert _refused(restorer, tmp_path, Directive("ckpt-9", "reconstructed", "", 0)) == "manifest_absent"
    assert _refused(restorer, tmp_path, Directive("ckpt-9", "reconstructed", "", 0)) == "manifest_absent"
    assert restorer.metrics() == {"restore_outcomes_by_type": {"refused/manifest_absent": 2}}


def test_an_unsupported_harness_version_is_refused_and_nothing_lands(tmp_path):
    checkpoint = _native(tmp_path)
    spy = SourceSpy()
    restorer = restore.Restorer(_reader(tmp_path), spy)
    refused = _refused(restorer, tmp_path, Directive(checkpoint, "native", CLAUDE, 0), "3.0.1 (Claude Code)")
    assert refused == "version_changed"
    assert not (tmp_path / "fresh").exists()
    assert spy.calls == []
    assert restorer.metrics() == {"restore_outcomes_by_type": {"refused/version_changed": 1}}


def test_another_conversation_than_the_decision_expects_is_refused(tmp_path):
    checkpoint = _native(tmp_path)
    restorer = restore.Restorer(_reader(tmp_path), SourceSpy())
    other = native_cases.OTHER["claude"]
    assert _refused(restorer, tmp_path, Directive(checkpoint, "native", other, 0)) == "conversation_mismatch"
    assert not (tmp_path / "fresh").exists()


def test_a_context_checkpoint_never_restores_as_native(tmp_path):
    checkpoint = _native(tmp_path, "3.0.1 (Claude Code)")
    spy = SourceSpy()
    restorer = restore.Restorer(_reader(tmp_path), spy)
    assert _refused(restorer, tmp_path, Directive(checkpoint, "native", CLAUDE, 0)) == "context_only"
    assert spy.calls == []
    restored = _restore(restorer, tmp_path, Directive(checkpoint, "reconstructed", "", 0))
    assert restored == Restored("reconstructed", checkpoint, WATERMARK)
    assert not (tmp_path / "fresh").exists()
    assert restorer.metrics() == {"restore_outcomes_by_type": {"refused/context_only": 1, "reconstructed": 1}}


def _source(root: Path) -> tuple[restore.Restorer, Path]:
    work = source_cases.worktree(root)
    validator = model.Validator(source_cases.store(root / "durable"))
    assert source_cases.commit(validator, git_capture.capture(work, "origin/dev"), "ckpt-src-0001").authoritative
    return restore.Restorer(_reader(root, source_cases)), source_cases.clone(root, "restored")


def test_a_reconstructed_restore_brings_back_the_source_state(tmp_path):
    restorer, target = _source(tmp_path)
    restored = restorer.restore(Directive("ckpt-src-0001", "reconstructed", "", 0), target, *_paths(tmp_path)[1:], "")
    assert restored == Restored("reconstructed", "ckpt-src-0001", WATERMARK)
    assert (target / "draft.md").read_text() == "allowed untracked draft\n"
    assert source_cases.state(target)["branch"] == source_cases.BRANCH


def test_a_source_refusal_is_reported_as_a_refused_restore(tmp_path):
    restorer, target = _source(tmp_path)
    source_cases.write(target / "mine.md", "mine\n")
    with pytest.raises(RestoreRefused) as raised:
        restorer.restore(Directive("ckpt-src-0001", "reconstructed", "", 0), target, *_paths(tmp_path)[1:], "")
    assert str(raised.value) == "source: the restore target has changes of its own"
    assert restorer.metrics() == {"restore_outcomes_by_type": {"refused/source": 1}}


def test_a_source_read_error_is_reported_as_a_refused_restore(tmp_path):
    restorer, _ = _source(tmp_path)

    def unreadable(validator, checkpoint_id, target):
        raise OSError("disk gone")

    restorer.source = unreadable
    with pytest.raises(RestoreRefused) as raised:
        restorer.restore(Directive("ckpt-src-0001", "reconstructed", "", 0), tmp_path, *_paths(tmp_path)[1:], "")
    assert str(raised.value) == "source: disk gone"


def test_a_fresh_directive_restores_nothing(tmp_path):
    restorer = restore.Restorer(_reader(tmp_path), SourceSpy())
    assert _restore(restorer, tmp_path, Directive("", "fresh", "", 7)) == Restored("fresh", "", 7)
    assert _restore(restorer, tmp_path, Directive("", "fresh", "", 8)) == Restored("fresh", "", 8)
    assert restorer.metrics() == {"restore_outcomes_by_type": {"fresh": 2}}


def test_the_default_source_restore_is_the_git_capture_restore(tmp_path):
    assert restore.Restorer(_reader(tmp_path)).source is git_capture.restore


def test_a_launch_without_a_restore_restores_nothing():
    assert restore.launched({"harness": "claude"}) is None


def test_a_fresh_launch_needs_no_store():
    spec = {"restore": {"checkpoint_id": "", "recovery": "fresh", "session_id": "", "watermark": 3}}
    assert restore.launched(spec) == Restored("fresh", "", 3)


@pytest.mark.parametrize("given", [(None, None), ("restorer", None), (None, "paths")])
def test_a_checkpoint_launch_without_a_store_refuses_to_start(given, tmp_path):
    restorer = restore.Restorer(_reader(tmp_path)) if given[0] else None
    paths = restore.Paths(tmp_path, tmp_path, tmp_path, VERSION) if given[1] else None
    spec = {"restore": {"checkpoint_id": "c", "recovery": "native", "session_id": "s", "watermark": 0}}
    with pytest.raises(RestoreRefused) as raised:
        restore.launched(spec, restorer, paths)
    assert str(raised.value) == restore.STORE_MISSING


def test_checkpoint_launch_passes_isolated_source_home_and_cwd_to_the_restorer(tmp_path):
    from types import SimpleNamespace

    calls = []
    expected = restore.Restored("native", "ckp-one", 41, CLAUDE, ("claude", "--resume", CLAUDE))
    restorer = SimpleNamespace(restore=lambda *args: calls.append(args) or expected)
    worktree, home, cwd = _paths(tmp_path)
    directive = restore.Directive("ckp-one", "native", CLAUDE, 41)
    spec = {"restore": {"checkpoint_id": "ckp-one", "recovery": "native", "session_id": CLAUDE, "watermark": 41}}
    assert restore.launched(spec, restorer, restore.Paths(worktree, home, cwd, VERSION)) == expected
    assert calls == [(directive, worktree, home, cwd, VERSION)]


def test_a_checkpoint_launch_restores_through_the_given_restorer(tmp_path):
    checkpoint = _native(tmp_path)
    restorer = restore.Restorer(_reader(tmp_path), SourceSpy())
    worktree, home, cwd = _paths(tmp_path)
    spec = {"restore": {"checkpoint_id": checkpoint, "recovery": "native", "session_id": CLAUDE, "watermark": 1}}
    restored = restore.launched(spec, restorer, restore.Paths(worktree, home, cwd, VERSION))
    assert (restored.recovery, restored.session_id, restored.watermark) == ("native", CLAUDE, WATERMARK)


def test_a_malformed_launch_restore_is_refused():
    with pytest.raises(RestoreRefused) as raised:
        restore.launched({"restore": {"recovery": "native"}})
    assert str(raised.value) == "malformed restore directive"
