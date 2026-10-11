import hashlib
import io
import json
import sqlite3
import tarfile
import tempfile
from contextlib import closing
from pathlib import Path

from scripts.swarm_v2.artifacts import base, local
from scripts.swarm_v2.auth_context import Registration
from scripts.swarm_v2.checkpoints import model, native

ROOT = Path(__file__).parent
TRANSCRIPT = ROOT / "fixtures" / "swarm_v2" / "checkpoints" / "context-only.json"
EVIDENCE_CLASS = (
    "mocked: fixture Claude and Codex homes laid out as the qualified CLIs write them, a local durable directory "
    "backend behind a granted artifact store and fixture launch registrations; no CLI, account, worker Pod or object "
    "store was used"
)
VERSIONS = {"claude": "2.1.296 (Claude Code)", "codex": "codex-cli 0.162.1"}
SESSIONS = {"claude": "5f0c1a2e-0000-4000-8000-00000000c1a0", "codex": "01a12b75-0000-7000-8000-00000000c0de"}
OTHER = {"claude": "5f0c1a2e-0000-4000-8000-0000000000ff", "codex": "01a12b75-0000-7000-8000-0000000000ff"}
ROLLOUT = "2026/10/11/rollout-2026-10-11T09-00-00-{}.jsonl"
CODEWORD = "PLUM-4127"
AUTH_VALUE = "fixture-account-value-never-portable"
AUTH_FILES = {"claude": (".credentials.json", ".claude.json"), "codex": ("auth.json",)}
LOGICAL_CWD = "/work/fixture"
TORN = b'{"type":"user","sessionId":"5f0c'


def write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def jsonl(records: list[dict]) -> bytes:
    return b"".join(json.dumps(record, sort_keys=True).encode() + b"\n" for record in records)


def claude_records(session: str, version: str = "2.1.296") -> list[dict]:
    common = {"sessionId": session, "version": version, "cwd": LOGICAL_CWD}
    return [
        {"type": "user", **common, "message": {"role": "user", "content": f"Remember the codeword {CODEWORD}."}},
        {"type": "assistant", **common, "message": {"role": "assistant", "content": "ok"}},
    ]


def codex_records(session: str, version: str = "0.162.1") -> list[dict]:
    meta = {"id": session, "session_id": session, "cli_version": version, "cwd": LOGICAL_CWD}
    turn = {"type": "message", "role": "user", "content": f"Remember the codeword {CODEWORD}."}
    return [{"type": "session_meta", "payload": meta}, {"type": "response_item", "payload": turn}]


def claude_home(root: Path, cwd: Path, records: list[dict] | None = None) -> Path:
    home = root / "claude-home"
    folder = home / "projects" / native.encoded(cwd)
    session = SESSIONS["claude"]
    write(folder / f"{session}.jsonl", jsonl(records or claude_records(session)) + TORN)
    write(folder / session / "tool-results" / "r1.txt", b"tool output\n")
    write(folder / f"{OTHER['claude']}.jsonl", jsonl(claude_records(OTHER["claude"])))
    for name in AUTH_FILES["claude"]:
        write(home / name, json.dumps({"token": AUTH_VALUE}).encode())
    return home


def thread_index(home: Path, rows: list[tuple[str, str]], journal: str = "wal") -> Path:
    db = home / native.Codex.index
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(f"PRAGMA journal_mode={journal}")
        conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, cli_version TEXT)")
        conn.executemany("INSERT INTO threads VALUES (?, ?, '0.162.1')", rows)
        conn.execute("CREATE TABLE remote_control_enrollments (account_id TEXT)")
        conn.execute("INSERT INTO remote_control_enrollments VALUES (?)", (AUTH_VALUE,))
        conn.commit()
    return db


def codex_home(root: Path, records: list[dict] | None = None, journal: str = "wal", index: bool = True) -> Path:
    home = root / "codex-home"
    rows = []
    for session, held in ((SESSIONS["codex"], records), (OTHER["codex"], None)):
        path = home / "sessions" / ROLLOUT.format(session)
        write(path, jsonl(held or codex_records(session)) + (TORN if session == SESSIONS["codex"] else b""))
        rows.append((session, str(path)))
    if index:
        thread_index(home, rows, journal)
    write(home / "auth.json", json.dumps({"token": AUTH_VALUE}).encode())
    return home


def home_for(harness: str, root: Path, cwd: Path, **kwargs) -> Path:
    return claude_home(root, cwd, **kwargs) if harness == "claude" else codex_home(root, **kwargs)


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


def manifest_for(checkpoint_id: str, harness: str) -> dict:
    manifest = json.loads(TRANSCRIPT.read_text())["manifest"]
    reconstruction = {**manifest["reconstruction"], "harness": harness}
    return {**manifest, "checkpoint_id": checkpoint_id, "operation_id": checkpoint_id, "reconstruction": reconstruction}


def commit(qualifier: native.Qualifier, captured, checkpoint_id: str, harness: str) -> model.Verdict:
    transcript = json.loads(TRANSCRIPT.read_text())["materials"]["normalized_transcript"]
    qualifier.validator.store.put(f"{checkpoint_id}.normalized_transcript", transcript.encode())
    return qualifier.checkpoint(captured, manifest_for(checkpoint_id, harness))


def shown(verdict: model.Verdict) -> dict:
    return {
        "completeness": verdict.completeness,
        "continuation": verdict.continuation,
        "reason": verdict.reason,
        "authoritative": verdict.authoritative,
    }


def outcome(result) -> dict:
    if isinstance(result, native.NativeCapture):
        return {"outcome": native.CAPTURED, "session_id": result.session_id, "quiescence": result.quiescence}
    if isinstance(result, native.Resumed):
        return {"outcome": result.outcome, "session_id": result.session_id, "command": list(result.command)}
    return {"outcome": result.outcome, "cause": result.cause, "decision": list(result.decision)}


def tree(home: Path) -> dict[str, str]:
    if not home.exists():
        return {}
    files = sorted(path for path in home.rglob("*") if path.is_file())
    return {path.relative_to(home).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}


def names(captured: native.NativeCapture) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(captured.material())) as archive:
        return archive.getnames()


def _positive_target(root: Path, harness: str) -> dict:
    cwd, fresh_cwd = root / "worker" / "repo", root / "fresh-pod" / "repo"
    home = home_for(harness, root, cwd)
    writer = native.Qualifier(model.Validator(store(root / "durable")))
    captured = writer.capture(harness, home, SESSIONS[harness], cwd, VERSIONS[harness], stopped=False)
    committed = commit(writer, captured, f"ckpt-native-{harness}", harness)
    fresh = root / "fresh-pod" / "home"
    reader = native.Qualifier(model.Validator(store(root / "durable", execution="e2", generation=3)))
    resumed = reader.restore(f"ckpt-native-{harness}", fresh, fresh_cwd, VERSIONS[harness])
    material = captured.material()
    placed = native.HARNESSES[harness].placed(SESSIONS[harness], fresh_cwd, names(captured)[-1])
    return {
        "capture": outcome(captured),
        "members": names(captured),
        "committed": shown(committed),
        "resumed_in_fresh_home": outcome(resumed),
        "placed_file_count": len(tree(fresh)),
        "conversation_placed_for_fresh_cwd": (fresh / placed).is_file(),
        "placed_conversation_has_codeword": any(CODEWORD in p.read_text() for p in fresh.rglob("*.jsonl")),
        "torn_record_carried": TORN in material,
        "other_conversation_carried": OTHER[harness].encode() in material,
        "auth_files_carried": any(name in material.decode(errors="replace") for name in AUTH_FILES[harness]),
        "auth_value_carried": AUTH_VALUE.encode() in material,
        "auth_files_in_fresh_home": [name for name in AUTH_FILES[harness] if (fresh / name).exists()],
        "native_resume_success_by_target": {
            "capture": writer.metrics()[native.METRIC],
            "restore": reader.metrics()[native.METRIC],
        },
    }


def _positive_fixture(root: Path) -> dict:
    return {harness: _positive_target(root / harness, harness) for harness in ("claude", "codex")}


def _positive() -> tuple[dict, bool]:
    with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
        observed = {"fixture": _positive_fixture(Path(one)), "second_fixture": _positive_fixture(Path(two))}
    first = observed["fixture"]
    checks = [observed["fixture"] == observed["second_fixture"]]
    for harness, seen in first.items():
        checks += [
            seen["capture"]
            == {"outcome": "captured", "session_id": SESSIONS[harness], "quiescence": "consistent_export"},
            seen["committed"]
            == {"completeness": "complete", "continuation": "native", "reason": "native", "authoritative": True},
            seen["resumed_in_fresh_home"]["outcome"] == native.RESUMED,
            seen["resumed_in_fresh_home"]["session_id"] == SESSIONS[harness],
            seen["placed_conversation_has_codeword"],
            seen["conversation_placed_for_fresh_cwd"],
            not seen["torn_record_carried"],
            not seen["other_conversation_carried"],
            not seen["auth_files_carried"],
            not seen["auth_value_carried"],
            seen["auth_files_in_fresh_home"] == [],
        ]
    return observed, all(checks)


def summary_material() -> bytes:
    return json.loads(TRANSCRIPT.read_text())["materials"]["normalized_transcript"].encode()


def database_material(db: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        archive.add(db, arcname=f"{native.Codex.prefix}/{db.name}")
    return buffer.getvalue()


def forged_commit(qualifier: native.Qualifier, checkpoint_id: str, harness: str, material: bytes) -> model.Verdict:
    """A manifest that claims native continuation over material that is not the CLI's conversation files."""
    ref = base.ArtifactRef.of(material)
    qualifier.validator.store.put(f"{checkpoint_id}.{model.NATIVE_SESSION}", material)
    fake = native.NativeCapture(harness, "0.162.1", SESSIONS[harness], (), "stopped_process")
    doc = native.merged(manifest_for(checkpoint_id, harness), fake)
    doc["inventory"][model.NATIVE_SESSION] = {"sha256": ref.sha256, "size": ref.size}
    doc["artifact_hashes"][model.NATIVE_SESSION] = "sha256:" + ref.sha256
    transcript = summary_material()
    qualifier.validator.store.put(f"{checkpoint_id}.normalized_transcript", transcript)
    return qualifier.validator.commit(doc)


def _rejection_fixture(root: Path) -> dict:
    cwd = root / "worker" / "repo"
    codex = codex_home(root / "codex", index=True)
    changed_claude = claude_home(root / "changed", cwd, records=[{"sessionId": SESSIONS["claude"], "note": "no type"}])
    changed_codex = codex_home(root / "changed-codex", records=[{"type": "turn", "payload": {}}], index=False)
    writer = native.Qualifier(model.Validator(store(root / "durable")))
    attempts = {
        "unqualified_installed_version": writer.capture(
            "claude", claude_home(root / "claude", cwd), SESSIONS["claude"], cwd, "3.0.1 (Claude Code)", True
        ),
        "unqualified_harness": writer.capture("copilot", root, "s", cwd, "1.0.80", True),
        "changed_claude_format": writer.capture(
            "claude", changed_claude, SESSIONS["claude"], cwd, VERSIONS["claude"], True
        ),
        "changed_codex_format": writer.capture("codex", changed_codex, SESSIONS["codex"], cwd, VERSIONS["codex"], True),
        "absent_conversation": writer.capture("codex", codex, OTHER["claude"], cwd, VERSIONS["codex"], True),
    }
    context = commit(writer, attempts["unqualified_installed_version"], "ckpt-context-claude", "claude")
    summary = forged_commit(writer, "ckpt-forged-summary", "codex", summary_material())
    database = forged_commit(writer, "ckpt-forged-database", "codex", database_material(codex / native.Codex.index))
    fresh = root / "fresh-pod" / "home"
    write(fresh / "keep.txt", b"protected\n")
    protected, stored = tree(fresh), sorted(writer.validator.store.backend.keys(""))
    reader = native.Qualifier(model.Validator(store(root / "durable", execution="e2", generation=3)))
    restores = {
        name: reader.restore(checkpoint, fresh, cwd, VERSIONS[harness])
        for name, checkpoint, harness in (
            ("context_checkpoint", "ckpt-context-claude", "claude"),
            ("normalized_summary_labelled_native", "ckpt-forged-summary", "codex"),
            ("database_copy_labelled_native", "ckpt-forged-database", "codex"),
        )
    }
    rejected = sorted(reader.validator.store.backend.keys(""))
    text = json.dumps([outcome(r) for r in (*attempts.values(), *restores.values())])
    return {
        "capture_attempts": {name: outcome(result) for name, result in attempts.items()},
        "context_checkpoint": shown(context),
        "forged_summary_commit": shown(summary),
        "forged_database_commit": shown(database),
        "restores": {name: outcome(result) for name, result in restores.items()},
        "fresh_home_unchanged": tree(fresh) == protected,
        "store_unchanged_by_restores": rejected == stored,
        "diagnostics_name_auth_value": AUTH_VALUE in text,
        "native_resume_success_by_target": {
            "capture": writer.metrics()[native.METRIC],
            "restore": reader.metrics()[native.METRIC],
        },
    }


def _rejection() -> tuple[dict, bool]:
    with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
        observed = {"fixture": _rejection_fixture(Path(one)), "second_fixture": _rejection_fixture(Path(two))}
    first = observed["fixture"]
    unavailable = [result["outcome"] == native.UNAVAILABLE for result in first["capture_attempts"].values()]
    restored = [result["outcome"] == native.UNAVAILABLE for result in first["restores"].values()]
    checks = [
        observed["fixture"] == observed["second_fixture"],
        all(unavailable),
        all(restored),
        first["capture_attempts"]["unqualified_installed_version"]["cause"] == "unqualified_version",
        first["capture_attempts"]["changed_claude_format"]["cause"] == native.FORMAT,
        first["capture_attempts"]["changed_codex_format"]["cause"] == native.FORMAT,
        first["context_checkpoint"]["continuation"] == "context",
        first["restores"]["context_checkpoint"]["cause"] == "context_only",
        first["restores"]["normalized_summary_labelled_native"]["cause"] == native.FORMAT,
        first["restores"]["database_copy_labelled_native"]["cause"] == native.FORMAT,
        first["fresh_home_unchanged"],
        first["store_unchanged_by_restores"],
        not first["diagnostics_name_auth_value"],
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


def _active_database(root: Path) -> dict:
    """A writer holds an open transaction while the capture reads the thread index."""
    wal_home = codex_home(root / "wal")
    held = sqlite3.connect(wal_home / native.Codex.index)
    held.execute("BEGIN IMMEDIATE")
    held.execute("UPDATE threads SET rollout_path = '/outside/rollout.jsonl'")
    journal_home = codex_home(root / "journal", journal="delete")
    locked = sqlite3.connect(journal_home / native.Codex.index)
    locked.execute("BEGIN EXCLUSIVE")
    qualifier = native.Qualifier(model.Validator(store(root / "durable")))
    try:
        wal = qualifier.capture("codex", wal_home, SESSIONS["codex"], root, VERSIONS["codex"], False)
        busy = qualifier.capture("codex", journal_home, SESSIONS["codex"], root, VERSIONS["codex"], False)
    finally:
        held.rollback()
        locked.rollback()
        held.close()
        locked.close()
    after = qualifier.capture("codex", journal_home, SESSIONS["codex"], root, VERSIONS["codex"], True)
    return {
        "wal_writer_open": outcome(wal),
        "exclusive_writer_open": outcome(busy),
        "after_writer_stopped": outcome(after),
    }


def _recovery_fixture(root: Path) -> dict:
    cwd = root / "worker" / "repo"
    home = claude_home(root, cwd)
    durable = root / "durable"
    first = native.Qualifier(model.Validator(store(durable)))
    previous = commit(
        first, first.capture("claude", home, SESSIONS["claude"], cwd, VERSIONS["claude"], True), "ckpt-n-1", "claude"
    )
    session = home / "projects" / native.encoded(cwd) / f"{SESSIONS['claude']}.jsonl"
    session.write_bytes(session.read_bytes().replace(TORN, b"") + jsonl(claude_records(SESSIONS["claude"])[:1]))
    second = first.capture("claude", home, SESSIONS["claude"], cwd, VERSIONS["claude"], True)
    interrupted = native.Qualifier(model.Validator(store(durable, backend=Interrupted(durable, writes=3))))
    try:
        commit(interrupted, second, "ckpt-n-2", "claude")
        error = None
    except OSError as raised:
        error = str(raised)
    restarted = native.Qualifier(model.Validator(store(durable)))
    absent = restarted.validator.verify("ckpt-n-2")
    material = restarted.validator.store.recorded(f"ckpt-n-2.{model.NATIVE_SESSION}")
    current = restarted.validator.verify("ckpt-n-1")
    resumed = commit(restarted, second, "ckpt-n-2", "claude")
    held = sorted(restarted.validator.store.backend.keys(""))
    replay = commit(restarted, second, "ckpt-n-2", "claude")
    replay_added = sorted(set(restarted.validator.store.backend.keys("")) - set(held))
    stale = commit(native.Qualifier(model.Validator(store(durable, generation=1))), second, "ckpt-n-2", "claude")
    reader = native.Qualifier(model.Validator(store(durable, execution="e2", generation=3)))
    fresh = root / "fresh-pod" / "home"
    upgraded = reader.restore("ckpt-n-2", fresh, cwd, "2.2.0 (Claude Code)")
    untouched = tree(fresh) == {}
    disabled = native.Qualifier(reader.validator, qualified={})
    unqualified = disabled.restore("ckpt-n-2", fresh, cwd, VERSIONS["claude"])
    context_kept = shown(reader.validator.verify("ckpt-n-2"))
    context_material = reader.validator.store.recorded("ckpt-n-2.normalized_transcript") is not None
    native_resumed = reader.restore("ckpt-n-2", fresh, cwd, VERSIONS["claude"])
    again = reader.restore("ckpt-n-2", fresh, cwd, VERSIONS["claude"])
    return {
        "previous_checkpoint": shown(previous),
        "interruption": error,
        "interrupted_manifest": shown(absent),
        "native_material_durable_while_manifest_absent": material is not None
        and restarted.validator.store.stat(material) == base.VERIFIED,
        "previous_checkpoint_while_interrupted": shown(current),
        "after_restart": shown(resumed),
        "replay": shown(replay),
        "replay_added_keys": replay_added,
        "stale_attempt": shown(stale),
        "installed_cli_upgraded": outcome(upgraded),
        "native_resume_disabled": outcome(unqualified),
        "checkpoint_while_disabled": context_kept,
        "context_material_while_disabled": context_material,
        "fresh_home_untouched_while_unavailable": untouched,
        "qualified_restore": outcome(native_resumed),
        "second_restore_into_same_home": outcome(again),
        "active_database": _active_database(root / "active"),
        "native_resume_success_by_target": reader.metrics()[native.METRIC],
    }


def _recovery() -> tuple[dict, bool]:
    with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
        observed = {"fixture": _recovery_fixture(Path(one)), "second_fixture": _recovery_fixture(Path(two))}
    first = observed["fixture"]
    active = first["active_database"]
    checks = [
        observed["fixture"] == observed["second_fixture"],
        first["previous_checkpoint"]["authoritative"],
        first["interruption"] == "worker stopped",
        first["interrupted_manifest"]["reason"] == "manifest_absent",
        first["native_material_durable_while_manifest_absent"],
        first["previous_checkpoint_while_interrupted"]["authoritative"],
        first["after_restart"]["authoritative"],
        first["replay"] == first["after_restart"],
        first["replay_added_keys"] == [],
        first["stale_attempt"]["reason"] == "scope_mismatch",
        first["installed_cli_upgraded"]["cause"] == "version_changed",
        first["native_resume_disabled"]["cause"] == "unqualified_version",
        first["checkpoint_while_disabled"]["authoritative"],
        first["context_material_while_disabled"],
        first["installed_cli_upgraded"]["decision"] == list(native.DECISION),
        first["fresh_home_untouched_while_unavailable"],
        first["qualified_restore"]["outcome"] == native.RESUMED,
        first["second_restore_into_same_home"]["cause"] == "conversation_present",
        active["wal_writer_open"]["outcome"] == native.CAPTURED,
        active["exclusive_writer_open"]["cause"] == native.BUSY,
        active["after_writer_stopped"]["quiescence"] == "stopped_process",
    ]
    return observed, all(checks)


def run_case(case: str) -> dict:
    observed, passed = {"a": _positive, "b": _rejection, "c": _recovery}[case]()
    return {
        "case": f"T-SV2-CKP-03-{case.upper()}",
        "state": "passed" if passed else "failed",
        "evidence_class": EVIDENCE_CLASS,
        "input_sha256": {TRANSCRIPT.name: hashlib.sha256(TRANSCRIPT.read_bytes()).hexdigest()},
        "observed": json.loads(json.dumps(observed)),
    }
