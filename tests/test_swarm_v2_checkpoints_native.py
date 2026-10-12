import io
import json
import sqlite3
import tarfile
from pathlib import Path, PurePosixPath

import pytest

from scripts.swarm_v2.checkpoints import model, native
from tests import sv2_ckp03_cases as cases

pytestmark = pytest.mark.unit

EVIDENCE = Path(__file__).parents[1] / "evidence" / "SV2-CKP-03"
CLAUDE, CODEX = cases.SESSIONS["claude"], cases.SESSIONS["codex"]


def qualifier(root: Path, **kwargs) -> native.Qualifier:
    return native.Qualifier(model.Validator(cases.store(root / "durable", **kwargs)))


def reader(root: Path) -> native.Qualifier:
    return qualifier(root, execution="e2", generation=3)


def captured(root: Path, harness: str, stopped: bool = True) -> tuple[native.Qualifier, object]:
    cwd = root / "repo"
    home = cases.home_for(harness, root, cwd)
    writer = qualifier(root)
    return writer, writer.capture(harness, home, cases.SESSIONS[harness], cwd, cases.VERSIONS[harness], stopped)


def committed(root: Path, harness: str) -> native.Qualifier:
    writer, result = captured(root, harness)
    assert cases.commit(writer, result, "ckpt-1", harness).authoritative
    return writer


def tar(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2.1.296 (Claude Code)", "2.1"),
        ("codex-cli 0.162.1", "0.162"),
        ("v10.20.3", "10.20"),
        ("2.1", None),
        ("", None),
    ],
)
def test_line_reads_major_and_minor_from_a_version_text(text, expected):
    assert native.line(text) == expected


def test_encoded_replaces_every_non_alphanumeric_character_with_a_dash():
    assert native.encoded(Path("/home/Me/dev/tcc-ecosystem/Repo_1.x")) == "-home-Me-dev-tcc-ecosystem-Repo-1-x"


def test_claude_capture_takes_the_named_conversation_and_its_companion_files_only(tmp_path):
    writer, result = captured(tmp_path, "claude", stopped=False)
    assert isinstance(result, native.NativeCapture)
    assert (result.harness, result.version, result.session_id) == ("claude", "2.1.296", CLAUDE)
    assert result.quiescence == "consistent_export"
    assert [name for name, _ in result.files] == ["companion/tool-results/r1.txt", "conversation.jsonl"]
    assert dict(result.files)["conversation.jsonl"] == cases.jsonl(cases.claude_records(CLAUDE))
    assert writer.metrics() == {native.METRIC: {"claude/captured": 1}}


def test_codex_capture_takes_the_indexed_rollout_up_to_its_last_complete_record(tmp_path):
    _, result = captured(tmp_path, "codex")
    assert isinstance(result, native.NativeCapture)
    assert (result.harness, result.version, result.session_id, result.quiescence) == (
        "codex",
        "0.162.1",
        CODEX,
        "stopped_process",
    )
    name = "rollout/" + cases.ROLLOUT.format(CODEX)
    assert dict(result.files) == {name: cases.jsonl(cases.codex_records(CODEX))}


def test_codex_capture_finds_the_rollout_by_name_when_no_index_exists(tmp_path):
    home = cases.codex_home(tmp_path, index=False)
    result = qualifier(tmp_path).capture("codex", home, CODEX, tmp_path, cases.VERSIONS["codex"], True)
    assert [name for name, _ in result.files] == ["rollout/" + cases.ROLLOUT.format(CODEX)]


def test_codex_capture_finds_the_rollout_by_name_when_the_index_lacks_the_thread(tmp_path):
    home = cases.codex_home(tmp_path)
    with sqlite3.connect(home / native.Codex.index) as conn:
        conn.execute("DELETE FROM threads")
    result = qualifier(tmp_path).capture("codex", home, CODEX, tmp_path, cases.VERSIONS["codex"], True)
    assert result.session_id == CODEX


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_the_portable_material_never_carries_account_state_or_another_conversation(tmp_path, harness):
    _, result = captured(tmp_path, harness)
    material = result.material()
    assert cases.AUTH_VALUE.encode() not in material
    assert cases.OTHER[harness].encode() not in material
    assert cases.TORN not in material
    with tarfile.open(fileobj=io.BytesIO(material)) as archive:
        assert all(info.mode == native.MEMBER_MODE and info.isreg() for info in archive.getmembers())


def test_material_is_identical_for_identical_files(tmp_path):
    _, one = captured(tmp_path / "one", "claude")
    _, two = captured(tmp_path / "two", "claude")
    assert one.material() == two.material()


def test_capture_fields_qualify_native_continuation_with_the_recorded_identity(tmp_path):
    _, result = captured(tmp_path, "claude")
    ref = model.ArtifactRef.of(result.material())
    assert result.fields() == {
        "continuation": "native",
        "native_resume_capability": "qualified",
        "native_session_id": CLAUDE,
        "native_harness_version": "2.1.296",
        "quiescence_method": "stopped_process",
        "inventory": {"native_session": {"sha256": ref.sha256, "size": ref.size}},
        "artifact_hashes": {"native_session": "sha256:" + ref.sha256},
        "native_resume": {"outcome": "captured", "harness": "claude"},
    }


@pytest.mark.parametrize(
    ("harness", "version", "cause"),
    [
        ("claude", "3.0.1 (Claude Code)", "unqualified_version"),
        ("codex", "codex-cli 0.163.0", "unqualified_version"),
        ("claude", "unknown", "unqualified_version"),
        ("copilot", "1.0.80", "unqualified_harness"),
    ],
)
def test_capture_outside_the_qualification_table_is_unavailable(tmp_path, harness, version, cause):
    writer = qualifier(tmp_path)
    result = writer.capture(harness, tmp_path, "s", tmp_path, version, True)
    assert result == native.Unavailable(harness, cause)
    assert result.outcome == native.UNAVAILABLE
    assert result.decision == ("continue_from_context", "start_fresh")
    assert writer.metrics() == {native.METRIC: {f"{harness}/native_resume_unavailable": 1}}


def test_an_empty_qualification_table_disables_capture(tmp_path):
    cwd = tmp_path / "repo"
    home = cases.claude_home(tmp_path, cwd)
    disabled = native.Qualifier(model.Validator(cases.store(tmp_path / "durable")), qualified={})
    result = disabled.capture("claude", home, CLAUDE, cwd, cases.VERSIONS["claude"], True)
    assert result == native.Unavailable("claude", "unqualified_version")


@pytest.mark.parametrize(
    "records",
    [
        [{"sessionId": CLAUDE, "version": "2.1.296"}],
        [{"type": "user", "version": "2.1.296"}],
        [{"type": "user", "sessionId": CLAUDE}],
        [{"type": "user", "sessionId": CLAUDE, "version": 2}],
        [{"type": 7, "sessionId": CLAUDE, "version": "2.1.296"}],
        [
            {"type": "user", "sessionId": CLAUDE, "version": "2.1.296"},
            {"type": "user", "sessionId": cases.OTHER["claude"], "version": "2.1.296"},
        ],
    ],
)
def test_a_changed_claude_format_is_unavailable(tmp_path, records):
    cwd = tmp_path / "repo"
    home = cases.claude_home(tmp_path, cwd, records=records)
    result = qualifier(tmp_path).capture("claude", home, CLAUDE, cwd, cases.VERSIONS["claude"], True)
    assert result == native.Unavailable("claude", native.FORMAT)


@pytest.mark.parametrize(
    "records",
    [
        [{"type": "turn", "payload": {"id": CODEX, "cli_version": "0.162.1"}}],
        [{"type": "session_meta", "payload": "flat"}],
        [{"type": "session_meta", "payload": {"cli_version": "0.162.1"}}],
        [{"type": "session_meta", "payload": {"id": CODEX}}],
        [{"type": "session_meta", "payload": {"id": CODEX, "cli_version": 162}}],
    ],
)
def test_a_changed_codex_format_is_unavailable(tmp_path, records):
    home = cases.codex_home(tmp_path, records=records)
    result = qualifier(tmp_path).capture("codex", home, CODEX, tmp_path, cases.VERSIONS["codex"], True)
    assert result == native.Unavailable("codex", native.FORMAT)


def test_a_conversation_without_a_complete_record_is_a_changed_format(tmp_path):
    cwd = tmp_path / "repo"
    home = cases.claude_home(tmp_path, cwd)
    (home / "projects" / native.encoded(cwd) / f"{CLAUDE}.jsonl").write_bytes(cases.TORN)
    result = qualifier(tmp_path).capture("claude", home, CLAUDE, cwd, cases.VERSIONS["claude"], True)
    assert result == native.Unavailable("claude", native.FORMAT)


def test_a_record_that_is_not_json_is_a_changed_format(tmp_path):
    cwd = tmp_path / "repo"
    home = cases.claude_home(tmp_path, cwd)
    (home / "projects" / native.encoded(cwd) / f"{CLAUDE}.jsonl").write_bytes(b"not json\n")
    result = qualifier(tmp_path).capture("claude", home, CLAUDE, cwd, cases.VERSIONS["claude"], True)
    assert result == native.Unavailable("claude", native.FORMAT)


def test_a_conversation_written_by_another_cli_line_is_a_version_change(tmp_path):
    cwd = tmp_path / "repo"
    home = cases.claude_home(tmp_path, cwd, records=cases.claude_records(CLAUDE, version="2.0.9"))
    result = qualifier(tmp_path).capture("claude", home, CLAUDE, cwd, cases.VERSIONS["claude"], True)
    assert result == native.Unavailable("claude", "version_changed")


@pytest.mark.parametrize("session", [["list"], {"a": 1}, 7])
def test_a_session_id_that_is_not_text_is_a_changed_format(tmp_path, session):
    cwd = tmp_path / "repo"
    records = [{"type": "user", "sessionId": session, "version": "2.1.296"}]
    home = cases.claude_home(tmp_path, cwd, records=records)
    result = qualifier(tmp_path).capture("claude", home, CLAUDE, cwd, cases.VERSIONS["claude"], True)
    assert result == native.Unavailable("claude", native.FORMAT)


def test_a_capture_past_the_archive_bounds_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(native, "ARCHIVE_MEMBERS", 1)
    _, result = captured(tmp_path / "members", "claude")
    assert result == native.Unavailable("claude", "native_too_large")
    monkeypatch.setattr(native, "ARCHIVE_MEMBERS", 10)
    monkeypatch.setattr(native, "ARCHIVE_BYTES", 10)
    _, result = captured(tmp_path / "bytes", "codex")
    assert result == native.Unavailable("codex", "native_too_large")


def test_a_capture_at_the_archive_bounds_is_taken(tmp_path, monkeypatch):
    _, first = captured(tmp_path / "probe", "claude")
    size = sum(len(data) for _, data in first.files)
    monkeypatch.setattr(native, "ARCHIVE_MEMBERS", 2)
    monkeypatch.setattr(native, "ARCHIVE_BYTES", size)
    _, result = captured(tmp_path / "bounded", "claude")
    assert isinstance(result, native.NativeCapture)


def test_a_file_holding_another_session_is_an_identity_mismatch(tmp_path):
    cwd = tmp_path / "repo"
    home = cases.claude_home(tmp_path, cwd, records=cases.claude_records(cases.OTHER["claude"]))
    result = qualifier(tmp_path).capture("claude", home, CLAUDE, cwd, cases.VERSIONS["claude"], True)
    assert result == native.Unavailable("claude", "identity_mismatch")


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_an_absent_conversation_is_unavailable(tmp_path, harness):
    cwd = tmp_path / "repo"
    home = cases.home_for(harness, tmp_path, cwd)
    result = qualifier(tmp_path).capture(harness, home, "missing-session", cwd, cases.VERSIONS[harness], True)
    assert result == native.Unavailable(harness, "conversation_absent")


def test_a_linked_conversation_file_is_not_read(tmp_path):
    cwd = tmp_path / "repo"
    home = cases.claude_home(tmp_path, cwd)
    session = home / "projects" / native.encoded(cwd) / f"{CLAUDE}.jsonl"
    target = tmp_path / "elsewhere.jsonl"
    target.write_bytes(session.read_bytes())
    session.unlink()
    session.symlink_to(target)
    result = qualifier(tmp_path).capture("claude", home, CLAUDE, cwd, cases.VERSIONS["claude"], True)
    assert result == native.Unavailable("claude", "conversation_absent")


def test_linked_companion_files_stay_out(tmp_path):
    cwd = tmp_path / "repo"
    home = cases.claude_home(tmp_path, cwd)
    (tmp_path / "secret.txt").write_text(cases.AUTH_VALUE)
    (home / "projects" / native.encoded(cwd) / CLAUDE / "linked.txt").symlink_to(tmp_path / "secret.txt")
    result = qualifier(tmp_path).capture("claude", home, CLAUDE, cwd, cases.VERSIONS["claude"], True)
    assert [name for name, _ in result.files] == ["companion/tool-results/r1.txt", "conversation.jsonl"]


def test_an_index_pointing_outside_the_sessions_folder_is_refused(tmp_path):
    home = cases.codex_home(tmp_path)
    with sqlite3.connect(home / native.Codex.index) as conn:
        conn.execute("UPDATE threads SET rollout_path = ?", (str(tmp_path / "outside.jsonl"),))
    result = qualifier(tmp_path).capture("codex", home, CODEX, tmp_path, cases.VERSIONS["codex"], True)
    assert result == native.Unavailable("codex", native.ESCAPE)


def test_a_linked_rollout_found_by_name_is_not_read(tmp_path):
    home = cases.codex_home(tmp_path, index=False)
    rollout = home / "sessions" / cases.ROLLOUT.format(CODEX)
    target = tmp_path / "outside.jsonl"
    target.write_bytes(rollout.read_bytes())
    rollout.unlink()
    rollout.symlink_to(target)
    result = qualifier(tmp_path).capture("codex", home, CODEX, tmp_path, cases.VERSIONS["codex"], True)
    assert result == native.Unavailable("codex", native.ESCAPE)


def test_an_indexed_rollout_under_a_linked_folder_inside_the_sessions_is_refused(tmp_path):
    home = cases.codex_home(tmp_path)
    day = home / "sessions" / "2026" / "10" / "11"
    day.rename(day.with_name("12"))
    day.symlink_to(day.with_name("12"))
    result = qualifier(tmp_path).capture("codex", home, CODEX, tmp_path, cases.VERSIONS["codex"], True)
    assert result == native.Unavailable("codex", native.ESCAPE)


def test_a_rollout_under_a_linked_folder_outside_the_home_is_refused(tmp_path):
    home = cases.codex_home(tmp_path / "source", index=False)
    day = home / "sessions" / "2026" / "10" / "11"
    outside = tmp_path / "outside"
    day.rename(outside)
    day.symlink_to(outside)
    result = qualifier(tmp_path).capture("codex", home, CODEX, tmp_path, cases.VERSIONS["codex"], True)
    assert result == native.Unavailable("codex", native.ESCAPE)


def test_an_index_that_is_not_a_database_is_a_changed_format(tmp_path):
    home = cases.codex_home(tmp_path, index=False)
    (home / native.Codex.index).write_bytes(b"not a database" * 100)
    result = qualifier(tmp_path).capture("codex", home, CODEX, tmp_path, cases.VERSIONS["codex"], True)
    assert result == native.Unavailable("codex", native.FORMAT)


def test_an_index_without_the_threads_table_is_a_changed_format(tmp_path):
    home = cases.codex_home(tmp_path, index=False)
    with sqlite3.connect(home / native.Codex.index) as conn:
        conn.execute("CREATE TABLE renamed (id TEXT)")
    result = qualifier(tmp_path).capture("codex", home, CODEX, tmp_path, cases.VERSIONS["codex"], True)
    assert result == native.Unavailable("codex", native.FORMAT)


def test_an_open_wal_writer_does_not_change_what_the_capture_reads(tmp_path):
    home = cases.codex_home(tmp_path)
    writer = sqlite3.connect(home / native.Codex.index)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE threads SET rollout_path = '/outside/rollout.jsonl'")
        result = qualifier(tmp_path).capture("codex", home, CODEX, tmp_path, cases.VERSIONS["codex"], False)
    finally:
        writer.rollback()
        writer.close()
    assert result.session_id == CODEX


def test_an_exclusive_writer_makes_the_capture_unavailable_as_busy(tmp_path):
    home = cases.codex_home(tmp_path, journal="delete")
    writer = sqlite3.connect(home / native.Codex.index)
    try:
        writer.execute("BEGIN EXCLUSIVE")
        result = qualifier(tmp_path).capture("codex", home, CODEX, tmp_path, cases.VERSIONS["codex"], False)
    finally:
        writer.rollback()
        writer.close()
    assert result == native.Unavailable("codex", native.BUSY)


def test_the_index_is_opened_read_only_without_waiting_on_a_lock(tmp_path, monkeypatch):
    home = cases.codex_home(tmp_path)
    opened = []
    real = native.sqlite3.connect

    def recording(database, **options):
        opened.append((database, options))
        return real(database, **options)

    monkeypatch.setattr(native.sqlite3, "connect", recording)
    qualifier(tmp_path).capture("codex", home, CODEX, tmp_path, cases.VERSIONS["codex"], True)
    uri = (home / native.Codex.index).resolve().as_uri()
    assert opened == [(f"{uri}?mode=ro", {"uri": True, "timeout": 0})]


def test_unavailable_fields_commit_a_context_continuation():
    assert native.Unavailable("codex", "unqualified_version").fields() == {
        "continuation": "context",
        "native_resume_capability": "unsupported",
        "native_session_id": None,
        "native_resume": {
            "outcome": "native_resume_unavailable",
            "harness": "codex",
            "cause": "unqualified_version",
            "decision": ["continue_from_context", "start_fresh"],
        },
    }


def test_a_conversation_the_capture_cannot_read_is_unavailable_not_an_error(tmp_path, monkeypatch):
    home = cases.claude_home(tmp_path, tmp_path / "cwd")

    def unreadable(path):
        raise PermissionError(path)

    monkeypatch.setattr(native, "_regular", unreadable)
    found = native.capture("claude", home, cases.SESSIONS["claude"], tmp_path / "cwd", cases.VERSIONS["claude"], False)
    assert (found.harness, found.outcome, found.cause) == ("claude", native.UNAVAILABLE, "native_unreadable")


def test_merged_replaces_native_material_and_keeps_every_other_material():
    manifest = {
        "inventory": {
            "native_session": {"sha256": "0" * 64, "size": 1},
            "source_bundle": {"sha256": "1" * 64, "size": 2},
        },
        "artifact_hashes": {"native_session": "sha256:" + "0" * 64, "source_bundle": "sha256:" + "1" * 64},
        "exclusions": ["environment variables", "untracked caches"],
        "kept": True,
    }
    doc = native.merged(manifest, native.Unavailable("claude", "x"))
    assert doc["inventory"] == {"source_bundle": {"sha256": "1" * 64, "size": 2}}
    assert doc["artifact_hashes"] == {"source_bundle": "sha256:" + "1" * 64}
    assert doc["exclusions"] == [
        "environment variables",
        "untracked caches",
        "account authentication stores",
        "harness databases, which the CLI rebuilds from its conversation files",
    ]
    assert doc["kept"] is True
    assert doc["continuation"] == "context"


def test_merged_on_an_empty_manifest_carries_the_exclusions():
    doc = native.merged({}, native.Unavailable("claude", "x"))
    assert doc["exclusions"] == list(native.EXCLUSIONS)
    assert doc["inventory"] == {}


def test_checkpoint_stores_the_native_material_before_the_manifest(tmp_path):
    writer, result = captured(tmp_path, "claude")
    verdict = cases.commit(writer, result, "ckpt-1", "claude")
    assert (verdict.completeness, verdict.continuation, verdict.authoritative) == ("complete", "native", True)
    ref = writer.validator.store.recorded("ckpt-1.native_session")
    assert writer.validator.store.get_range(ref) == result.material()


def test_checkpoint_refuses_a_capture_from_another_harness(tmp_path):
    writer, result = captured(tmp_path, "claude")
    with pytest.raises(native.NativeError, match="harness_mismatch"):
        writer.checkpoint(result, cases.manifest_for("ckpt-1", "codex"))
    assert writer.validator.store.recorded("ckpt-1.native_session") is None


def test_checkpoint_stores_nothing_for_an_ungranted_authority(tmp_path):
    writer, result = captured(tmp_path, "claude")
    manifest = cases.manifest_for("ckpt-1", "claude")
    manifest["authority"] = {**manifest["authority"], "task_generation": 1}
    verdict = writer.checkpoint(result, manifest)
    assert verdict.reason == "scope_mismatch"
    assert writer.validator.store.recorded("ckpt-1.native_session") is None


def test_an_unavailable_capture_commits_a_context_checkpoint(tmp_path):
    writer = qualifier(tmp_path)
    verdict = cases.commit(writer, native.Unavailable("claude", "unqualified_version"), "ckpt-1", "claude")
    assert (verdict.completeness, verdict.continuation, verdict.authoritative) == ("complete", "context", True)
    assert writer.validator.store.recorded("ckpt-1.native_session") is None


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_restore_places_the_conversation_for_the_fresh_cwd_and_reports_its_identity(tmp_path, harness):
    committed(tmp_path, harness)
    home, cwd = tmp_path / "fresh", tmp_path / "fresh-repo"
    restoring = reader(tmp_path)
    result = restoring.restore("ckpt-1", home, cwd, cases.VERSIONS[harness])
    session = cases.SESSIONS[harness]
    assert result == native.Resumed(harness, session, native.HARNESSES[harness].command(session))
    assert restoring.metrics() == {native.METRIC: {f"{harness}/resumed": 1}}
    files = sorted(p.relative_to(home).as_posix() for p in home.rglob("*") if p.is_file())
    if harness == "claude":
        folder = f"projects/{native.encoded(cwd)}"
        assert files == [f"{folder}/{session}.jsonl", f"{folder}/{session}/tool-results/r1.txt"]
    else:
        assert files == ["sessions/" + cases.ROLLOUT.format(session)]
    assert all((p.stat().st_mode & 0o777) == native.MEMBER_MODE for p in home.rglob("*") if p.is_file())


@pytest.mark.parametrize("separator", ["\u2028", "\u2029"])
def test_native_conversation_records_split_only_on_newline(tmp_path, separator):
    cwd = tmp_path / "repo"
    home = cases.home_for("claude", tmp_path, cwd)
    path = home / "projects" / native.encoded(cwd) / f"{CLAUDE}.jsonl"
    path.write_text(path.read_text().replace("fixture", f"fixture{separator}paragraph"))
    writer = qualifier(tmp_path)
    taken = writer.capture("claude", home, CLAUDE, cwd, cases.VERSIONS["claude"], stopped=True)
    assert isinstance(taken, native.NativeCapture)
    assert cases.commit(writer, taken, "ckpt-1", "claude").authoritative
    resumed = reader(tmp_path).restore("ckpt-1", tmp_path / "fresh", cwd, cases.VERSIONS["claude"])
    assert resumed == native.Resumed("claude", CLAUDE, ("claude", "--resume", CLAUDE))


def test_resume_commands_name_the_session():
    assert native.Claude().command("s1") == ("claude", "--resume", "s1")
    assert native.Codex().command("s1") == ("codex", "resume", "s1")


def test_claude_places_companion_files_under_the_session_folder():
    placed = native.Claude().placed("s1", Path("/w/r"), "companion/a/b.txt")
    assert placed == PurePosixPath("projects/-w-r/s1/a/b.txt")


def test_restore_of_a_context_checkpoint_is_unavailable_and_changes_nothing(tmp_path):
    cases.commit(qualifier(tmp_path), native.Unavailable("claude", "unqualified_version"), "ckpt-1", "claude")
    home = tmp_path / "fresh"
    result = reader(tmp_path).restore("ckpt-1", home, tmp_path, cases.VERSIONS["claude"])
    assert result == native.Unavailable("claude", "context_only")
    assert not home.exists()


def test_restore_of_a_missing_checkpoint_is_unavailable(tmp_path):
    restoring = reader(tmp_path)
    result = restoring.restore("ckpt-none", tmp_path / "fresh", tmp_path, cases.VERSIONS["claude"])
    assert result == native.Unavailable("unknown", "manifest_absent")
    assert restoring.metrics() == {native.METRIC: {"unknown/native_resume_unavailable": 1}}


@pytest.mark.parametrize(
    ("version", "cause"),
    [("2.2.0 (Claude Code)", "version_changed"), ("3.0.1 (Claude Code)", "version_changed"), ("", "version_changed")],
)
def test_restore_on_another_installed_line_is_unavailable(tmp_path, version, cause):
    committed(tmp_path, "claude")
    home = tmp_path / "fresh"
    assert reader(tmp_path).restore("ckpt-1", home, tmp_path, version) == native.Unavailable("claude", cause)
    assert not home.exists()


def test_restore_with_native_resume_disabled_is_unavailable(tmp_path):
    committed(tmp_path, "codex")
    disabled = native.Qualifier(reader(tmp_path).validator, qualified={})
    home = tmp_path / "fresh"
    result = disabled.restore("ckpt-1", home, tmp_path, cases.VERSIONS["codex"])
    assert result == native.Unavailable("codex", "unqualified_version")
    assert not home.exists()


def test_restore_never_overwrites_a_conversation_already_in_the_home(tmp_path):
    committed(tmp_path, "codex")
    home = tmp_path / "fresh"
    target = home / "sessions" / cases.ROLLOUT.format(CODEX)
    cases.write(target, b"kept\n")
    result = reader(tmp_path).restore("ckpt-1", home, tmp_path, cases.VERSIONS["codex"])
    assert result == native.Unavailable("codex", "conversation_present")
    assert target.read_bytes() == b"kept\n"


def test_a_failed_read_back_removes_everything_the_restore_placed(tmp_path, monkeypatch):
    committed(tmp_path, "claude")
    home = tmp_path / "fresh"
    home.mkdir()

    def absent(self, home, session_id, cwd):
        raise native.NativeError("conversation_absent")

    monkeypatch.setattr(native.Claude, "collect", absent)
    result = reader(tmp_path).restore("ckpt-1", home, tmp_path / "fresh-repo", cases.VERSIONS["claude"])
    assert result == native.Unavailable("claude", "conversation_absent")
    assert list(home.iterdir()) == []


def test_a_read_back_of_another_session_is_an_identity_mismatch(tmp_path, monkeypatch):
    committed(tmp_path, "codex")
    home = tmp_path / "fresh"
    monkeypatch.setattr(native.Codex, "collect", lambda self, home, session_id, cwd: {"rollout/x": b""})
    monkeypatch.setattr(
        native.Codex,
        "identity",
        lambda self, files: (cases.OTHER["codex"] if files == {"rollout/x": b""} else CODEX, "0.162.1"),
    )
    result = reader(tmp_path).restore("ckpt-1", home, tmp_path, cases.VERSIONS["codex"])
    assert result == native.Unavailable("codex", "identity_mismatch")
    assert not any(path.is_file() for path in home.rglob("*"))


def test_a_write_failure_removes_what_was_placed_and_reports_placement_failed(tmp_path, monkeypatch):
    committed(tmp_path, "claude")
    home = tmp_path / "fresh"
    home.mkdir()
    opened = []
    real = native.os.open

    def failing(path, flags, mode):
        opened.append(path)
        if len(opened) == 2:
            raise OSError("disk full")
        return real(path, flags, mode)

    monkeypatch.setattr(native.os, "open", failing)
    result = reader(tmp_path).restore("ckpt-1", home, tmp_path / "fresh-repo", cases.VERSIONS["claude"])
    assert result == native.Unavailable("claude", "placement_failed")
    assert len(opened) == 2
    assert list(home.iterdir()) == []


def test_restore_never_writes_through_a_link_inside_the_home(tmp_path):
    committed(tmp_path, "codex")
    home = tmp_path / "fresh"
    (home / "elsewhere").mkdir(parents=True)
    (home / "sessions").symlink_to(home / "elsewhere")
    result = reader(tmp_path).restore("ckpt-1", home, tmp_path, cases.VERSIONS["codex"])
    assert result == native.Unavailable("codex", native.ESCAPE)
    assert list((home / "elsewhere").iterdir()) == []


def test_an_indexed_rollout_that_is_a_link_is_not_read(tmp_path):
    home = cases.codex_home(tmp_path)
    rollout = home / "sessions" / cases.ROLLOUT.format(CODEX)
    kept = rollout.with_name("kept.jsonl")
    rollout.rename(kept)
    rollout.symlink_to(kept)
    result = qualifier(tmp_path).capture("codex", home, CODEX, tmp_path, cases.VERSIONS["codex"], True)
    assert result == native.Unavailable("codex", native.ESCAPE)


@pytest.mark.parametrize("linked", [False, True])
def test_a_relative_index_path_resolves_under_the_sessions_folder(tmp_path, linked):
    home = cases.codex_home(tmp_path)
    rollout = home / "sessions" / cases.ROLLOUT.format(CODEX)
    if linked:
        kept = rollout.with_name("kept.jsonl")
        rollout.rename(kept)
        rollout.symlink_to(kept)
    with sqlite3.connect(home / native.Codex.index) as conn:
        conn.execute("UPDATE threads SET rollout_path = ? WHERE id = ?", (cases.ROLLOUT.format(CODEX), CODEX))
    result = qualifier(tmp_path).capture("codex", home, CODEX, tmp_path, cases.VERSIONS["codex"], True)
    if linked:
        assert result == native.Unavailable("codex", native.ESCAPE)
    else:
        assert result.session_id == CODEX


def test_a_relative_index_path_climbing_out_is_refused(tmp_path):
    home = cases.codex_home(tmp_path)
    with sqlite3.connect(home / native.Codex.index) as conn:
        conn.execute("UPDATE threads SET rollout_path = '../auth.json'")
    result = qualifier(tmp_path).capture("codex", home, CODEX, tmp_path, cases.VERSIONS["codex"], True)
    assert result == native.Unavailable("codex", native.ESCAPE)


def test_cleanup_ignores_a_folder_that_gained_other_files(tmp_path):
    folder = tmp_path / "made"
    folder.mkdir()
    (folder / "other.txt").write_text("kept")
    native._remove([folder])
    assert (folder / "other.txt").read_text() == "kept"


def test_restore_never_follows_a_link_already_at_a_target(tmp_path):
    committed(tmp_path, "codex")
    home = tmp_path / "fresh"
    target = home / "sessions" / cases.ROLLOUT.format(CODEX)
    target.parent.mkdir(parents=True)
    target.symlink_to(tmp_path / "dangling.jsonl")
    result = reader(tmp_path).restore("ckpt-1", home, tmp_path, cases.VERSIONS["codex"])
    assert result == native.Unavailable("codex", native.ESCAPE)
    assert not (tmp_path / "dangling.jsonl").exists()


@pytest.mark.parametrize(
    ("members", "cause"),
    [
        ({"rollout/../../escape.jsonl": b"x\n"}, native.ESCAPE),
        ({"/abs.jsonl": b"x\n"}, native.ESCAPE),
        ({"companion/a.txt": cases.jsonl(cases.codex_records(CODEX))}, native.ESCAPE),
        ({"rollout/a.jsonl": b"x\n", "rollout/b.jsonl": b"y\n"}, native.FORMAT),
        ({"rollout/a.jsonl": cases.jsonl(cases.codex_records(cases.OTHER["codex"]))}, "identity_mismatch"),
    ],
)
def test_restore_refuses_forged_material_before_writing(tmp_path, members, cause):
    writer = qualifier(tmp_path)
    cases.forged_commit(writer, "ckpt-1", "codex", tar(members))
    home = tmp_path / "fresh"
    assert reader(tmp_path).restore("ckpt-1", home, tmp_path, cases.VERSIONS["codex"]) == native.Unavailable(
        "codex", cause
    )
    assert not home.exists()
    assert not (tmp_path / "escape.jsonl").exists()


def test_restore_refuses_a_link_member(tmp_path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo("rollout/a.jsonl")
        info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
        archive.addfile(info)
    cases.forged_commit(qualifier(tmp_path), "ckpt-1", "codex", buffer.getvalue())
    result = reader(tmp_path).restore("ckpt-1", tmp_path / "fresh", tmp_path, cases.VERSIONS["codex"])
    assert result == native.Unavailable("codex", native.ESCAPE)


@pytest.mark.parametrize("material", [b"not a tar", json.dumps({"summary": "normalized"}).encode()])
def test_restore_of_non_native_material_labelled_native_is_unavailable(tmp_path, material):
    cases.forged_commit(qualifier(tmp_path), "ckpt-1", "codex", material)
    result = reader(tmp_path).restore("ckpt-1", tmp_path / "fresh", tmp_path, cases.VERSIONS["codex"])
    assert result == native.Unavailable("codex", native.FORMAT)


def test_restore_refuses_an_archive_past_the_member_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(native, "ARCHIVE_MEMBERS", 1)
    cases.forged_commit(qualifier(tmp_path), "ckpt-1", "codex", tar({"rollout/a": b"1\n", "rollout/b": b"2\n"}))
    result = reader(tmp_path).restore("ckpt-1", tmp_path / "fresh", tmp_path, cases.VERSIONS["codex"])
    assert result == native.Unavailable("codex", native.TOO_LARGE)


def test_restore_refuses_an_archive_past_the_byte_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(native, "ARCHIVE_BYTES", 2)
    cases.forged_commit(qualifier(tmp_path), "ckpt-1", "codex", tar({"rollout/a": b"123\n"}))
    result = reader(tmp_path).restore("ckpt-1", tmp_path / "fresh", tmp_path, cases.VERSIONS["codex"])
    assert result == native.Unavailable("codex", native.TOO_LARGE)


@pytest.mark.parametrize(("members", "limit", "refused"), [(1, 2, False), (2, 2, False), (3, 2, True), (2, 1, True)])
def test_members_counts_against_the_member_limit(monkeypatch, members, limit, refused):
    monkeypatch.setattr(native, "ARCHIVE_MEMBERS", limit)
    data = tar({f"m{index}": b"abc" for index in range(members)})
    if refused:
        with pytest.raises(native.NativeError, match=f"^{native.TOO_LARGE}$"):
            native.members(data)
    else:
        assert native.members(data) == {f"m{index}": b"abc" for index in range(members)}


@pytest.mark.parametrize(("limit", "refused"), [(6, False), (5, True)])
def test_members_sums_every_member_against_the_byte_limit(monkeypatch, limit, refused):
    monkeypatch.setattr(native, "ARCHIVE_BYTES", limit)
    data = tar({"a": b"abc", "b": b"def"})
    if refused:
        with pytest.raises(native.NativeError, match=f"^{native.TOO_LARGE}$"):
            native.members(data)
    else:
        assert native.members(data) == {"a": b"abc", "b": b"def"}


def test_members_refuses_a_compressed_archive():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("rollout/a.jsonl")
        info.size = 3
        archive.addfile(info, io.BytesIO(b"x\n\n"))
    with pytest.raises(native.NativeError, match=f"^{native.FORMAT}$"):
        native.members(buffer.getvalue())


def test_write_new_never_replaces_or_follows(tmp_path):
    existing = tmp_path / "existing"
    existing.write_bytes(b"kept")
    with pytest.raises(FileExistsError):
        native._write_new(existing, b"new", [])
    (tmp_path / "link").symlink_to(tmp_path / "target")
    with pytest.raises(OSError):
        native._write_new(tmp_path / "link", b"new", [])
    assert existing.read_bytes() == b"kept"
    assert not (tmp_path / "target").exists()


def test_claude_identity_needs_its_conversation_member():
    with pytest.raises(native.NativeError, match=f"^{native.FORMAT}$"):
        native.Claude().identity({"companion/x": b"{}\n"})


def test_claude_identity_reports_the_last_recorded_version():
    records = [{"type": "user", "sessionId": CLAUDE, "version": f"2.1.{n}"} for n in (1, 2, 3)]
    assert native.Claude().identity({"conversation.jsonl": cases.jsonl(records)}) == (CLAUDE, "2.1.3")


def test_capture_counts_every_outcome_by_harness(tmp_path):
    writer, _ = captured(tmp_path, "claude")
    cwd = tmp_path / "repo"
    writer.capture("claude", tmp_path / "claude-home", CLAUDE, cwd, cases.VERSIONS["claude"], True)
    writer.capture("claude", tmp_path / "claude-home", "missing-session", cwd, cases.VERSIONS["claude"], True)
    writer.capture("claude", tmp_path / "claude-home", CLAUDE, cwd, "2.0.1", True)
    assert writer.metrics() == {native.METRIC: {"claude/captured": 2, "claude/native_resume_unavailable": 2}}


def test_capture_counts_identity_and_size_refusals_under_the_harness(tmp_path, monkeypatch):
    cwd = tmp_path / "repo"
    home = cases.claude_home(tmp_path, cwd, records=cases.claude_records(cases.OTHER["claude"]))
    writer = qualifier(tmp_path)
    writer.capture("claude", home, CLAUDE, cwd, cases.VERSIONS["claude"], True)
    other = cases.claude_home(tmp_path / "old", cwd, records=cases.claude_records(CLAUDE, version="2.0.9"))
    writer.capture("claude", other, CLAUDE, cwd, cases.VERSIONS["claude"], True)
    monkeypatch.setattr(native, "ARCHIVE_MEMBERS", 1)
    large = cases.claude_home(tmp_path / "large", cwd)
    writer.capture("claude", large, CLAUDE, cwd, cases.VERSIONS["claude"], True)
    assert writer.metrics() == {native.METRIC: {"claude/native_resume_unavailable": 3}}


def test_restore_with_native_resume_disabled_counts_under_the_harness(tmp_path):
    committed(tmp_path, "codex")
    disabled = native.Qualifier(reader(tmp_path).validator, qualified={})
    disabled.restore("ckpt-1", tmp_path / "fresh", tmp_path, cases.VERSIONS["codex"])
    assert disabled.metrics() == {native.METRIC: {"codex/native_resume_unavailable": 1}}


def test_checkpoint_without_reconstruction_is_a_harness_mismatch(tmp_path):
    writer, result = captured(tmp_path, "claude")
    manifest = cases.manifest_for("ckpt-1", "claude")
    del manifest["reconstruction"]
    with pytest.raises(native.NativeError) as raised:
        writer.checkpoint(result, manifest)
    assert str(raised.value) == "harness_mismatch"


def test_checkpoint_without_authority_stores_nothing(tmp_path):
    writer, result = captured(tmp_path, "claude")
    manifest = cases.manifest_for("ckpt-1", "claude")
    del manifest["authority"]
    verdict = writer.checkpoint(result, manifest)
    assert not verdict.authoritative
    assert writer.validator.store.recorded("ckpt-1.native_session") is None


def test_an_index_path_under_the_resolved_home_is_found_through_a_linked_home(tmp_path):
    real = cases.codex_home(tmp_path / "real")
    alias = tmp_path / "alias"
    alias.symlink_to(real)
    result = qualifier(tmp_path).capture("codex", alias, CODEX, tmp_path, cases.VERSIONS["codex"], True)
    assert result.session_id == CODEX


TOKEN = "ghp_" + "Z9" * 18


def planted(root: Path) -> tuple[native.Qualifier, object]:
    cwd = root / "repo"
    common = {"sessionId": CLAUDE, "version": "2.1.296", "cwd": cases.LOGICAL_CWD}
    printed = {"type": "user", **common, "toolUseResult": {"stdout": f"token {TOKEN}"}}
    home = cases.claude_home(root, cwd, [*cases.claude_records(CLAUDE), printed])
    writer = qualifier(root)
    return writer, writer.capture("claude", home, CLAUDE, cwd, cases.VERSIONS["claude"], True)


def test_a_capture_keeps_no_secret_a_tool_printed_and_counts_it(tmp_path):
    writer, result = planted(tmp_path)
    assert TOKEN.encode() not in result.material()
    assert result.fields()["secret_redactions"] == 1
    assert cases.commit(writer, result, "ckpt-1", "claude").authoritative
    stored = [path.read_bytes() for path in (tmp_path / "durable").rglob("*") if path.is_file()]
    assert stored
    assert all(TOKEN.encode() not in data for data in stored)
    store = writer.validator.store
    assert json.loads(store.get_range(store.recorded("ckpt-1")))["secret_redactions"] == 1


def test_a_redacted_conversation_restores_with_its_marker(tmp_path):
    writer, result = planted(tmp_path)
    assert cases.commit(writer, result, "ckpt-1", "claude").authoritative
    home, cwd = tmp_path / "fresh", tmp_path / "fresh-repo"
    restored = reader(tmp_path).restore("ckpt-1", home, cwd, cases.VERSIONS["claude"])
    assert restored == native.Resumed("claude", CLAUDE, native.HARNESSES["claude"].command(CLAUDE))
    placed = (home / "projects" / native.encoded(cwd) / f"{CLAUDE}.jsonl").read_text()
    assert "token [REDACTED:github_token]" in placed
    assert TOKEN not in placed


@pytest.mark.parametrize(("slack", "available"), [(0, True), (-1, False)])
def test_the_archive_bound_applies_to_the_redacted_files(tmp_path, monkeypatch, slack, available):
    _, first = planted(tmp_path / "first")
    kept = sum(len(data) for _, data in first.files)
    monkeypatch.setattr(native, "ARCHIVE_BYTES", kept + slack)
    _, result = planted(tmp_path / "second")
    assert isinstance(result, native.NativeCapture) is available
    if not available:
        assert result.cause == native.TOO_LARGE


def test_a_key_split_across_records_makes_the_capture_unavailable(tmp_path):
    cwd = tmp_path / "repo"
    common = {"sessionId": CLAUDE, "version": "2.1.296", "cwd": cases.LOGICAL_CWD}
    opened = {"type": "user", **common, "toolUseResult": {"stdout": "-----BEGIN RSA PRIVATE" + " KEY-----\nAAAA"}}
    home = cases.claude_home(tmp_path, cwd, [*cases.claude_records(CLAUDE), opened])
    result = qualifier(tmp_path).capture("claude", home, CLAUDE, cwd, cases.VERSIONS["claude"], True)
    assert result == native.Unavailable("claude", "secret_unredactable")


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    first, second = cases.run_case(case), cases.run_case(case)
    assert first == second
    assert first["state"] == "passed", json.dumps(first, indent=2, sort_keys=True)
    path = EVIDENCE / f"{case}-result.json"
    committed_record = json.loads(path.read_text()) if path.exists() else None
    assert committed_record == first, json.dumps(first, indent=2, sort_keys=True)
