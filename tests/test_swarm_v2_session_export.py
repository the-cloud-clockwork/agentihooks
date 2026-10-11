import hashlib
import json
import os
from pathlib import Path

import pytest

from scripts.swarm_v2 import session_export
from scripts.swarm_v2 import supervision_protocol as protocol

CLAUDE_ID = "0b7c5a52-5d1e-4d5f-9b8e-3f6a2c1d4e5f"
OLDER_ID = "11111111-2222-4333-8444-555555555555"
CODEX_ID = "019a2b3c-4d5e-7f60-8a9b-0c1d2e3f4a5b"


def _scope(incarnation="inc-1"):
    return {
        "authority": {"execution_id": "exe-" + "a" * 32},
        "incarnation": incarnation,
        "supervisor_pid": 1,
        "process_namespace": "pid:[1]",
    }


def _home(tmp_path, harness):
    home = tmp_path / "attempt" / "homes" / harness
    environ = {
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "CODEX_HOME": str(home / ".codex"),
    }
    return tmp_path / "attempt", home, environ


def _session(path, body, mtime):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    os.utime(path, (mtime, mtime))


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def test_claude_sessions_are_copied_into_a_manifest_the_supervisor_accepts(tmp_path):
    attempt, home, environ = _home(tmp_path, "claude")
    projects = home / ".claude" / "projects" / "-home-worker-attempts-exe"
    _session(projects / f"{CLAUDE_ID}.jsonl", b'{"type": "user"}\n', 2_000)
    _session(projects / f"{OLDER_ID}.jsonl", b'{"type": "old"}\n', 1_000)
    _session(projects / CLAUDE_ID / "subagents" / "agent-1.jsonl", b'{"type": "sub"}\n', 2_500)
    _session(projects / "notes.txt", b"not a session", 3_000)
    _session(home / ".claude" / ".credentials.json", b'{"token": "never"}', 3_000)
    (projects / "linked.jsonl").symlink_to(home / ".claude" / ".credentials.json")
    scope = _scope()

    fields = session_export.export(attempt, scope, environ)

    relative = "checkpoints/inc-1/manifest.json"
    manifest_path = attempt / relative
    assert fields == {"status": "complete", "manifest": relative, "sha256": _digest(manifest_path.read_bytes())}
    manifest = json.loads(manifest_path.read_text())
    assert manifest == {
        **scope,
        "status": "complete",
        "checkpoint_id": "ckp-inc-1",
        "harness": "claude",
        "native_session_id": CLAUDE_ID,
        "files": [
            {
                "path": f"-home-worker-attempts-exe/{CLAUDE_ID}/subagents/agent-1.jsonl",
                "sha256": _digest(b'{"type": "sub"}\n'),
                "size": 16,
            },
            {
                "path": f"-home-worker-attempts-exe/{CLAUDE_ID}.jsonl",
                "sha256": _digest(b'{"type": "user"}\n'),
                "size": 17,
            },
            {
                "path": f"-home-worker-attempts-exe/{OLDER_ID}.jsonl",
                "sha256": _digest(b'{"type": "old"}\n'),
                "size": 16,
            },
        ],
    }
    copied = attempt / "checkpoints" / "inc-1" / "session"
    assert (copied / f"-home-worker-attempts-exe/{CLAUDE_ID}.jsonl").read_bytes() == b'{"type": "user"}\n'
    assert (copied / f"-home-worker-attempts-exe/{CLAUDE_ID}/subagents/agent-1.jsonl").exists()
    assert sorted(p.name for p in copied.rglob("*") if p.is_file()) == [
        f"{CLAUDE_ID}.jsonl",
        f"{OLDER_ID}.jsonl",
        "agent-1.jsonl",
    ]
    assert protocol.checkpoint(attempt, {**scope, **fields}, scope) == "ckp-inc-1"


def test_codex_rollouts_name_their_session_id(tmp_path):
    attempt, home, environ = _home(tmp_path, "codex")
    rollout = home / ".codex" / "sessions" / "2026" / "10" / "11" / f"rollout-2026-10-11T20-30-00-{CODEX_ID}.jsonl"
    _session(rollout, b'{"type": "session_meta"}\n', 1_000)
    _session(home / ".codex" / "auth.json", b"{}", 2_000)

    fields = session_export.export(attempt, _scope("inc-2"), environ)

    manifest = json.loads((attempt / fields["manifest"]).read_text())
    assert (manifest["harness"], manifest["native_session_id"]) == ("codex", CODEX_ID)
    assert [entry["path"] for entry in manifest["files"]] == [f"2026/10/11/{rollout.name}"]


def test_an_attempt_without_a_native_session_reports_incomplete(tmp_path):
    attempt, home, environ = _home(tmp_path, "claude")
    (home / ".claude" / "projects").mkdir(parents=True)
    scope = _scope()

    fields = session_export.export(attempt, scope, environ)

    assert fields == {"status": "incomplete", "reason": "no_native_session"}
    assert not (attempt / "checkpoints").exists()
    assert protocol.checkpoint(attempt, {**scope, **fields}, scope) is None


def test_a_missing_session_folder_reports_incomplete(tmp_path):
    attempt, _, environ = _home(tmp_path, "codex")

    assert session_export.export(attempt, _scope(), environ) == {"status": "incomplete", "reason": "no_native_session"}


@pytest.fixture
def supervised(tmp_path, monkeypatch):
    attempt, home, environ = _home(tmp_path, "claude")
    root = attempt / "run" / "supervision" / "inc-1"
    root.mkdir(parents=True)
    scope = _scope()
    protocol.write(root / "context.json", scope)
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("SWARM_SUPERVISION_DIR", str(root))
    monkeypatch.chdir(attempt)
    monkeypatch.setattr(session_export.time, "sleep", lambda seconds: pytest.fail("waited on a quiesced incarnation"))
    return attempt, home, root, scope


def test_the_exporter_reports_ready_then_exports_once_its_incarnation_is_quiesced(supervised, monkeypatch):
    attempt, home, root, scope = supervised
    _session(home / ".claude" / "projects" / "p" / f"{CLAUDE_ID}.jsonl", b"{}\n", 1_000)
    protocol.write(root / "quiesced.json", {**scope, "incarnation": "previous", "status": "quiesced"})
    pauses = []

    def pause(seconds):
        assert not pauses, "the exporter waited again after its incarnation was quiesced"
        pauses.append(seconds)
        assert protocol.read(root / "exporter.json") == {**scope, "pid": os.getpid(), "status": "ready"}
        assert not (root / "exporter.checkpoint.json").exists()
        protocol.write(root / "quiesced.json", {**scope, "status": "quiesced"})

    monkeypatch.setattr(session_export.time, "sleep", pause)

    assert session_export.main() == 0

    assert pauses == [0.05]
    acknowledgement = protocol.read(root / "exporter.checkpoint.json")
    assert protocol.checkpoint(Path.cwd(), acknowledgement, scope) == "ckp-inc-1"


def test_a_forced_quiescence_is_never_exported(supervised):
    attempt, home, root, scope = supervised
    _session(home / ".claude" / "projects" / "p" / f"{CLAUDE_ID}.jsonl", b"{}\n", 1_000)
    protocol.write(root / "quiesced.json", {**scope, "status": "forced"})

    assert session_export.main() == 0

    assert protocol.read(root / "exporter.checkpoint.json") == {
        **scope,
        "status": "incomplete",
        "reason": "forced_quiescence",
    }
    assert not (attempt / "checkpoints").exists()


def test_a_linked_claude_home_is_never_exported(tmp_path):
    attempt, home, environ = _home(tmp_path, "claude")
    outside = tmp_path / "outside"
    _session(outside / "projects" / "p" / f"{CLAUDE_ID}.jsonl", b"{}\n", 1_000)
    home.mkdir(parents=True)
    (home / ".claude").symlink_to(outside, target_is_directory=True)

    fields = session_export.export(attempt, _scope(), environ)

    assert fields == {"status": "incomplete", "reason": "linked_session_folder"}
    assert not (attempt / "checkpoints").exists()


def test_a_linked_session_folder_is_never_exported(tmp_path):
    attempt, home, environ = _home(tmp_path, "claude")
    outside = tmp_path / "outside"
    _session(outside / "p" / f"{CLAUDE_ID}.jsonl", b"{}\n", 1_000)
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "projects").symlink_to(outside, target_is_directory=True)

    fields = session_export.export(attempt, _scope(), environ)

    assert fields == {"status": "incomplete", "reason": "linked_session_folder"}
    assert not (attempt / "checkpoints").exists()


def test_linked_folders_inside_the_session_folder_are_not_followed(tmp_path):
    attempt, home, environ = _home(tmp_path, "claude")
    projects = home / ".claude" / "projects"
    _session(projects / "p" / f"{CLAUDE_ID}.jsonl", b"{}\n", 1_000)
    _session(tmp_path / "outside" / f"{OLDER_ID}.jsonl", b"{}\n", 2_000)
    (projects / "linked").symlink_to(tmp_path / "outside", target_is_directory=True)

    fields = session_export.export(attempt, _scope(), environ)

    manifest = json.loads((attempt / fields["manifest"]).read_text())
    assert [entry["path"] for entry in manifest["files"]] == [f"p/{CLAUDE_ID}.jsonl"]


def test_only_subagent_transcripts_report_incomplete(tmp_path):
    attempt, home, environ = _home(tmp_path, "claude")
    _session(home / ".claude" / "projects" / "p" / CLAUDE_ID / "subagents" / "agent-1.jsonl", b"{}\n", 1_000)

    fields = session_export.export(attempt, _scope(), environ)

    assert fields == {"status": "incomplete", "reason": "no_native_session"}
    assert not (attempt / "checkpoints").exists()


def test_every_copied_session_file_is_synced_before_the_manifest(tmp_path, monkeypatch):
    attempt, home, environ = _home(tmp_path, "claude")
    _session(home / ".claude" / "projects" / "p" / f"{CLAUDE_ID}.jsonl", b"{}\n", 1_000)
    _session(home / ".claude" / "projects" / "p" / f"{OLDER_ID}.jsonl", b"{}\n", 900)
    synced = []
    real = os.fsync

    def fsync(descriptor):
        synced.append(Path(os.readlink(f"/proc/self/fd/{descriptor}")).name)
        real(descriptor)

    monkeypatch.setattr(os, "fsync", fsync)

    session_export.export(attempt, _scope(), environ)

    assert synced[:3] == [f"{CLAUDE_ID}.jsonl", f"{OLDER_ID}.jsonl", "manifest.pending"]


def test_the_exporter_leaves_without_a_checkpoint_when_its_supervisor_is_gone(supervised, monkeypatch):
    _, _, root, _ = supervised
    parents = iter([41, 1])
    monkeypatch.setattr(session_export.os, "getppid", lambda: next(parents))
    monkeypatch.setattr(session_export.time, "sleep", lambda seconds: pytest.fail("waited after the supervisor left"))

    assert session_export.main() == 1

    assert not (root / "exporter.checkpoint.json").exists()
