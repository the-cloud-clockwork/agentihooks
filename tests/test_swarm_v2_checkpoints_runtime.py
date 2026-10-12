import json
import sys
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from scripts.swarm_v2.checkpoints import model, native, restore, runtime
from tests import sv2_ckp03_cases as cases

pytestmark = pytest.mark.unit


def test_the_production_store_uses_the_registered_attempt_scope(tmp_path):
    from scripts.swarm_v2.auth_context import Registration

    registration = Registration(
        "g", "s1", "e1", 2, "seat", "t1", "account", "brain", ["local:fixture"], "issuer", "worker", "key", ""
    )
    granted = runtime.granted(tmp_path / "durable", registration)
    assert granted.scope.execution_id == "e1"
    assert granted.scope.task_id == "t1"
    assert granted.scope.generation == 2
    assert granted.backend.kind == "local"


def test_controller_listing_reads_only_verified_newest_generation(tmp_path):
    from scripts.swarm_v2.auth_context import Registration

    registration = Registration(
        "g", "s1", "e1", 2, "seat", "t1", "account", "brain", ["local:fixture"], "issuer", "worker", "key", ""
    )
    store = runtime.granted(tmp_path / "durable", registration)
    qualifier = native.Qualifier(model.Validator(store))
    cwd = tmp_path / "repo"
    home = cases.home_for("claude", tmp_path, cwd)
    captured = qualifier.capture("claude", home, cases.SESSIONS["claude"], cwd, cases.VERSIONS["claude"], stopped=True)
    assert cases.commit(qualifier, captured, "ckpt-1", "claude").authoritative
    listing = runtime.Checkpoints(tmp_path / "durable", lambda execution: registration, "dev")
    rows = listing.list("e1")
    assert rows == [
        {
            "checkpoint_id": "ckpt-1",
            "sequence": 1,
            "status": "complete",
            "compatibility": "dev",
            "continuation": "native",
            "watermark": 41,
        }
    ]
    ref = store.recorded("ckpt-1")
    store.backend.write(store.scope.key("objects", ref.sha256), b"corrupt")
    assert listing.list("e1") == []


def test_an_older_execution_cannot_offer_a_checkpoint_after_a_new_generation_promotes(tmp_path):
    import json

    from scripts.swarm_v2.auth_context import Registration

    old = Registration(
        "g", "s1", "e1", 2, "seat", "t1", "account", "brain", ["local:fixture"], "issuer", "worker", "key", ""
    )
    store = runtime.granted(tmp_path / "durable", old)
    qualifier = native.Qualifier(model.Validator(store))
    cwd = tmp_path / "repo"
    home = cases.home_for("claude", tmp_path, cwd)
    taken = qualifier.capture("claude", home, cases.SESSIONS["claude"], cwd, cases.VERSIONS["claude"], stopped=True)
    assert cases.commit(qualifier, taken, "ckpt-1", "claude").authoritative
    store.backend.write(
        store.scope.key("pointers", "00000000000000000003", "00000000000000000001.json"),
        json.dumps({"generation": 3, "checkpoint_id": "newer"}).encode(),
    )
    assert runtime.Checkpoints(tmp_path / "durable", lambda execution: old, "dev").list("e1") == []


def test_replacement_restores_the_registered_checkpoint_before_returning_a_resume_command(tmp_path):
    from scripts.swarm_v2.auth_context import Registration

    registration = Registration(
        "g", "s1", "e1", 2, "seat", "t1", "account", "brain", ["local:fixture"], "issuer", "worker", "key", ""
    )
    store = runtime.granted(tmp_path / "durable", registration)
    qualifier = native.Qualifier(model.Validator(store))
    cwd = tmp_path / "repo"
    home = cases.home_for("claude", tmp_path, cwd)
    captured = qualifier.capture("claude", home, cases.SESSIONS["claude"], cwd, cases.VERSIONS["claude"], stopped=True)
    assert cases.commit(qualifier, captured, "ckpt-1", "claude").authoritative
    replacement = runtime.granted(tmp_path / "durable", replace(registration, execution_id="e2", generation=3))
    directive = restore.Directive("ckpt-1", "native", cases.SESSIONS["claude"], 41)
    paths = restore.Paths(tmp_path / "worktree", tmp_path / "fresh", cwd, cases.VERSIONS["claude"])
    result = runtime.restoring(replacement, directive, paths)
    assert result.command == ("claude", "--resume", cases.SESSIONS["claude"])
    assert result.recovery == "native"


@pytest.mark.parametrize("storage", [{}, {runtime.ROOT: "/fixture"}, {runtime.REMOTE: "true"}])
@pytest.mark.parametrize("verified", [False, True])
def test_a_drain_requires_a_durable_verified_checkpoint(tmp_path, monkeypatch, storage, verified):
    from scripts.swarm_v2 import supervision_runtime
    from tests.test_swarm_v2_supervision_runtime import supervisor

    owner = supervisor.__wrapped__(tmp_path, monkeypatch)
    owner.environment.update(storage)
    monkeypatch.setattr(owner, "quiesce", lambda: True)
    monkeypatch.setattr(owner, "wait", lambda: pytest.fail("unbounded wait"))
    monkeypatch.setattr(supervision_runtime, "checkpoint", lambda *args: "unverified")
    monkeypatch.setattr(supervision_runtime.trees, "cleanup", lambda *args: None)
    calls = []
    monkeypatch.setattr(runtime, "validate_export", lambda *args: calls.append(args) or verified)
    result = owner.drain("termination")
    complete = not storage or verified
    assert result["checkpoint_status"] == ("complete" if complete else "incomplete")
    assert result["checkpoint_id"] == ("unverified" if complete else None)
    assert calls == ([(owner.launch.attempt, "unverified", owner.environment)] if storage else [])


def test_a_supervisor_launch_uses_the_restored_native_command(tmp_path, monkeypatch):
    import json

    from scripts.swarm_v2 import supervision_runtime
    from tests.test_swarm_v2_supervision_runtime import supervisor

    owner = supervisor.__wrapped__(tmp_path, monkeypatch)
    (tmp_path / "restore.json").write_text(
        json.dumps({"recovery": "native", "command": ["codex", "resume", "session"]})
    )
    monkeypatch.setattr(owner, "spawn", lambda *args: None)
    monkeypatch.setattr(owner, "ready", lambda *args: True)
    commands = []
    monkeypatch.setattr(
        owner, "herdr", lambda words: commands.append(words) or {"result": {"root_pane": {"pane_id": "p"}}}
    )
    monkeypatch.setattr(supervision_runtime, "native_command", lambda command, *args: command)
    assert owner.start() is None
    assert "codex resume session" in commands[-1][-1]


@pytest.fixture
def published_attempt(tmp_path, monkeypatch):
    from scripts.swarm_v2.checkpoints import capture
    from tests.test_swarm_v2_checkpoints_capture import portable

    attempt, scope, environ, newest = portable.__wrapped__(tmp_path, monkeypatch)
    capture.publish(attempt, scope, environ, newest)
    return attempt, environ


@pytest.mark.parametrize("recovery", ["native", "reconstructed"])
def test_worker_prepare_materializes_source_and_conversation(published_attempt, tmp_path, monkeypatch, recovery):
    from scripts.swarm_v2 import filesystem
    from tests import sv2_ckp02_cases as sources

    original, environ = published_attempt
    attempt = tmp_path / "e2"
    attempt.mkdir()
    record = {"attempt": "e2", "layout": filesystem.mapping(filesystem.load()), "homes": {"claude": "homes/claude"}}
    (attempt / "execution.json").write_text(json.dumps(record))
    sources.git(tmp_path, "clone", "-q", "-b", "dev", str(tmp_path / "remote.git"), str(attempt / "worktrees"))
    spec = json.loads((original / "launch.json").read_text())
    spec["registration"].update(execution_id="e2", generation=3)
    spec["authority"].update(execution_id="e2", generation=3)
    spec["restore"] = {
        "checkpoint_id": "ckp-fixture",
        "recovery": recovery,
        "session_id": cases.SESSIONS["claude"] if recovery == "native" else "",
        "watermark": 41,
    }
    original_store = runtime.worker_store

    def scoped(actual, root, environment):
        assert (actual, root, environment) == (spec, attempt, environ)
        return original_store(actual, root, environment)

    def installed(harness, environment):
        assert environment == environ
        return cases.VERSIONS[harness]

    monkeypatch.setattr(runtime, "worker_store", scoped)
    monkeypatch.setattr(runtime, "version", installed)
    result = runtime.prepare(spec, attempt, environ)
    assert result.recovery == recovery
    assert result.checkpoint_id == "ckp-fixture"
    assert result.watermark == 2
    assert (attempt / "worktrees" / "app.py").read_text() == "print('committed locally')\n"
    assert (attempt / "worktrees" / "notes.md").read_text() == "staged notes\nunstaged line\n"
    assert (attempt / "worktrees" / "draft.md").read_text() == "allowed untracked draft\n"
    if recovery == "native":
        assert result.command == ("claude", "--resume", cases.SESSIONS["claude"])
        restored = attempt / "homes/claude/.claude/projects" / native.encoded(attempt) / f"{result.session_id}.jsonl"
        assert restored.is_file()
        assert not (attempt / "continuation.txt").exists()
    else:
        assert result.command == ()
        store = runtime.worker_store(spec, attempt, environ)
        doc = json.loads(store.get_range(store.recorded("ckp-fixture")))
        from scripts.swarm_v2.artifacts.base import ArtifactRef

        transcript = store.get_range(ArtifactRef(**doc["inventory"][model.TRANSCRIPT])).decode()
        continuation = attempt / "continuation.txt"
        assert (
            continuation.read_text()
            == "Continue from this recorded conversation. Native resume was unavailable.\n" + transcript
        )
        assert continuation.stat().st_mode & 0o777 == 0o600
        (attempt / "restore.json").write_text(json.dumps(asdict(result)))
        assert runtime.command(attempt, ("claude", "--print")) == ("claude", "--print", continuation.read_text())


def test_worker_prepare_restores_source_and_native_state_for_its_prepared_checkout(published_attempt, tmp_path):
    from scripts.swarm_v2 import filesystem
    from tests import sv2_ckp02_cases as sources

    original, environ = published_attempt
    attempt = tmp_path / "e2"
    attempt.mkdir()
    record = {"attempt": "e2", "layout": filesystem.mapping(filesystem.load()), "homes": {"claude": "homes/claude"}}
    (attempt / "execution.json").write_text(json.dumps(record))
    checkout = attempt / "worktrees" / "task"
    checkout.parent.mkdir()
    sources.git(tmp_path, "clone", "-q", "-b", "dev", str(tmp_path / "remote.git"), str(checkout))
    workspace = attempt / "spool" / "workspaces" / "t1.json"
    workspace.parent.mkdir(parents=True)
    workspace.write_text(json.dumps({"path": str(checkout)}))
    spec = json.loads((original / "launch.json").read_text())
    spec["registration"].update(execution_id="e2", generation=3)
    spec["authority"].update(execution_id="e2", generation=3, task_id="t1")
    spec["checkout"] = {"project": "github.com/the-cloud-clockwork/agentihooks", "agent": "engineer-1"}
    spec["restore"] = {
        "checkpoint_id": "ckp-fixture",
        "recovery": "native",
        "session_id": cases.SESSIONS["claude"],
        "watermark": 41,
    }
    result = runtime.prepare(spec, attempt, environ)
    assert result.recovery == "native"
    assert (checkout / "app.py").read_text() == "print('committed locally')\n"
    assert (checkout / "notes.md").read_text() == "staged notes\nunstaged line\n"
    restored = attempt / "homes/claude/.claude/projects" / native.encoded(checkout) / f"{result.session_id}.jsonl"
    assert restored.is_file()
    assert not (attempt / "worktrees" / ".git").exists()


def test_worker_prepare_requires_granted_project_before_materialization(published_attempt):
    attempt, environ = published_attempt
    spec = json.loads((attempt / "launch.json").read_text())
    spec["registration"]["project_ids"] = ["local:other"]
    spec["restore"] = {
        "checkpoint_id": "ckp-fixture",
        "recovery": "native",
        "session_id": cases.SESSIONS["claude"],
        "watermark": 1,
    }
    with pytest.raises(restore.RestoreRefused) as caught:
        runtime.prepare(spec, attempt, environ)
    assert str(caught.value) == "checkpoint project is not granted to this worker"


def test_worker_prepare_refuses_unverified_checkpoint_before_materialization(published_attempt):
    attempt, environ = published_attempt
    spec = json.loads((attempt / "launch.json").read_text())
    spec["restore"] = {
        "checkpoint_id": "missing",
        "recovery": "native",
        "session_id": cases.SESSIONS["claude"],
        "watermark": 1,
    }
    verdict = model.Validator(runtime.worker_store(spec, attempt, environ)).verify("missing")
    with pytest.raises(restore.RestoreRefused) as caught:
        runtime.prepare(spec, attempt, environ)
    assert str(caught.value) == verdict.reason


def test_controller_listing_checks_original_execution_generation(published_attempt):
    from scripts.swarm_v2.auth_context import Registration

    attempt, environ = published_attempt
    spec = json.loads((attempt / "launch.json").read_text())
    registration = Registration(**spec["registration"])

    def lookup(execution):
        return registration if execution == registration.execution_id else None

    good = runtime.Checkpoints(
        runtime.configured(environ), lookup, "fixture", lambda execution: 2 if execution == "e1" else None
    )
    assert good.list("e1")[0]["checkpoint_id"] == "ckp-fixture"
    assert good.list("unknown") == []
    bad = runtime.Checkpoints(
        runtime.configured(environ), lookup, "fixture", lambda execution: 3 if execution == "e1" else None
    )
    assert bad.list("e1") == []
    assert runtime.Checkpoints(None, lookup, "fixture").list("e1") == []


def test_worker_prepare_restores_a_codex_checkpoint_into_its_isolated_home(tmp_path, monkeypatch):
    from scripts.swarm_v2 import filesystem
    from scripts.swarm_v2.auth_context import Registration

    registration = Registration(
        "g", "s1", "e1", 2, "seat", "t1", "account", "brain", ["local:fixture"], "issuer", "worker", "key", ""
    )
    store = runtime.granted(tmp_path / "durable", registration)
    qualifier = native.Qualifier(model.Validator(store))
    home = cases.home_for("codex", tmp_path, tmp_path)
    captured = qualifier.capture(
        "codex", home, cases.SESSIONS["codex"], tmp_path, cases.VERSIONS["codex"], stopped=True
    )
    assert cases.commit(qualifier, captured, "ckpt-1", "codex").authoritative
    attempt = tmp_path / "e2"
    attempt.mkdir()
    record = {"attempt": "e2", "layout": filesystem.mapping(filesystem.load()), "homes": {"codex": "homes/codex"}}
    (attempt / "execution.json").write_text(json.dumps(record))
    spec = {
        "harness": "codex",
        "registration": asdict(replace(registration, execution_id="e2", generation=3)),
        "authority": {"execution_id": "e2", "generation": 3},
        "restore": {
            "checkpoint_id": "ckpt-1",
            "recovery": "native",
            "session_id": cases.SESSIONS["codex"],
            "watermark": 41,
        },
    }
    monkeypatch.setattr(runtime, "version", lambda harness, environ: cases.VERSIONS[harness])
    result = runtime.prepare(spec, attempt, {runtime.ROOT: str(tmp_path / "durable")})
    assert result.recovery == "native"
    assert result.command == ("codex", "resume", cases.SESSIONS["codex"])
    codex_home = attempt / "homes/codex/.codex"
    assert len(list(codex_home.rglob("*.jsonl"))) == 1
    assert not (attempt / "homes/codex/.claude").exists()


def test_production_restorer_clones_the_declared_source_before_applying_checkpoint(tmp_path, monkeypatch):
    from scripts.swarm_v2.checkpoints import capture
    from tests.test_swarm_v2_checkpoints_capture import portable

    attempt, scope, environ, newest = portable.__wrapped__(tmp_path, monkeypatch)
    spec = json.loads((attempt / "launch.json").read_text())
    spec["registration"]["project_ids"] = ["github.com/fixture/project"]
    (attempt / "launch.json").write_text(json.dumps(spec))
    assert capture.publish(attempt, scope, environ, newest)["status"] == "complete"
    store = runtime.worker_store(spec, attempt, environ)
    target = tmp_path / "cold-source"
    original_run = runtime.subprocess.run
    clones = []

    def run(argv, **kwargs):
        if argv[:2] == ["git", "clone"]:
            clones.append(argv)
            argv = [*argv[:2], str(tmp_path / "remote.git"), *argv[3:]]
        return original_run(argv, **kwargs)

    monkeypatch.setattr(runtime.subprocess, "run", run)
    directive = restore.Directive("ckp-fixture", "native", cases.SESSIONS["claude"], 41)
    paths = restore.Paths(target, tmp_path / "new-home", attempt, cases.VERSIONS["claude"])
    restored = runtime.restoring(store, directive, paths)
    assert restored.recovery == "native"
    assert clones == [["git", "clone", "https://github.com/fixture/project.git", str(target)]]
    assert (target / "app.py").read_text() == "print('committed locally')\n"
    assert (target / "draft.md").read_text() == "allowed untracked draft\n"


@pytest.fixture
def artifact_endpoint(tmp_path):
    from tests.test_swarm_v2_artifacts_api import storage
    from tests.test_swarm_v2_artifacts_transport import endpoint

    held = storage.__wrapped__()
    serving = endpoint.__wrapped__(held, tmp_path)
    value = next(serving)
    try:
        yield value
    finally:
        with pytest.raises(StopIteration):
            next(serving)


@pytest.mark.parametrize("recovery", ["native", "reconstructed"])
def test_scoped_http_store_publishes_and_restores_a_real_checkpoint(artifact_endpoint, tmp_path, monkeypatch, recovery):
    from scripts.swarm_v2 import filesystem
    from scripts.swarm_v2.artifacts.base import ArtifactError
    from scripts.swarm_v2.auth_context import Registration
    from scripts.swarm_v2.broadcast_bridge import grant_path
    from scripts.swarm_v2.checkpoints import capture
    from tests import sv2_ckp02_cases as sources
    from tests.test_swarm_v2_checkpoints_capture import portable

    endpoint, _, _, authority = artifact_endpoint
    attempt, scope, environ, newest = portable.__wrapped__(tmp_path, monkeypatch)
    spec = json.loads((attempt / "launch.json").read_text())
    spec["control_url"] = endpoint["control_url"]
    registration = Registration(**spec["registration"])
    authority.authorize.return_value = registration
    authority.current.return_value.execution_id = registration.execution_id
    grant_path(attempt).parent.mkdir(exist_ok=True)
    grant_path(attempt).write_text("grant")
    (attempt / "launch.json").write_text(json.dumps(spec))
    environ[runtime.REMOTE] = "true"
    assert capture.publish(attempt, scope, environ, newest)["status"] == "complete"
    exported = runtime.worker_store(spec, attempt, environ)
    manifest = json.loads(exported.get_range(exported.recorded("ckp-fixture")))
    assert manifest["authority"]["task_generation"] == 7
    assert manifest["authority"]["execution_generation"] == 2

    replacement = tmp_path / "e2"
    replacement.mkdir()
    record = {"attempt": "e2", "layout": filesystem.mapping(filesystem.load()), "homes": {"claude": "homes/claude"}}
    (replacement / "execution.json").write_text(json.dumps(record))
    sources.git(tmp_path, "clone", "-q", "-b", "dev", str(tmp_path / "remote.git"), str(replacement / "worktrees"))
    grant_path(replacement).parent.mkdir(exist_ok=True)
    grant_path(replacement).write_text("grant")
    registration = replace(registration, execution_id="e2", generation=3)
    authority.authorize.return_value = registration
    claim = authority.current.return_value
    claim.execution_id, claim.execution_generation, claim.generation = "e2", 3, 8
    spec.update(registration=asdict(registration), authority={"execution_id": "e2", "generation": 3})
    spec["restore"] = {
        "checkpoint_id": "ckp-fixture",
        "recovery": recovery,
        "session_id": cases.SESSIONS["claude"] if recovery == "native" else "",
        "watermark": 41,
    }
    restored = runtime.prepare(spec, replacement, environ)
    assert restored.recovery == recovery
    assert restored.watermark == 2
    assert (replacement / "worktrees/app.py").read_text() == "print('committed locally')\n"
    resumed = runtime.worker_store(spec, replacement, environ)
    with pytest.raises(ArtifactError) as caught:
        resumed.backend.write(resumed.scope.key("pointers", "7", "latest.json"), b"{}")
    assert "forbidden_scope" in str(caught.value)


def test_historical_store_scope_selects_last_matching_task_claim():
    from scripts.swarm_v2.auth_context import Registration

    registration = Registration(
        "g", "s1", "e1", 2, "seat", "t1", "account", "brain", ["local:fixture"], "issuer", "worker", "key", ""
    )
    journal = [
        {},
        {"claim": {"execution_id": "other", "execution_generation": 2, "generation": 99}},
        {"claim": {"execution_id": "e1", "execution_generation": 1, "generation": 98}},
        {"claim": {"execution_id": "e1", "execution_generation": 2, "generation": 7}},
        {"claim": {"execution_id": "e1", "execution_generation": 2, "generation": 8}},
        {"claim": {"execution_id": "e1", "execution_generation": 2, "generation": 9}},
    ]
    assert runtime.historical(registration, journal) == replace(registration, generation=9)
    assert runtime.historical(registration, []) is None
    assert runtime.historical(None, journal) is None


def test_controller_fetch_requires_a_verified_checkpoint(published_attempt):
    from scripts.swarm_v2.auth_context import Registration

    attempt, environ = published_attempt
    spec = json.loads((attempt / "launch.json").read_text())
    registration = Registration(**spec["registration"])
    reader = runtime.Checkpoints(
        runtime.configured(environ), lambda execution: registration if execution == "e1" else None, "fixture"
    )
    store = runtime.worker_store(spec, attempt, environ)
    expected = json.loads(store.get_range(store.recorded("ckp-fixture")))
    assert reader.fetch("e1", "ckp-fixture") == expected
    with pytest.raises(restore.RestoreRefused) as caught:
        reader.fetch("e1", "unverified")
    assert str(caught.value) == "checkpoint is not verified for this execution"


def test_source_restoration_clones_only_a_declared_github_project(tmp_path, monkeypatch):
    from scripts.swarm_v2.checkpoints import git_capture

    doc = {"authority": {"project_id": "github.com/fixture/project"}}
    store = SimpleNamespace(recorded=lambda identifier: identifier, get_range=lambda ref: json.dumps(doc).encode())
    validator = SimpleNamespace(store=store)
    calls = []
    monkeypatch.setattr(runtime.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(git_capture, "restore", lambda validator, identifier, target: (validator, identifier, target))
    target = tmp_path / "worktree"
    assert runtime.source(validator, "ckp-one", target) == (validator, "ckp-one", target)
    assert calls == [
        (
            (["git", "clone", "https://github.com/fixture/project.git", str(target)],),
            {"capture_output": True, "timeout": 120, "check": True},
        )
    ]
    doc["authority"]["project_id"] = "local:fixture"
    with pytest.raises(git_capture.SourceError) as caught:
        runtime.source(validator, "ckp-one", target)
    assert str(caught.value) == "source checkout is unavailable"
    target.mkdir()
    (target / ".git").mkdir()
    assert runtime.source(validator, "ckp-one", target) == (validator, "ckp-one", target)
    assert len(calls) == 1


def test_worker_prepare_fresh_requires_no_store_and_preserves_archived_cursor(tmp_path):
    directive = {"checkpoint_id": "", "recovery": "fresh", "session_id": "", "watermark": 41}
    assert runtime.prepare({"restore": directive}, tmp_path, {}) == restore.Restored("fresh", "", 41)
    assert runtime.prepare({}, tmp_path, {}) is None


def test_checkpoint_configuration_names_invalid_root_and_missing_store(tmp_path):
    assert runtime.configured({}) is None
    assert runtime.configured({runtime.ROOT: ""}) is None
    assert runtime.configured({runtime.ROOT: str(tmp_path)}) == tmp_path
    with pytest.raises(restore.RestoreRefused) as caught:
        runtime.configured({runtime.ROOT: "relative"})
    assert str(caught.value) == "checkpoint root must be absolute"
    with pytest.raises(restore.RestoreRefused) as caught:
        runtime.worker_store({}, tmp_path, {})
    assert str(caught.value) == restore.STORE_MISSING
    assert runtime.validate_export(tmp_path, "missing", {}) is False


def test_worker_store_refuses_mismatched_launch_authority(published_attempt):
    attempt, environ = published_attempt
    spec = json.loads((attempt / "launch.json").read_text())
    spec["authority"]["execution_id"] = "another"
    with pytest.raises(restore.RestoreRefused) as caught:
        runtime.worker_store(spec, attempt, environ)
    assert str(caught.value) == "checkpoint registration does not match launch authority"


def test_remote_store_adapter_uses_the_whole_authenticated_launch(tmp_path, monkeypatch):
    calls = []
    backend = object()
    remote = SimpleNamespace(
        controller_backend=lambda environ: calls.append(("controller", environ)) or backend,
        worker_store=lambda spec, attempt, environ: calls.append(("worker", spec, attempt, environ)) or backend,
    )
    monkeypatch.setitem(sys.modules, "scripts.swarm_v2.artifacts.remote", remote)
    environ = {"AGENTIHOOKS_CHECKPOINT_BUCKET": "fixture", runtime.REMOTE: "true"}
    spec = {"registration": {"execution_id": "e1"}, "authority": {"execution_id": "e1"}}
    assert runtime.configured(environ) is backend
    assert runtime.worker_store(spec, tmp_path, environ) is backend
    assert calls == [("controller", environ), ("worker", spec, tmp_path, environ)]


def test_remote_export_validation_uses_the_launch_scope(published_attempt, monkeypatch):
    attempt, local = published_attempt
    spec = json.loads((attempt / "launch.json").read_text())
    store = runtime.worker_store(spec, attempt, local)
    calls = []
    remote = SimpleNamespace(worker_store=lambda spec, root, environ: calls.append((spec, root, environ)) or store)
    monkeypatch.setitem(sys.modules, "scripts.swarm_v2.artifacts.remote", remote)
    environ = {runtime.REMOTE: "true"}
    assert runtime.validate_export(attempt, "ckp-fixture", environ) is True
    assert calls == [(spec, attempt, environ)]


@pytest.mark.parametrize("recovery", ["fresh", "reconstructed"])
def test_worker_command_keeps_continuity_type_visible(tmp_path, recovery):
    (tmp_path / "restore.json").write_text(json.dumps({"recovery": recovery, "command": []}))
    (tmp_path / "continuation.txt").write_text("Recorded conversation")
    expected = ("claude", "Recorded conversation") if recovery == "reconstructed" else ("claude",)
    assert runtime.command(tmp_path, ("claude",)) == expected


def test_harness_version_probe_is_bounded_and_reads_its_output(monkeypatch):
    calls = []
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(stdout=" 2.1.0\n"),
    )
    environ = {"PATH": "/fixture/bin"}
    assert runtime.version("claude", environ) == "2.1.0"
    assert calls == [
        (
            (["claude", "--version"],),
            {"env": environ, "capture_output": True, "text": True, "timeout": 10, "check": True},
        )
    ]


def test_malformed_directive_is_refused_before_loading_any_store(tmp_path):
    with pytest.raises(restore.RestoreRefused):
        runtime.prepare({"restore": None}, tmp_path, {})
