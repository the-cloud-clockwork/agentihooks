import hashlib
import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from hooks.context import brain_adapter, project_cache
from hooks.context.brain_adapter import BrainEntry
from hooks.context.brain_writer_hook import _drain_outbox, _marker_request, _write_to_outbox
from hooks.context.project_identity import ProjectIdentity
from hooks.context.project_memory import ProjectMemory
from hooks.context.project_sessions import record_session
from scripts.swarm_v2 import keyspace

FIXTURE = json.loads((Path(__file__).parent / "fixtures/swarm_v2/keyspace.json").read_text())
SESSION = FIXTURE["session_id"]
IDENTITY = ProjectIdentity(**FIXTURE["identity"])
NAMES = tuple(FIXTURE["installations"])
SECRETS = ("fixture-secret", "fixture-token", "operator")


@pytest.fixture
def world(monkeypatch, tmp_path):
    home = tmp_path / FIXTURE["home"]
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", home)
    monkeypatch.setattr("hooks.context.brain_adapter._HASH_CACHE_FILE", home / "brain_feed_hash")
    monkeypatch.setattr("hooks.common.LOG_FILE", str(tmp_path / "hooks.log"))
    monkeypatch.setenv("AGENTIHOOKS_SWARM", FIXTURE["swarm"])
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", FIXTURE["task"])
    monkeypatch.delenv("BRAIN_PROJECT_SCOPE", raising=False)
    return home


def use_brain(monkeypatch, name: str) -> None:
    monkeypatch.setattr("hooks.config.BRAIN_URL", FIXTURE["installations"][name]["brain_url"])


def install(home: Path) -> keyspace.Installation:
    return keyspace.installation(home, datetime.fromisoformat(FIXTURE["installed_at"]))


def marker(at: str | None = FIXTURE["marker_at"]) -> dict:
    found = {**FIXTURE["marker"], "attrs": {}}
    return {**found, "at": at} if at else found


def scope() -> dict:
    return {**IDENTITY.attributes(), "swarm": FIXTURE["swarm"], "task": FIXTURE["task"]}


def refresh(lesson: str) -> None:
    project_cache.store_feed([BrainEntry("hot-arcs", "Arcs", lesson)])
    with patch("hooks._async.fork_and_call") as fork:
        project_cache.project_context(SESSION)
    with patch(
        "hooks.context.project_memory.VaultProjectSource.fetch",
        return_value=ProjectMemory(IDENTITY.project, lessons=[lesson]),
    ):
        project_cache.refresh_project_cache(*fork.call_args.args[1:])


def context() -> str:
    with patch("hooks._async.fork_and_call"):
        return project_cache.project_context(SESSION) or ""


def files(home: Path) -> dict[str, bytes]:
    return {str(path.relative_to(home)): path.read_bytes() for path in sorted(home.rglob("*")) if path.is_file()}


def run_installation(monkeypatch, home: Path, name: str) -> dict:
    record = install(home)
    use_brain(monkeypatch, name)
    record_session(SESSION, IDENTITY)
    refresh(f"{name} lesson")
    project_cache.defer_project_context(SESSION, f"{name} pending")
    brain_adapter._save_persisted_hash(f"{name}-hash")
    _, key = _marker_request({**marker(), "scope": scope()}, SESSION)
    snapshot = files(home)
    result = {
        "installation": record.installation_id,
        "brain": brain_adapter.brain_id(),
        "marker_key": key,
        "caches": sorted(name for name in snapshot if keyspace.NAMESPACED.fullmatch(Path(name).name)),
        "context": context(),
        "pending": project_cache.take_project_context(SESSION),
        "publish_hash": brain_adapter._load_persisted_hash(),
        "secret_free": not any(secret.encode() in body for body in snapshot.values() for secret in SECRETS),
        "mismatches": project_cache.cache_scope_mismatch_total(),
    }
    shutil.move(home, home.with_name(f"{home.name}-{name}"))
    return result


def paired(monkeypatch, home: Path) -> dict[str, dict]:
    return {name: run_installation(monkeypatch, home, name) for name in NAMES}


def test_paired_installations_on_the_same_path_get_independent_identities(world, monkeypatch):
    runs = paired(monkeypatch, world)
    personal, swarm = runs["personal"], runs["swarm"]
    assert personal["installation"] != swarm["installation"]
    assert personal["brain"] != swarm["brain"]
    assert personal["marker_key"] != swarm["marker_key"]
    assert personal["caches"] and swarm["caches"]
    assert not set(personal["caches"]) & set(swarm["caches"])
    for name, run in runs.items():
        assert f"{name} lesson" in run["context"]
        assert run["pending"] == f"{name} pending"
        assert run["publish_hash"] == f"{name}-hash"
        assert run["secret_free"]
        assert run["mismatches"] == 0


def test_a_second_independent_fixture_is_independent_too(world, monkeypatch, tmp_path):
    first = paired(monkeypatch, world)
    second_home = tmp_path / "second" / FIXTURE["home"]
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", second_home)
    monkeypatch.setattr("hooks.context.brain_adapter._HASH_CACHE_FILE", second_home / "brain_feed_hash")
    second = paired(monkeypatch, second_home)
    identities = [run["installation"] for run in (*first.values(), *second.values())]
    assert len(set(identities)) == 4
    assert first["swarm"]["brain"] == second["swarm"]["brain"]
    assert first["swarm"]["marker_key"] != second["swarm"]["marker_key"]
    assert all(run["mismatches"] == 0 and run["secret_free"] for run in second.values())


def test_two_brains_on_one_installation_keep_separate_caches(world, monkeypatch):
    install(world)
    record_session(SESSION, IDENTITY)
    for name in NAMES:
        use_brain(monkeypatch, name)
        refresh(f"{name} lesson")
    use_brain(monkeypatch, "personal")
    assert "personal lesson" in context()
    assert "swarm lesson" not in context()
    use_brain(monkeypatch, "swarm")
    assert "swarm lesson" in context()
    assert "personal lesson" not in context()
    assert project_cache.cache_scope_mismatch_total() == 0


def test_public_behaviour_matches_the_preceding_code(world, monkeypatch):
    golden = json.loads((Path(__file__).parent / "fixtures/swarm_v2/keyspace-golden.json").read_text())
    monkeypatch.delenv("AGENTIHOOKS_SWARM")
    monkeypatch.setattr("hooks.config.BRAIN_SOURCE_TYPE", "none")
    with patch("hooks.context.project_sessions.enabled", return_value=False):
        record_session(SESSION, IDENTITY)
    project_cache.store_feed([BrainEntry("hot-arcs", "Arcs", "golden")])
    with patch("hooks._async.fork_and_call") as fork:
        assert project_cache.project_context(SESSION) == golden["empty_context"]
    with patch(
        "hooks.context.project_memory.VaultProjectSource.fetch",
        return_value=ProjectMemory(IDENTITY.project, lessons=["golden lesson"]),
    ):
        project_cache.refresh_project_cache(*fork.call_args.args[1:])
    assert context() == golden["context"]
    assert _marker_request({**marker(None), "scope": None}, SESSION)[1] == golden["legacy_marker_key"]


def test_the_session_index_keeps_the_canonical_project(world):
    from hooks.context.project_sessions import _index_path, lookup

    record_session(SESSION, IDENTITY)
    assert lookup(SESSION) == IDENTITY
    with _index_path().open("a") as stream:
        stream.write(json.dumps({"session_id": "older", "project": "a", "repo": "a"}) + "\n")
    assert lookup("older") == ProjectIdentity("a", "a", "", "", "", "unknown")


def forge(home: Path, source: str, target: str, kind: str, monkeypatch) -> None:
    paths = {}
    for name in (source, target):
        use_brain(monkeypatch, name)
        brain = project_cache.namespace()
        project = keyspace.Namespace(brain.installation, brain.brain, IDENTITY.project_id)
        paths[name] = {
            "feed": project_cache._feed_path(brain),
            "project-memory": project_cache._cache_path(IDENTITY, project),
        }
    paths[target][kind].write_bytes(paths[source][kind].read_bytes())


def test_a_cache_entry_from_another_brain_cannot_satisfy_a_context_request(world, monkeypatch):
    install(world)
    record_session(SESSION, IDENTITY)
    use_brain(monkeypatch, "personal")
    refresh("personal lesson")
    use_brain(monkeypatch, "swarm")
    project_cache.store_feed([BrainEntry("hot-arcs", "Arcs", "swarm feed")])
    forge(world, "personal", "swarm", "feed", monkeypatch)
    forge(world, "personal", "swarm", "project-memory", monkeypatch)
    use_brain(monkeypatch, "swarm")
    protected = {name: body for name, body in files(world).items() if name.startswith("brain/project-memory/k2-")}
    with patch("hooks._async.fork_and_call") as fork:
        assert "personal lesson" not in (project_cache.project_context(SESSION) or "")
    fork.assert_not_called()
    after = {name: body for name, body in files(world).items() if name.startswith("brain/project-memory/k2-")}
    assert after == protected
    assert project_cache.cache_scope_mismatch_total() == 2
    log = (world.parents[1] / "hooks.log").read_text()
    assert "cache scope mismatch" in log
    assert not any(secret in log for secret in SECRETS)


def test_pending_context_from_another_brain_is_never_delivered(world, monkeypatch):
    install(world)
    use_brain(monkeypatch, "personal")
    project_cache.defer_project_context(SESSION, "personal pending")
    use_brain(monkeypatch, "swarm")
    assert project_cache.take_project_context(SESSION) is None
    use_brain(monkeypatch, "personal")
    assert project_cache.take_project_context(SESSION) == "personal pending"
    assert project_cache.take_project_context(SESSION) is None


def test_a_forged_pending_entry_is_refused_and_kept(world, monkeypatch):
    install(world)
    use_brain(monkeypatch, "personal")
    project_cache.defer_project_context(SESSION, "personal pending")
    source = project_cache._pending_path(SESSION, project_cache.namespace())
    use_brain(monkeypatch, "swarm")
    target = project_cache._pending_path(SESSION, project_cache.namespace())
    target.write_bytes(source.read_bytes())
    assert project_cache.take_project_context(SESSION) is None
    assert target.read_bytes() == source.read_bytes()
    assert project_cache.cache_scope_mismatch_total() == 1


def test_a_publish_hash_from_another_brain_reads_as_empty(world, monkeypatch):
    install(world)
    use_brain(monkeypatch, "personal")
    brain_adapter._save_persisted_hash("personal-hash")
    use_brain(monkeypatch, "swarm")
    assert brain_adapter._load_persisted_hash() == ""
    (world / "brain_feed_hash").write_text(json.dumps({"hash": "legacy"}))
    assert brain_adapter._load_persisted_hash() == ""


def test_an_invalid_brain_url_is_never_hashed_or_echoed(world, monkeypatch):
    monkeypatch.setattr("hooks.config.BRAIN_URL", "ftp://operator:fixture-secret@brain")
    assert brain_adapter.brain_id() == "invalid"
    monkeypatch.setattr("hooks.config.BRAIN_URL", "")
    monkeypatch.setattr("hooks.config.BRAIN_SOURCE_TYPE", "file")
    monkeypatch.setattr("hooks.config.BRAIN_SOURCE_PATH", str(world / "feed"))
    assert brain_adapter.brain_id() == keyspace.brain_identity(path=str(world / "feed"))
    monkeypatch.setattr("hooks.config.BRAIN_SOURCE_TYPE", "none")
    assert brain_adapter.brain_id() == "none"


def test_a_tampered_installation_record_is_refused(world):
    world.mkdir(parents=True)
    for record in ({"installation_id": "personal", "created_at": FIXTURE["installed_at"]}, {"x": 1}, []):
        (world / keyspace.INSTALLATION_FILE).write_text(json.dumps(record))
        with pytest.raises(ValueError, match="^Invalid installation record$"):
            keyspace.installation(world)
    (world / keyspace.INSTALLATION_FILE).write_text(json.dumps({"installation_id": "inst-" + "0" * 32}))
    with pytest.raises(ValueError, match="^Invalid installation record$"):
        keyspace.installation(world)


def legacy_files(directory: Path, count: int) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    stale = {"feed_hash": "x", "at": 9e18, "memory": {"project": "agentihooks", "lessons": ["stale lesson"]}}
    made = [directory / "feed.json"]
    made[0].write_text(json.dumps({"hash": "x", "entries": []}))
    for index in range(count):
        path = directory / (f"pending-{index:024x}.json" if index % 2 else f"{index:024x}.json")
        path.write_text(json.dumps({"context": "stale pending"} if index % 2 else stale))
        made.append(path)
    return made


def test_legacy_entries_coexist_and_are_never_read_as_current(world, monkeypatch):
    install(world)
    record_session(SESSION, IDENTITY)
    use_brain(monkeypatch, "swarm")
    state = world / "brain" / "project-memory"
    legacy = legacy_files(state, 3)
    assert "stale lesson" not in context()
    assert project_cache.take_project_context(SESSION) is None
    assert all(path.exists() for path in legacy)
    refresh("swarm lesson")
    assert "swarm lesson" in context()
    assert not any(path.exists() for path in legacy)
    assert project_cache.cache_scope_mismatch_total() == 0


def test_the_legacy_sweep_is_bounded_and_keeps_everything_else(tmp_path):
    legacy = legacy_files(tmp_path, 40)
    kept = [
        tmp_path / f"k2-feed-{'0' * 32}.json",
        tmp_path / "cache-scope-mismatches.json",
        tmp_path / "pending-short.json",
    ]
    for path in kept:
        path.write_text("{}")
    assert keyspace.sweep_legacy(tmp_path) == 32
    assert sum(path.exists() for path in legacy) == 9
    assert keyspace.sweep_legacy(tmp_path, limit=100) == 9
    assert all(path.exists() for path in kept)
    assert keyspace.sweep_legacy(tmp_path / "missing") == 0


def test_removal_counts_only_files_it_removed(tmp_path):
    present = tmp_path / "present"
    present.write_text("x")
    assert keyspace._remove([tmp_path / "gone", present, tmp_path / "gone-too"]) == 1
    assert not present.exists()


def test_markers_before_the_cutover_keep_the_legacy_key(world, monkeypatch):
    install(world)
    use_brain(monkeypatch, "swarm")
    content = FIXTURE["marker"]["content"]
    legacy = uuid.uuid5(uuid.NAMESPACE_URL, f"{SESSION}-lesson-{content}").hex[:32]
    assert _marker_request({**marker(FIXTURE["legacy_marker_at"]), "scope": scope()}, SESSION)[1] == legacy
    assert _marker_request({**marker(None), "scope": scope()}, SESSION)[1] == legacy
    assert _marker_request({**marker("not a time"), "scope": scope()}, SESSION)[1] == legacy
    current = _marker_request({**marker(FIXTURE["installed_at"]), "scope": scope()}, SESSION)[1]
    assert current != legacy
    assert current == _marker_request({**marker(FIXTURE["installed_at"]), "scope": scope()}, SESSION)[1]


def test_markers_after_the_cutover_separate_tasks_projects_and_brains(world, monkeypatch):
    install(world)
    use_brain(monkeypatch, "swarm")
    keys = {_marker_request({**marker(), "scope": scope()}, SESSION)[1]}
    keys.add(_marker_request({**marker(), "scope": {**scope(), "task": "other"}}, SESSION)[1])
    keys.add(_marker_request({**marker(), "scope": {**scope(), "project_id": "local:other"}}, SESSION)[1])
    use_brain(monkeypatch, "personal")
    keys.add(_marker_request({**marker(), "scope": scope()}, SESSION)[1])
    assert len(keys) == 4
    assert all(keyspace.MARKER_KEY.fullmatch(found) for found in keys)


def test_an_outbox_replay_keeps_the_key_of_its_first_post(world, monkeypatch, tmp_path):
    install(world)
    use_brain(monkeypatch, "personal")
    original = _marker_request({**marker(), "scope": scope()}, SESSION)[1]
    outbox = tmp_path / "outbox"
    _write_to_outbox([{**marker(), "scope": scope()}], SESSION, str(outbox))
    legacy = outbox / "00000000T000000-lesson-legacy.json"
    legacy.write_text(json.dumps({"type": "lesson", "content": "old", "session_id": SESSION}))
    use_brain(monkeypatch, "swarm")
    with (
        patch("hooks._brain_http.brain_http_enabled", return_value=True),
        patch("hooks._brain_http.post", return_value={"ok": True}) as post,
    ):
        assert _drain_outbox(str(outbox)) == 2
    keys = [call.kwargs["idempotency_key"] for call in post.call_args_list]
    assert keys == [keyspace.legacy_marker_key(SESSION, "lesson", "old"), original]


def test_rollback_drops_only_new_rebuildable_caches(world, monkeypatch, capsys):
    install(world)
    record_session(SESSION, IDENTITY)
    use_brain(monkeypatch, "swarm")
    refresh("swarm lesson")
    project_cache.defer_project_context(SESSION, "pending")
    state = world / "brain" / "project-memory"
    legacy = legacy_files(state, 2)
    durable = {name: body for name, body in files(world).items() if not keyspace.REBUILDABLE.fullmatch(Path(name).name)}
    assert keyspace.main(["drop", str(state)]) == 0
    assert int(capsys.readouterr().out) == 2
    assert files(world) == durable
    assert all(path.exists() for path in legacy)
    assert project_cache.take_project_context(SESSION) == "pending"
    assert keyspace.main(["drop"]) == 2
    assert keyspace.main(["purge", str(state)]) == 2
    assert keyspace.drop_namespaced(world / "missing") == 0


def test_marker_keys_follow_the_event_time_scope_across_stops(world, monkeypatch):
    from hooks.context.project_sessions import record_scope

    install(world)
    use_brain(monkeypatch, "swarm")
    record_scope(SESSION, scope(), "2026-10-08T12:10:00+00:00")
    first = _marker_request(marker(), SESSION)[1]
    with patch("hooks.context.project_sessions.enabled", return_value=False):
        record_session(SESSION, ProjectIdentity("other", "other", project_id="local:other"))
    record_scope(SESSION, {**scope(), "task": "later"}, "2026-10-08T13:00:00+00:00")
    assert _marker_request(marker(), SESSION)[1] == first
    later = _marker_request(marker("2026-10-08T13:10:00+00:00"), SESSION)[1]
    assert later != first


def test_one_text_under_two_tasks_of_one_session_is_two_markers_and_reposts_nothing(world, monkeypatch, tmp_path):
    from hooks.context import brain_writer_hook
    from hooks.context.project_sessions import record_scope

    install(world)
    use_brain(monkeypatch, "swarm")
    monkeypatch.setattr("hooks.config.BRAIN_WRITER_ENABLED", True)
    monkeypatch.setattr("hooks.config.BRAIN_WRITER_MAX_MARKERS", 20)
    monkeypatch.setattr("hooks.config.BRAIN_WRITER_OUTBOX", str(tmp_path / "outbox"))
    record_scope(SESSION, {**scope(), "task": "one"}, "2026-10-08T10:00:00+00:00")
    record_scope(SESSION, {**scope(), "task": "two"}, "2026-10-08T12:40:00+00:00")
    text = f"<!-- @lesson -->{FIXTURE['marker']['content']}<!-- @/lesson -->"
    times = (FIXTURE["legacy_marker_at"], FIXTURE["marker_at"], "2026-10-08T12:50:00+00:00")
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        "".join(json.dumps({"type": "assistant", "timestamp": at, "message": {"content": text}}) + "\n" for at in times)
    )
    with (
        patch("hooks._brain_http.brain_http_enabled", return_value=True),
        patch("hooks._brain_http.post", return_value={"ok": True}) as post,
    ):
        brain_writer_hook.write_markers(SESSION, str(transcript))
        brain_writer_hook.write_markers(SESSION, str(transcript))
    keys = [call.kwargs["idempotency_key"] for call in post.call_args_list]
    assert keys[:3] == keys[3:]
    assert keys[0] == keyspace.legacy_marker_key(SESSION, "lesson", FIXTURE["marker"]["content"])
    assert len(set(keys[:3])) == 3


def test_hook_redis_keys_are_scoped_by_installation(world, tmp_path, monkeypatch):
    from hooks._redis import _KEY_PREFIX, redis_key
    from hooks.context import controls_toggle

    record = install(world)
    first = redis_key("file_cache", SESSION)
    assert first == f"{_KEY_PREFIX}:{record.installation_id}:file_cache:{SESSION}"
    assert controls_toggle._global_key() == f"{_KEY_PREFIX}:{record.installation_id}:controls_disabled:_global"
    other = tmp_path / "other" / FIXTURE["home"]
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", other)
    assert redis_key("file_cache", SESSION) != first
    assert redis_key("file_cache", SESSION) == redis_key("file_cache", SESSION)


class FakeRedis:
    def __init__(self):
        self.store = {}

    def set(self, key, value):
        self.store[key] = value

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.store.pop(key, None)


def test_the_controls_switch_lives_under_the_installation_key(world, monkeypatch):
    from hooks.context import controls_toggle

    install(world)
    fake = FakeRedis()
    monkeypatch.setattr(controls_toggle, "get_redis", lambda: fake)
    monkeypatch.setattr(controls_toggle, "_FLAG_DIR", world / "controls_flags")
    monkeypatch.setattr(controls_toggle, "_GLOBAL_FLAG", world / "controls_flags" / "active.flag")
    controls_toggle.set_controls_disabled("owner-session")
    assert fake.store == {controls_toggle._global_key(): "owner-session"}
    controls_toggle._GLOBAL_FLAG.unlink()
    assert controls_toggle._read_owner() == "owner-session"
    controls_toggle.clear_controls_disabled(force=True)
    assert fake.store == {}
    assert controls_toggle._read_owner() is None


def test_brain_status_reports_cache_scope_mismatches(world, monkeypatch):
    install(world)
    use_brain(monkeypatch, "personal")
    project_cache.defer_project_context(SESSION, "personal pending")
    source = project_cache._pending_path(SESSION, project_cache.namespace())
    use_brain(monkeypatch, "swarm")
    project_cache._pending_path(SESSION, project_cache.namespace()).write_bytes(source.read_bytes())
    project_cache.take_project_context(SESSION)
    assert brain_adapter.get_status()["cache_scope_mismatch_total"] == 1


def test_concurrent_installation_creation_keeps_the_first_record(tmp_path, monkeypatch):
    winner = {"installation_id": "inst-" + "a" * 32, "created_at": FIXTURE["installed_at"]}
    target = tmp_path / keyspace.INSTALLATION_FILE

    def lose(source, destination):
        Path(destination).write_text(json.dumps(winner))
        raise FileExistsError(destination)

    monkeypatch.setattr("scripts.swarm_v2.keyspace.os.link", lose)
    assert keyspace.installation(tmp_path) == keyspace.Installation(**winner)
    assert [path.name for path in tmp_path.iterdir() if "installation" in path.name] == [target.name]
    monkeypatch.undo()
    assert keyspace.installation(tmp_path).installation_id == winner["installation_id"]


def test_a_new_installation_records_its_time_and_a_random_id(tmp_path):
    first = install(tmp_path / "one")
    second = install(tmp_path / "two")
    assert first.created_at == second.created_at == FIXTURE["installed_at"]
    assert keyspace.INSTALLATION_ID.fullmatch(first.installation_id)
    assert first.installation_id != second.installation_id
    assert keyspace.installation(tmp_path / "one") == first
    fresh = keyspace.installation(tmp_path / "three")
    assert keyspace.instant(fresh.created_at).tzinfo is not None


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("https://brain.example/api", "HTTPS://Brain.Example:443/api/"),
        ("https://brain.example/api", "https://user:pass@brain.example/api?token=x#feed"),
        ("http://brain.example", "http://brain.example:80"),
        ("http://[::1]:8080/", "http://[::1]:8080"),
    ],
)
def test_equal_brains_normalize_to_one_identity(left, right):
    assert keyspace.brain_identity(url=left) == keyspace.brain_identity(url=right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("https://brain.example/api", "http://brain.example/api"),
        ("https://brain.example/api", "https://brain.example:8443/api"),
        ("https://brain.example/api", "https://brain.example/other"),
        ("https://brain.example", "https://other.example"),
    ],
)
def test_different_brains_keep_different_identities(left, right):
    assert keyspace.brain_identity(url=left) != keyspace.brain_identity(url=right)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:32]


@pytest.mark.parametrize(
    ("url", "normalized"),
    [
        ("HTTPS://Brain.Example:443/api/", "https://brain.example/api"),
        ("https://operator:fixture-secret@brain.example/api?token=fixture-token#feed", "https://brain.example/api"),
        ("http://[::1]:8080/", "http://[::1]:8080"),
        ("https://brain.example/indeX", "https://brain.example/indeX"),
    ],
)
def test_brain_identities_hash_the_secret_free_normalized_url(url, normalized):
    assert keyspace.brain_identity(url=url) == "url-" + digest(normalized)


def test_brain_identities_carry_no_secret_and_name_their_source(tmp_path):
    found = keyspace.brain_identity(url="https://operator:fixture-secret@brain.example/?token=fixture-token")
    assert not any(secret in found for secret in SECRETS)
    path = keyspace.brain_identity(path=str(tmp_path / "a" / ".." / "feed"))
    assert path == "file-" + digest(str((tmp_path / "feed").resolve()))
    assert keyspace.brain_identity() == "none"
    for url in ("ftp://brain.example", "https://", "brain.example"):
        with pytest.raises(ValueError, match="^Invalid brain URL$"):
            keyspace.brain_identity(url=url)
    with pytest.raises(ValueError, match="out of range"):
        keyspace.brain_identity(url="https://brain.example:99999")


def test_keys_are_versioned_deterministic_and_scoped():
    scope = keyspace.Namespace("inst-" + "a" * 32, "url-" + "b" * 32, "github.com/o/r")
    found = keyspace.key(scope, "feed", "x", "y")
    canonical = (
        '[{"brain":"url-' + "b" * 32 + '","format":"2","generation":"","installation":"inst-' + "a" * 32 + '",'
        '"policy":"1","project":"github.com/o/r"},"feed",["x","y"]]'
    )
    assert found == "k2-feed-" + digest(canonical)
    variants = {
        keyspace.key(scope, "feed", "y", "x"),
        keyspace.key(scope, "pending", "x", "y"),
        keyspace.key(keyspace.Namespace(scope.installation, scope.brain), "feed", "x", "y"),
        keyspace.key(keyspace.Namespace(scope.installation, scope.brain, scope.project, "2"), "feed", "x", "y"),
        keyspace.key(
            keyspace.Namespace(scope.installation, scope.brain, scope.project, generation="3"), "feed", "x", "y"
        ),
    }
    assert found not in variants and len(variants) == 5
    assert scope.document() == {
        "format": "2",
        "installation": scope.installation,
        "brain": scope.brain,
        "project": "github.com/o/r",
        "policy": "1",
        "generation": "",
    }
    for kind in ("", "Feed", "feed/x", "-feed", "a" * 33):
        with pytest.raises(ValueError, match="^Invalid key kind$"):
            keyspace.key(scope, kind)


def test_marker_keys_hash_the_canonical_namespace_task_and_text():
    scope = keyspace.Namespace("inst-" + "a" * 32, "url-" + "b" * 32, "github.com/o/r")
    raw = (
        '[{"brain":"url-' + "b" * 32 + '","format":"2","generation":"","installation":"inst-' + "a" * 32 + '",'
        '"policy":"1","project":"github.com/o/r"},"marker","s","lesson","t","text"]'
    )
    assert keyspace.marker_key(scope, "s", "lesson", "t", "text") == uuid.uuid5(uuid.NAMESPACE_URL, "k2:" + raw).hex
    assert keyspace.legacy_marker_key("s", "lesson", "text") == uuid.uuid5(uuid.NAMESPACE_URL, "s-lesson-text").hex


def test_admission_compares_the_whole_namespace_and_kind():
    scope = keyspace.Namespace("inst-" + "a" * 32, "url-" + "b" * 32)
    document = keyspace.stamp(scope, "feed", {"hash": "h", "namespace": "forged"})
    assert document == {"hash": "h", "namespace": scope.document(), "kind": "feed"}
    assert keyspace.admits(document, scope, "feed")
    assert not keyspace.admits(document, scope, "pending")
    assert not keyspace.admits(document, keyspace.Namespace(scope.installation, "url-" + "c" * 32), "feed")
    assert not keyspace.admits([], scope, "feed")


def test_the_cutover_is_inclusive_and_needs_a_time():
    record = keyspace.Installation("inst-" + "a" * 32, "2026-10-08T12:00:00+00:00")
    assert keyspace.current("2026-10-08T12:00:00Z", record)
    assert keyspace.current("2026-10-08T12:00:00", record)
    assert not keyspace.current("2026-10-08T11:59:59.999+00:00", record)
    assert not keyspace.current("2026-10-08T13:00:00+02:00", record)
    assert keyspace.current("2026-10-08T14:00:00+02:00", record)
    assert not keyspace.current(None, record) and not keyspace.current("", record)


def test_live_marker_keys_use_the_installation_brain_and_event_time_scope(world, monkeypatch):
    record = install(world)
    use_brain(monkeypatch, "swarm")
    content = FIXTURE["marker"]["content"]
    namespace = keyspace.Namespace(record.installation_id, brain_adapter.brain_id(), IDENTITY.project_id)
    expected = keyspace.marker_key(namespace, SESSION, "lesson", FIXTURE["task"], content)
    assert _marker_request({**marker(), "scope": scope()}, SESSION)[1] == expected
    bare = keyspace.Namespace(record.installation_id, brain_adapter.brain_id())
    assert _marker_request({**marker(), "scope": {}}, SESSION)[1] == keyspace.marker_key(
        bare, SESSION, "lesson", "", content
    )
    assert _marker_request({**marker(), "idempotency_key": "None", "scope": {}}, SESSION)[1] != "None"


def test_cache_paths_hash_their_namespace_kind_and_identity(world, monkeypatch):
    record = install(world)
    use_brain(monkeypatch, "swarm")
    state = world / "brain" / "project-memory"
    brain = project_cache.namespace()
    assert brain == keyspace.Namespace(record.installation_id, brain_adapter.brain_id())
    scoped = keyspace.Namespace(brain.installation, brain.brain, IDENTITY.project_id)
    assert project_cache._feed_path(brain) == state / f"{keyspace.key(brain, 'feed')}.json"
    assert project_cache._pending_path(SESSION, brain) == state / f"{keyspace.key(brain, 'pending', SESSION)}.json"
    assert project_cache._cache_path(IDENTITY, scoped) == state / (
        keyspace.key(scoped, "project-memory", IDENTITY.remote) + ".json"
    )
    local = ProjectIdentity("alpha", "alpha")
    assert project_cache._cache_path(local, scoped) == state / f"{keyspace.key(scoped, 'project-memory', 'alpha')}.json"


def test_a_context_request_starts_one_scoped_background_refresh(world, monkeypatch):
    record = install(world)
    record_session(SESSION, IDENTITY)
    use_brain(monkeypatch, "swarm")
    project_cache.store_feed([BrainEntry("hot-arcs", "Arcs", "x")])
    with patch("hooks._async.fork_and_call") as fork:
        project_cache.project_context(SESSION)
    feed = json.loads(project_cache._feed_path(project_cache.namespace()).read_text())
    scoped = keyspace.Namespace(record.installation_id, brain_adapter.brain_id(), IDENTITY.project_id)
    fork.assert_called_once_with(
        project_cache.refresh_project_cache,
        IDENTITY,
        feed["entries"],
        feed["hash"],
        scoped,
        timeout_sec=180,
        task_name="brain_project",
    )


def test_each_refused_read_is_counted_by_kind_and_logged_without_the_brain(world, monkeypatch):
    install(world)
    use_brain(monkeypatch, "personal")
    project_cache.defer_project_context(SESSION, "personal pending")
    source = project_cache._pending_path(SESSION, project_cache.namespace())
    use_brain(monkeypatch, "swarm")
    target = project_cache._pending_path(SESSION, project_cache.namespace())
    target.write_bytes(source.read_bytes())
    assert project_cache.take_project_context(SESSION) is None
    assert project_cache.take_project_context(SESSION) is None
    state = world / "brain" / "project-memory"
    assert json.loads((state / "cache-scope-mismatches.json").read_text()) == {"pending": 2}
    assert (state / "cache-scope-mismatches.json.lock").exists()
    entries = [json.loads(line) for line in (world.parents[1] / "hooks.log").read_text().splitlines()]
    refused = [entry for entry in entries if entry["message"] == "brain_project: cache scope mismatch"]
    assert [entry["payload"] for entry in refused] == [{"kind": "pending"}, {"kind": "pending"}]


def test_a_fresh_installation_records_utc_and_the_usage_goes_to_stderr(tmp_path, capsys):
    assert keyspace.installation(tmp_path).created_at.endswith("+00:00")
    assert keyspace.main(["drop"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "usage: python -m scripts.swarm_v2.keyspace drop <cache directory>\n"
