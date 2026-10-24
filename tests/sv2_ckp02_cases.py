import dataclasses
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

from scripts.swarm_v2.artifacts import base, local
from scripts.swarm_v2.auth_context import Registration
from scripts.swarm_v2.checkpoints import git_capture, model

ROOT = Path(__file__).parent
TRANSCRIPT = ROOT / "fixtures" / "swarm_v2" / "checkpoints" / "context-only.json"
EVIDENCE_CLASS = (
    "mocked: local git repositories, a local durable directory backend behind a granted artifact store and fixture "
    "launch registrations; no object store, worker Pod or harness was used"
)
BRANCH = "engineer-fixture-0001"
STAMP = "2026-10-11T09:00:00+00:00"
IDENTITY = {
    "GIT_AUTHOR_NAME": "fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_AUTHOR_DATE": STAMP,
    "GIT_COMMITTER_DATE": STAMP,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
}
PROHIBITED = ".env"
PROHIBITED_TEXT = "FIXTURE_SETTING=prohibited\n"


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, **IDENTITY}
    return subprocess.run(["git", "-C", str(cwd), *args], env=env, capture_output=True, text=True, check=True).stdout


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def worktree(root: Path) -> Path:
    """A base pushed to dev, one local commit, index and working tree changes, one allowed and one prohibited file."""
    remote, work = root / "remote.git", root / "worker"
    git(root, "init", "-q", "--bare", str(remote))
    git(root, "init", "-q", str(work))
    git(work, "checkout", "-q", "-b", BRANCH)
    write(work / "app.py", "print('base')\n")
    write(work / "notes.md", "base notes\n")
    write(work / "old.txt", "remove me\n")
    git(work, "add", "app.py", "notes.md", "old.txt")
    git(work, "commit", "-q", "-m", "base")
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "-q", "origin", "HEAD:refs/heads/dev")
    git(work, "fetch", "-q", "origin")
    write(work / "app.py", "print('committed locally')\n")
    git(work, "commit", "-q", "-am", "local only")
    write(work / "staged.py", "STAGED = True\n")
    write(work / "notes.md", "staged notes\n")
    git(work, "add", "staged.py", "notes.md")
    write(work / "notes.md", "staged notes\nunstaged line\n")
    (work / "old.txt").unlink()
    write(work / "draft.md", "allowed untracked draft\n")
    write(work / PROHIBITED, PROHIBITED_TEXT)
    return work


def clone(root: Path, name: str) -> Path:
    target = root / name
    git(root, "clone", "-q", "-b", "dev", str(root / "remote.git"), str(target))
    return target


def store(durable: Path, execution: str = "e1", generation: int = 2, backend=None) -> base.ArtifactStore:
    granted = Registration(
        grant_id="lgr-" + "1" * 32,
        swarm_id="s1",
        execution_id=execution,
        generation=generation,
        seat_id="eng-1",
        task_id="t1",
        account="acct",
        brain_id="brain",
        project_ids=["local:fixture"],
        issuer="issuer",
        audience="audience",
        key_id="key",
        registered_at="2026-10-11T00:00:00Z",
    )
    return base.ArtifactStore(backend or local.LocalBackend(durable), lambda token: granted, "grant")


def manifest_for(checkpoint_id: str) -> dict:
    bundle = json.loads(TRANSCRIPT.read_text())
    return {**bundle["manifest"], "checkpoint_id": checkpoint_id, "operation_id": checkpoint_id}


def commit(validator: model.Validator, source: git_capture.SourceCapture, checkpoint_id: str) -> model.Verdict:
    transcript = json.loads(TRANSCRIPT.read_text())["materials"]["normalized_transcript"]
    validator.store.put(f"{checkpoint_id}.normalized_transcript", transcript.encode())
    return git_capture.checkpoint(validator, source, manifest_for(checkpoint_id))


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def state(work: Path) -> dict:
    return {
        "head": git(work, "rev-parse", "HEAD").strip(),
        "branch": git(work, "symbolic-ref", "--short", "HEAD").strip(),
        "staged_sha256": sha(git(work, "diff", "--cached", "--binary", "--full-index")),
        "unstaged_sha256": sha(git(work, "diff", "--binary", "--full-index")),
        "tracked_status": git(work, "status", "--porcelain", "--untracked-files=no").splitlines(),
        "untracked": sorted(git(work, "ls-files", "--others", "--exclude-standard").split()),
    }


def shown(verdict: model.Verdict) -> dict:
    return {"completeness": verdict.completeness, "reason": verdict.reason, "authoritative": verdict.authoritative}


def members(source: git_capture.SourceCapture) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(source.materials[git_capture.UNTRACKED])) as archive:
        return archive.getnames()


def _unbundled(source: git_capture.SourceCapture) -> dict[str, bytes]:
    return {role: data for role, data in source.materials.items() if role != model.SOURCE_BUNDLE}


def keys(validator: model.Validator) -> list[str]:
    return validator.store.backend.keys("")


def _positive_fixture(root: Path) -> dict:
    work = worktree(root)
    before = state(work)
    source = git_capture.capture(work, "origin/dev")
    writer = model.Validator(store(root / "durable"))
    committed = commit(writer, source, "ckpt-src-0001")
    shutil.rmtree(work)
    target = clone(root, "restored")
    reader = model.Validator(store(root / "durable", execution="e2", generation=3))
    restored = git_capture.restore(reader, "ckpt-src-0001", target)
    after = state(target)
    again = git_capture.capture(target, "origin/dev")
    return {
        "branch_state": source.branch_state,
        "archive_members": members(source),
        "committed": shown(committed),
        "restored_by_next_attempt_after_worker_removed": shown(restored),
        "worker_files_left": work.exists(),
        "original": before,
        "restored": after,
        "allowed_untracked_text": (target / "draft.md").read_text(),
        "prohibited_restored": (target / PROHIBITED).exists(),
        "recaptured_patches_and_archive_equal": _unbundled(again) == _unbundled(source),
        "recaptured_commits_equal": (again.base_commit, again.head_commit) == (source.base_commit, source.head_commit),
        "checkpoint_source_bytes": source.metrics()[git_capture.METRIC],
    }


def _positive() -> tuple[dict, bool]:
    with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
        observed = {"fixture": _positive_fixture(Path(one)), "second_fixture": _positive_fixture(Path(two))}
    first = observed["fixture"]
    original = {**first["original"], "untracked": ["draft.md"]}
    checks = [
        observed["fixture"] == observed["second_fixture"],
        first["branch_state"] == git_capture.BUNDLED,
        first["committed"]["authoritative"],
        first["restored_by_next_attempt_after_worker_removed"]["authoritative"],
        not first["worker_files_left"],
        first["original"]["untracked"] == [PROHIBITED, "draft.md"],
        first["restored"] == original,
        first["allowed_untracked_text"] == "allowed untracked draft\n",
        not first["prohibited_restored"],
        first["recaptured_patches_and_archive_equal"],
        first["recaptured_commits_equal"],
    ]
    return observed, all(checks)


def forged(name: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as archive:
        info = tarfile.TarInfo(name)
        info.size = 7
        archive.addfile(info, io.BytesIO(b"escaped"))
    return buffer.getvalue()


def _rejection_fixture(root: Path) -> dict:
    work = worktree(root)
    write(root / "outside.txt", "outside the worktree\n")
    (work / "linked.txt").symlink_to(root / "outside.txt")
    source = git_capture.capture(work, "origin/dev")
    text = json.dumps(source.fields())
    validator = model.Validator(store(root / "durable"))
    commit(validator, source, "ckpt-src-0001")
    escaping = dataclasses.replace(
        source, materials={**source.materials, git_capture.UNTRACKED: forged("../escape.txt")}
    )
    held = keys(validator)
    try:
        git_capture.checkpoint(validator, escaping, manifest_for("ckpt-src-0002"))
        refused_at_checkpoint = None
    except git_capture.SourceError as error:
        refused_at_checkpoint = str(error)
    checkpoint_stored = sorted(set(keys(validator)) - set(held))
    for role, data in escaping.materials.items():
        validator.store.put(f"ckpt-src-0003.{role}", data)
    bundle = json.loads(TRANSCRIPT.read_text())
    manifest = {**bundle["manifest"], "checkpoint_id": "ckpt-src-0003", "operation_id": "ckpt-src-0003"}
    validator.store.put("ckpt-src-0003.normalized_transcript", bundle["materials"]["normalized_transcript"].encode())
    forged_commit = validator.commit(git_capture.merged(manifest, escaping))
    target = clone(root, "restored")
    reader = model.Validator(store(root / "durable", execution="e2", generation=3))
    protected, stored = state(target), keys(reader)
    try:
        git_capture.restore(reader, "ckpt-src-0003", target)
        reason = None
    except git_capture.SourceError as error:
        reason = str(error)
    rejected_state, rejected_keys = state(target), keys(reader)
    corrected = git_capture.restore(reader, "ckpt-src-0001", target)
    return {
        "archive_members": members(source),
        "exclusions": source.exclusions,
        "manifest_names_the_prohibited_file": PROHIBITED in text,
        "manifest_names_the_prohibited_value": PROHIBITED_TEXT.strip() in text,
        "manifest_names_the_link": "linked.txt" in text,
        "escaping_archive_refused_at_checkpoint": refused_at_checkpoint,
        "escaping_checkpoint_stored_keys": checkpoint_stored,
        "escaping_archive_committed_outside_checkpoint": shown(forged_commit),
        "escape_reason": reason,
        "reason_names_the_member": reason is not None and "escape.txt" in reason,
        "target_unchanged_by_rejection": rejected_state == protected,
        "store_unchanged_by_rejection": rejected_keys == stored,
        "escaped_file_written": (root / "escape.txt").exists(),
        "new_valid_request": shown(corrected),
        "untracked_after_valid_request": state(target)["untracked"],
        "checkpoint_source_bytes": source.metrics()[git_capture.METRIC],
    }


def _rejection() -> tuple[dict, bool]:
    with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
        observed = {"fixture": _rejection_fixture(Path(one)), "second_fixture": _rejection_fixture(Path(two))}
    first = observed["fixture"]
    checks = [
        observed["fixture"] == observed["second_fixture"],
        first["archive_members"] == ["draft.md"],
        "excluded credential: 1" in first["exclusions"],
        "excluded link: 1" in first["exclusions"],
        not first["manifest_names_the_prohibited_file"],
        not first["manifest_names_the_prohibited_value"],
        not first["manifest_names_the_link"],
        first["escaping_archive_refused_at_checkpoint"] == git_capture.ESCAPE,
        first["escaping_checkpoint_stored_keys"] == [],
        first["escape_reason"] == git_capture.ESCAPE,
        not first["reason_names_the_member"],
        first["target_unchanged_by_rejection"],
        first["store_unchanged_by_rejection"],
        not first["escaped_file_written"],
        first["new_valid_request"]["authoritative"],
        first["untracked_after_valid_request"] == ["draft.md"],
    ]
    return observed, all(checks)


class Interrupted(local.LocalBackend):
    def __init__(self, root: Path, writes: int) -> None:
        super().__init__(root)
        self.writes = writes

    def write(self, key: str, data: bytes) -> None:
        if self.writes == 0:
            raise OSError("worker stopped")
        self.writes -= 1
        super().write(key, data)


def _recovery_fixture(root: Path) -> dict:
    work = worktree(root)
    durable = root / "durable"
    first = commit(model.Validator(store(durable)), git_capture.capture(work, "origin/dev"), "ckpt-src-0001")
    write(work / "staged.py", "STAGED = 'second checkpoint'\n")
    git(work, "add", "staged.py")
    write(work / "second.md", "second untracked draft\n")
    second = git_capture.capture(work, "origin/dev")
    interrupted = model.Validator(store(durable, backend=Interrupted(durable, writes=3)))
    try:
        commit(interrupted, second, "ckpt-src-0002")
        error = None
    except OSError as raised:
        error = str(raised)
    restarted = model.Validator(store(durable))
    absent = restarted.verify("ckpt-src-0002")
    kept = restarted.store.recorded(f"ckpt-src-0002.{git_capture.STAGED}") is not None
    previous = restarted.verify("ckpt-src-0001")
    recaptured = git_capture.capture(work, "origin/dev")
    resumed = commit(restarted, recaptured, "ckpt-src-0002")
    held = keys(restarted)
    replayed = commit(restarted, recaptured, "ckpt-src-0002")
    replay_added = sorted(set(keys(restarted)) - set(held))
    stale = commit(model.Validator(store(durable, generation=1)), recaptured, "ckpt-src-0002")
    stale_added = sorted(set(keys(restarted)) - set(held))
    target = clone(root, "restored")
    reader = model.Validator(store(durable, execution="e2", generation=3))
    restored = git_capture.restore(reader, "ckpt-src-0002", target)
    return {
        "first_checkpoint": shown(first),
        "interruption": error,
        "interrupted_manifest": shown(absent),
        "incomplete_material_kept_for_diagnostics": kept,
        "previous_checkpoint_while_interrupted": shown(previous),
        "recapture_after_restart_equal": recaptured == second,
        "after_restart": shown(resumed),
        "replay": shown(replayed),
        "replay_added_keys": replay_added,
        "stale_attempt": shown(stale),
        "stale_attempt_added_keys": stale_added,
        "restored": shown(restored),
        "restored_matches_worker": state(target) == {**state(work), "untracked": ["draft.md", "second.md"]},
        "previous_checkpoint_after_restart": shown(reader.verify("ckpt-src-0001")),
        "checkpoint_source_bytes": second.metrics()[git_capture.METRIC],
    }


def _recovery() -> tuple[dict, bool]:
    with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
        observed = {"fixture": _recovery_fixture(Path(one)), "second_fixture": _recovery_fixture(Path(two))}
    first = observed["fixture"]
    checks = [
        observed["fixture"] == observed["second_fixture"],
        first["first_checkpoint"]["authoritative"],
        first["interruption"] == "worker stopped",
        first["interrupted_manifest"]["reason"] == "manifest_absent",
        first["incomplete_material_kept_for_diagnostics"],
        first["previous_checkpoint_while_interrupted"]["authoritative"],
        first["recapture_after_restart_equal"],
        first["after_restart"]["authoritative"],
        first["replay"] == first["after_restart"],
        first["replay_added_keys"] == [],
        first["stale_attempt"]["reason"] == "scope_mismatch",
        first["stale_attempt_added_keys"] == [],
        first["restored"]["authoritative"],
        first["restored_matches_worker"],
        first["previous_checkpoint_after_restart"]["authoritative"],
    ]
    return observed, all(checks)


def run_case(case: str) -> dict:
    observed, passed = {"a": _positive, "b": _rejection, "c": _recovery}[case]()
    return {
        "case": f"T-SV2-CKP-02-{case.upper()}",
        "state": "passed" if passed else "failed",
        "evidence_class": EVIDENCE_CLASS,
        "input_sha256": {TRANSCRIPT.name: hashlib.sha256(TRANSCRIPT.read_bytes()).hexdigest()},
        "observed": json.loads(json.dumps(observed)),
    }
