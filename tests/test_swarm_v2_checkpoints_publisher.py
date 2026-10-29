import io
import json
import tarfile
from pathlib import Path

import pytest

from scripts.swarm_v2 import contracts
from scripts.swarm_v2.artifacts.base import ABSENT, VERIFIED, ArtifactRef
from scripts.swarm_v2.checkpoints import model, native, publisher, redaction
from tests.sv2_ckp02_cases import MEMORY, scratch
from tests.sv2_ckp04_cases import LostAck, checkpoint, files, make, pointers, run_case

pytestmark = pytest.mark.unit

EVIDENCE = Path(__file__).parents[1] / "evidence" / "SV2-CKP-04"


@pytest.fixture
def root(tmp_path):
    return tmp_path / "durable"


def test_publish_promotes_verified_checkpoints_in_sequence(root):
    pub = make(root)
    assert pub.latest() == ""
    assert pub.sequence() == 0
    first = pub.publish(*checkpoint(1))
    assert first.promoted is True
    assert first.reason == "promoted"
    assert first.verdict.authoritative
    assert pub.latest() == "ckpt-g2-s1"
    second = pub.publish(*checkpoint(2))
    assert (second.promoted, second.reason) == (True, "promoted")
    assert pub.latest() == "ckpt-g2-s2"
    assert pub.sequence() == 2
    assert pointers(root) == [
        "s1/t1/pointers/0000000002/0000000001.json",
        "s1/t1/pointers/0000000002/0000000002.json",
    ]
    assert pub.metrics() == {"checkpoint_publish_failures": {}}


def test_the_committed_manifest_lists_every_uploaded_material(root):
    pub = make(root)
    doc, materials = checkpoint(1)
    pub.publish(doc, materials)
    store = pub.validator.store
    committed = json.loads(store.get_range(store.recorded("ckpt-g2-s1")))
    ref = ArtifactRef.of(materials[model.TRANSCRIPT])
    assert committed["inventory"] == {model.TRANSCRIPT: {"sha256": ref.sha256, "size": ref.size}}
    assert committed["artifact_hashes"] == {model.TRANSCRIPT: "sha256:" + ref.sha256}
    assert store.recorded("ckpt-g2-s1.normalized_transcript") == ref
    pointer = {
        "checkpoint_id": "ckpt-g2-s1",
        "checkpoint_sequence": 1,
        "execution_id": "e1",
        "generation": 2,
        "manifest": {"sha256": store.recorded("ckpt-g2-s1").sha256, "size": store.recorded("ckpt-g2-s1").size},
    }
    assert (root / pointers(root)[0]).read_bytes() == json.dumps(pointer, sort_keys=True).encode()


def test_listing_keeps_carried_roles_and_adds_each_material():
    carried = {"sha256": "1" * 64, "size": 3}
    doc = {"checkpoint_id": "c", "inventory": {"other": carried}, "artifact_hashes": {"other": "sha256:" + "1" * 64}}
    ref = ArtifactRef.of(b"x")
    assert publisher._listed(doc, {model.TRANSCRIPT: b"x"}) == {
        "checkpoint_id": "c",
        "inventory": {"other": carried, model.TRANSCRIPT: {"sha256": ref.sha256, "size": 1}},
        "artifact_hashes": {"other": "sha256:" + "1" * 64, model.TRANSCRIPT: "sha256:" + ref.sha256},
    }
    assert publisher._listed({"checkpoint_id": "c"}, {model.TRANSCRIPT: b"x"}) == {
        "checkpoint_id": "c",
        "inventory": {model.TRANSCRIPT: {"sha256": ref.sha256, "size": 1}},
        "artifact_hashes": {model.TRANSCRIPT: "sha256:" + ref.sha256},
    }


def test_reads_name_an_exact_length_as_an_object_store_range_needs(root):
    class Ranged(LostAck):
        def read(self, key, start, length):
            assert type(length) is int
            return self.inner.read(key, start, length)

    pub = make(root, backend=Ranged(root))
    assert pub.publish(*checkpoint(1)).promoted
    pub.store.put("ckpt-g2-s2.normalized_transcript", b"orphan")
    assert pub.latest() == "ckpt-g2-s1"
    assert pub.reclaim("ckpt-g2-s2") == ["ckpt-g2-s2.normalized_transcript"]


def test_an_older_generation_is_refused_once_a_newer_generation_has_written(root):
    stale = make(root, "e1", 2)
    assert stale.publish(*checkpoint(1)).promoted
    newer = make(root, "e2", 3)
    assert newer.publish(*checkpoint(1, 3, "e2")).promoted
    before = files(root)
    refused = stale.publish(*checkpoint(2))
    assert (refused.promoted, refused.reason, refused.verdict) == (False, "superseded_generation", None)
    assert files(root) == before
    assert stale.metrics() == {"checkpoint_publish_failures": {"superseded_generation": 1}}
    assert newer.latest() == make(root, "e3", 4).latest() == "ckpt-g3-s1"
    assert stale.latest() == ""


def test_a_generation_superseded_during_upload_never_commits_its_manifest(root):
    stale = make(root, "e1", 2)
    newer = make(root, "e2", 3)
    put = stale.store.put

    def interleaved(artifact_id, data):
        newer.publish(*checkpoint(1, 3, "e2"))
        return put(artifact_id, data)

    stale.store.put = interleaved
    refused = stale.publish(*checkpoint(1))
    assert (refused.promoted, refused.reason, refused.verdict) == (False, "superseded_generation", None)
    assert stale.store.recorded("ckpt-g2-s1") is None
    assert pointers(root) == ["s1/t1/pointers/0000000003/0000000001.json"]
    assert newer.reclaim("ckpt-g2-s1") == ["ckpt-g2-s1.normalized_transcript"]


def test_a_generation_superseded_during_commit_never_writes_its_pointer(root):
    stale = make(root, "e1", 2)
    newer = make(root, "e2", 3)
    commit = stale.validator.commit

    def interleaved(doc):
        newer.publish(*checkpoint(1, 3, "e2"))
        return commit(doc)

    stale.validator.commit = interleaved
    refused = stale.publish(*checkpoint(1))
    assert (refused.promoted, refused.reason) == (False, "superseded_generation")
    assert refused.verdict.authoritative
    assert pointers(root) == ["s1/t1/pointers/0000000003/0000000001.json"]
    assert make(root, "e3", 4).latest() == "ckpt-g3-s1"
    assert stale.metrics() == {"checkpoint_publish_failures": {"superseded_generation": 1}}
    assert newer.reclaim("ckpt-g2-s1") == ["ckpt-g2-s1", "ckpt-g2-s1.normalized_transcript"]
    assert newer.store.recorded("ckpt-g2-s1") is None


def test_a_generation_superseded_during_its_pointer_write_reports_the_refusal(root):
    stale = make(root, "e1", 2)
    newer = make(root, "e2", 3)
    point = stale._point

    def interleaved(checkpoint_id, sequence):
        newer.publish(*checkpoint(1, 3, "e2"))
        point(checkpoint_id, sequence)

    stale._point = interleaved
    refused = stale.publish(*checkpoint(1))
    assert (refused.promoted, refused.reason) == (False, "superseded_generation")
    assert refused.verdict.authoritative is True
    assert pointers(root) == [
        "s1/t1/pointers/0000000002/0000000001.json",
        "s1/t1/pointers/0000000003/0000000001.json",
    ]
    assert make(root, "e3", 4).latest() == "ckpt-g3-s1"


def test_latest_picks_the_newest_generation_over_a_higher_sequence(root):
    assert make(root, "e1", 2).publish(*checkpoint(9)).promoted
    newer = make(root, "e2", 3)
    assert newer.publish(*checkpoint(1, 3, "e2")).promoted
    assert make(root, "e3", 4).latest() == "ckpt-g3-s1"
    assert newer.sequence() == 1


@pytest.mark.parametrize("sequence", [1, 2])
def test_a_sequence_at_or_below_the_pointer_is_refused(root, sequence):
    pub = make(root)
    assert pub.publish(*checkpoint(2)).promoted
    doc, materials = checkpoint(sequence, payload=b"other")
    doc["checkpoint_id"] = doc["operation_id"] = "ckpt-other"
    before = files(root)
    refused = pub.publish(doc, materials)
    assert (refused.promoted, refused.reason, refused.verdict) == (False, "stale_sequence", None)
    assert files(root) == before
    assert pub.latest() == "ckpt-g2-s2"
    assert pub.publish(doc, materials).reason == "stale_sequence"
    assert pub.metrics() == {"checkpoint_publish_failures": {"stale_sequence": 2}}


def test_a_replayed_publish_promotes_once(root):
    pub = make(root)
    assert pub.publish(*checkpoint(1)).promoted
    again = pub.publish(*checkpoint(1))
    assert (again.promoted, again.reason) == (True, "replayed")
    assert again.verdict.authoritative
    assert len(pointers(root)) == 1
    assert pub.metrics() == {"checkpoint_publish_failures": {}}


def test_a_replay_under_the_same_identity_with_other_content_is_refused(root):
    pub = make(root)
    assert pub.publish(*checkpoint(1)).promoted
    before = files(root)
    refused = pub.publish(*checkpoint(1, payload=b"other"))
    assert (refused.promoted, refused.reason, refused.verdict) == (False, "identity_conflict", None)
    assert files(root) == before
    assert pub.metrics() == {"checkpoint_publish_failures": {"identity_conflict": 1}}


def test_a_replay_whose_object_no_longer_verifies_is_refused(root):
    pub = make(root)
    doc, materials = checkpoint(1)
    assert pub.publish(doc, materials).promoted
    (root / "s1" / "t1" / "objects" / ArtifactRef.of(materials[model.TRANSCRIPT]).sha256).write_bytes(b"torn")
    refused = pub.publish(doc, materials)
    assert (refused.promoted, refused.reason) == (False, "corrupt_object")
    assert refused.verdict.authoritative is False
    assert pub.metrics() == {"checkpoint_publish_failures": {"corrupt_object": 1}}


@pytest.mark.parametrize("sequence", [-1, True, "2", 2.0])
def test_a_sequence_that_is_not_a_count_is_refused(root, sequence):
    pub = make(root)
    doc, materials = checkpoint(1)
    doc["checkpoint_sequence"] = sequence
    refused = pub.publish(doc, materials)
    assert (refused.promoted, refused.reason, refused.verdict) == (False, "invalid_sequence", None)
    assert files(root) == {}


def test_sequence_zero_is_a_first_checkpoint(root):
    pub = make(root)
    assert pub.publish(*checkpoint(0)).promoted
    assert pub.sequence() == 0
    assert pub.latest() == "ckpt-g2-s0"


def test_a_restart_recaptures_an_interrupted_identity_with_other_bytes(root):
    pub = make(root)
    assert pub.publish(*checkpoint(1, payload=b"first")).promoted
    pub.store.put("ckpt-g2-s2.normalized_transcript", b"lost")
    again = pub.publish(*checkpoint(2, payload=b"second"))
    assert (again.promoted, again.reason) == (True, "promoted")
    assert pub.store.stat(ArtifactRef.of(b"lost")) == ABSENT
    assert pub.latest() == "ckpt-g2-s2"


def test_object_store_retry_keeps_uploaded_immutable_material_records(root, monkeypatch):
    pub = make(root)
    pub.store.backend.kind = "object-store"
    doc, materials = checkpoint(1)
    role, material = next(iter(materials.items()))
    reference = pub.store.put(f"{doc['checkpoint_id']}.{role}", material)
    original_remove = pub.store.backend.remove
    removed = []

    def staging_only(key):
        assert "/staging/" in key, "immutable final object must not be deleted"
        removed.append(key)
        original_remove(key)

    monkeypatch.setattr(pub.store.backend, "remove", staging_only)
    assert pub.publish(doc, materials).promoted
    assert pub.store.recorded(f"{doc['checkpoint_id']}.{role}") == reference
    assert pub.validator.verify(doc["checkpoint_id"]).authoritative
    assert removed
    before = files(root)
    assert pub.reclaim(doc["checkpoint_id"]) == []
    assert files(root) == before


def test_a_committed_unpromoted_identity_with_other_bytes_is_refused(root):
    pub = make(root)
    doc, materials = checkpoint(1, payload=b"first")
    pub.store.put("ckpt-g2-s1.normalized_transcript", b"first")
    assert pub.validator.commit(publisher._listed(doc, materials)).authoritative
    before = files(root)
    refused = pub.publish(*checkpoint(1, payload=b"other"))
    assert (refused.promoted, refused.reason, refused.verdict) == (False, "identity_conflict", None)
    assert files(root) == before
    assert pub.publish(doc, materials).reason == "promoted"


def test_a_stale_attempt_meeting_a_newer_identity_is_refused_as_superseded(root):
    assert make(root, "e2", 3).publish(*checkpoint(1, 3, "e2")).promoted
    stale = make(root, "e1", 2)
    doc, materials = checkpoint(1, payload=b"other")
    doc["checkpoint_id"] = doc["operation_id"] = "ckpt-g3-s1"
    refused = stale.publish(doc, materials)
    assert (refused.promoted, refused.reason) == (False, "superseded_generation")


def test_a_document_outside_the_granted_scope_uploads_nothing(root):
    pub = make(root)
    refused = pub.publish(*checkpoint(1, execution="e9"))
    assert (refused.promoted, refused.reason, refused.verdict) == (False, "scope_mismatch", None)
    assert files(root) == {}
    assert pub.metrics() == {"checkpoint_publish_failures": {"scope_mismatch": 1}}


def test_another_execution_of_the_same_generation_is_refused(root):
    assert make(root, "e1", 2).publish(*checkpoint(1)).promoted
    other = make(root, "e9", 2)
    before = files(root)
    refused = other.publish(*checkpoint(2, execution="e9"))
    assert (refused.promoted, refused.reason) == (False, "scope_mismatch")
    assert files(root) == before


def test_an_uncommitted_manifest_is_never_promoted(root):
    pub = make(root)
    assert pub.publish(*checkpoint(1)).promoted
    doc, materials = checkpoint(2)
    doc["status"] = "uploading"
    refused = pub.publish(doc, materials)
    assert (refused.promoted, refused.reason) == (False, "not_committed")
    assert refused.verdict.reason == "not_committed"
    assert pub.latest() == "ckpt-g2-s1"
    assert len(pointers(root)) == 1
    assert pub.metrics() == {"checkpoint_publish_failures": {"not_committed": 1}}


def test_latest_skips_a_pointer_whose_object_no_longer_verifies(root):
    pub = make(root)
    pub.publish(*checkpoint(1))
    doc, materials = checkpoint(2)
    pub.publish(doc, materials)
    ref = ArtifactRef.of(materials[model.TRANSCRIPT])
    (root / "s1" / "t1" / "objects" / ref.sha256).write_bytes(b"torn")
    assert pub.latest() == "ckpt-g2-s1"


def test_latest_never_falls_back_to_an_older_generation(root):
    assert make(root, "e1", 2).publish(*checkpoint(1)).promoted
    newer = make(root, "e2", 3)
    doc, materials = checkpoint(1, 3, "e2")
    newer.publish(doc, materials)
    (root / "s1" / "t1" / "objects" / ArtifactRef.of(materials[model.TRANSCRIPT]).sha256).unlink()
    assert newer.latest() == ""


def test_latest_ignores_a_pointer_naming_other_manifest_content(root):
    pub = make(root)
    pub.publish(*checkpoint(1))
    pub.publish(*checkpoint(2))
    path = root / "s1/t1/pointers/0000000002/0000000002.json"
    pointer = json.loads(path.read_text())
    pointer["manifest"]["size"] += 1
    path.write_text(json.dumps(pointer))
    assert pub.latest() == "ckpt-g2-s1"


def _mutations(root: Path) -> list[tuple[str, str]]:
    backend = LostAck(root)
    pub = make(root, backend=backend)
    pub.publish(*checkpoint(1))
    backend.calls.clear()
    pub.publish(*checkpoint(2))
    return backend.calls


def test_a_lost_acknowledgement_at_every_boundary_keeps_a_verified_latest_and_replays_once(root):
    calls = _mutations(root / "count")
    assert [name for name, _ in calls] == ["write", "move"] * 4 + ["write"]
    assert calls[-1][1] == "s1/t1/pointers/0000000002/0000000002.json"
    for lose_at in range(1, len(calls) + 1):
        attempt = root / f"lose-{lose_at}"
        backend = LostAck(attempt)
        pub = make(attempt, backend=backend)
        assert pub.publish(*checkpoint(1)).promoted
        backend.calls.clear()
        backend.lose_at = lose_at
        with pytest.raises(ConnectionError):
            pub.publish(*checkpoint(2))
        current = pub.latest()
        assert current == ("ckpt-g2-s2" if lose_at == len(calls) else "ckpt-g2-s1"), lose_at
        assert pub.validator.verify(current).authoritative
        backend.lose_at = None
        again = pub.publish(*checkpoint(2))
        assert again.promoted, lose_at
        assert again.reason == ("replayed" if lose_at == len(calls) else "promoted")
        assert pub.latest() == "ckpt-g2-s2"
        assert len(pointers(attempt)) == 2


def _crashed(root: Path, payload: bytes | None = None) -> publisher.Publisher:
    pub = make(root)
    assert pub.publish(*checkpoint(1, payload=b"first")).promoted

    def stopped(doc):
        raise ConnectionError("stopped before the manifest commit")

    commit = pub.validator.commit
    pub.validator.commit = stopped
    with pytest.raises(ConnectionError):
        pub.publish(*checkpoint(2, payload=payload))
    pub.validator.commit = commit
    return pub


def test_a_stop_before_manifest_commit_leaves_reclaimable_orphans_and_the_old_checkpoint(root):
    pub = _crashed(root, b"second")
    store = pub.validator.store
    assert store.recorded("ckpt-g2-s2") is None
    assert pub.latest() == "ckpt-g2-s1"
    assert pub.validator.verify("ckpt-g2-s1").authoritative
    assert pub.reclaim("ckpt-g2-s1") == []
    assert pub.reclaim("ckpt-g2-s2") == ["ckpt-g2-s2.normalized_transcript"]
    assert store.stat(ArtifactRef.of(b"second")) == ABSENT
    assert store.recorded("ckpt-g2-s2.normalized_transcript") is None
    assert store.stat(ArtifactRef.of(b"first")) == VERIFIED
    assert pub.validator.verify("ckpt-g2-s1").authoritative
    assert pub.reclaim("ckpt-g2-s2") == []


def test_reclaim_keeps_an_object_another_checkpoint_still_records(root):
    pub = _crashed(root, b"first")
    assert pub.reclaim("ckpt-g2-s2") == ["ckpt-g2-s2.normalized_transcript"]
    assert pub.validator.store.stat(ArtifactRef.of(b"first")) == VERIFIED
    assert pub.validator.verify("ckpt-g2-s1").authoritative


def test_reclaim_leaves_a_newer_generation_record(root):
    newer = make(root, "e2", 3)
    newer.validator.store.put("ckpt-g3-s1.normalized_transcript", b"newer")
    stale = make(root, "e1", 2)
    assert stale.reclaim("ckpt-g3-s1") == []
    assert newer.validator.store.stat(ArtifactRef.of(b"newer")) == VERIFIED


def test_reclaim_takes_only_the_named_checkpoint_not_a_sibling_id(root):
    pub = make(root)
    pub.store.put("ckpt-g2-s2.normalized_transcript", b"two")
    pub.store.put("ckpt-g2-s20.normalized_transcript", b"twenty")
    pub.store.put("ckpt-g2-s2x", b"other")
    assert pub.reclaim("ckpt-g2-s2") == ["ckpt-g2-s2.normalized_transcript"]
    assert pub.store.recorded("ckpt-g2-s20.normalized_transcript") == ArtifactRef.of(b"twenty")
    assert pub.store.recorded("ckpt-g2-s2x") == ArtifactRef.of(b"other")
    assert pub.store.stat(ArtifactRef.of(b"two")) == ABSENT


def test_reclaim_removes_an_object_two_of_its_records_share_once(root):
    pub = make(root)
    pub.store.put("ckpt-g2-s2.a", b"same")
    pub.store.put("ckpt-g2-s2.b", b"same")
    removed = []
    remove = pub.store.backend.remove
    pub.store.backend.remove = lambda key: (removed.append(key), remove(key))
    assert pub.reclaim("ckpt-g2-s2") == ["ckpt-g2-s2.a", "ckpt-g2-s2.b"]
    assert removed == [
        "s1/t1/artifacts/ckpt-g2-s2.a.json",
        "s1/t1/artifacts/ckpt-g2-s2.b.json",
        "s1/t1/objects/" + ArtifactRef.of(b"same").sha256,
    ]


def test_rollback_lowering_the_write_minor_stops_promotion_and_keeps_the_last_checkpoint(root):
    pub = make(root)
    assert pub.publish(*checkpoint(1)).promoted
    loaded = contracts.load()
    compat = loaded["compat"]["contracts"]
    lowered = {**compat, "checkpoint": {**compat["checkpoint"], "write_minor": 1}}
    rolled = publisher.Publisher(
        model.Validator(pub.store, {**loaded, "compat": {**loaded["compat"], "contracts": lowered}})
    )
    refused = rolled.publish(*checkpoint(2))
    assert (refused.promoted, refused.reason) == (False, "promotion_disabled")
    assert rolled.latest() == pub.latest() == "ckpt-g2-s1"
    assert len(pointers(root)) == 1


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    first, second = run_case(case), run_case(case)
    assert first == second
    assert first["state"] == "passed", json.dumps(first, indent=2, sort_keys=True)
    path = EVIDENCE / f"{case}-result.json"
    committed = json.loads(path.read_text()) if path.exists() else None
    assert committed == first, json.dumps(first, indent=2, sort_keys=True)


def test_package_case_fixtures_run_on_the_memory_filesystem():
    mounts = dict(line.split()[1:3] for line in Path("/proc/mounts").read_text().splitlines())
    assert mounts[str(MEMORY)] == "tmpfs"
    with scratch() as directories:
        assert [path.parent for path in directories] == [MEMORY, MEMORY]
        assert all(path.is_dir() for path in directories)


TOKEN = "ghp_" + "Z9" * 18
QUIET = '{"type": "user", "message": "plain"}\n'


def _native(members: dict[str, str]) -> bytes:
    files = tuple((name, text.encode()) for name, text in members.items())
    return native.NativeCapture("claude", "2.1.3", "s-1", files, "stopped_process").material()


def _published_members(pub: publisher.Publisher) -> dict[str, str]:
    store = pub.validator.store
    data = store.get_range(store.recorded(f"ckpt-g2-s1.{model.NATIVE_SESSION}"))
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        return {info.name: archive.extractfile(info).read().decode() for info in archive.getmembers()}


def _committed(pub: publisher.Publisher) -> dict:
    store = pub.validator.store
    return json.loads(store.get_range(store.recorded("ckpt-g2-s1")))


def test_a_planted_token_never_reaches_a_published_checkpoint(root):
    pub = make(root)
    doc, materials = checkpoint(1)
    printed = json.dumps({"type": "tool_result", "content": f"ran it: {TOKEN} done"}, separators=(",", ":"))
    materials[model.NATIVE_SESSION] = _native({"projects/s-1.jsonl": QUIET + printed + "\n"})
    result = pub.publish(doc, materials)
    assert (result.promoted, result.reason) == (True, "promoted")
    assert all(TOKEN.encode() not in data for data in files(root).values())
    lines = _published_members(pub)["projects/s-1.jsonl"].splitlines(keepends=True)
    assert lines[0] == QUIET
    assert json.loads(lines[1]) == {"type": "tool_result", "content": "ran it: [REDACTED:github_token] done"}
    assert _committed(pub)["secret_redactions"] == 1


def test_a_secret_named_by_its_record_key_is_redacted(root):
    pub = make(root)
    doc, materials = checkpoint(1)
    key, value = "api" + "_key", "abcd" + "1234efgh"
    materials[model.NATIVE_SESSION] = _native({"projects/s-1.jsonl": json.dumps({"type": "env", key: value}) + "\n"})
    assert pub.publish(doc, materials).promoted
    assert all(value.encode() not in data for data in files(root).values())
    hidden = json.dumps({"type": "env", key: "[REDACTED:generic_secret]"}, separators=(",", ":"))
    assert _published_members(pub)["projects/s-1.jsonl"] == hidden + "\n"
    assert _committed(pub)["secret_redactions"] == 1


def test_a_token_held_as_a_record_key_is_redacted_in_the_rewritten_line(root):
    pub = make(root)
    doc, materials = checkpoint(1)
    materials[model.NATIVE_SESSION] = _native({"projects/s-1.jsonl": json.dumps({TOKEN: [1, "x"]}) + "\r\n"})
    assert pub.publish(doc, materials).promoted
    assert _published_members(pub)["projects/s-1.jsonl"] == '{"[REDACTED:github_token]":[1,"x"]}\r\n'
    assert _committed(pub)["secret_redactions"] == 1


def test_secrets_inside_nested_values_are_each_counted(root):
    pub = make(root)
    doc, materials = checkpoint(1)
    record = {"type": "t", "items": [TOKEN, {"text": f"{TOKEN} and {TOKEN}"}], "n": 3}
    materials[model.NATIVE_SESSION] = _native({"projects/s-1.jsonl": json.dumps(record) + "\n"})
    assert pub.publish(doc, materials).promoted
    hidden = "[REDACTED:github_token]"
    assert json.loads(_published_members(pub)["projects/s-1.jsonl"]) == {
        "type": "t",
        "items": [hidden, {"text": f"{hidden} and {hidden}"}],
        "n": 3,
    }
    assert _committed(pub)["secret_redactions"] == 3


def test_native_material_that_is_not_an_archive_is_refused_before_any_upload(root):
    pub = make(root)
    doc, materials = checkpoint(1)
    materials[model.NATIVE_SESSION] = b"not an archive"
    result = pub.publish(doc, materials)
    assert (result.promoted, result.reason, result.verdict) == (False, "native_format_changed", None)
    assert files(root) == {}
    assert pub.metrics() == {"checkpoint_publish_failures": {"native_format_changed": 1}}


def test_a_line_that_is_not_a_record_is_redacted_as_text(root):
    pub = make(root)
    doc, materials = checkpoint(1)
    materials[model.NATIVE_SESSION] = _native({"history.txt": f"token {TOKEN}\n{{broken\n"})
    assert pub.publish(doc, materials).promoted
    assert _published_members(pub)["history.txt"] == "token [REDACTED:github_token]\n{broken\n"
    assert _committed(pub)["secret_redactions"] == 1


def test_clean_native_material_publishes_byte_identical_with_a_zero_count(root):
    pub = make(root)
    doc, materials = checkpoint(1)
    archive = _native({"projects/s-1.jsonl": QUIET, "history.txt": "no secret here\n"})
    materials[model.NATIVE_SESSION] = archive
    assert pub.publish(doc, materials).promoted
    assert pub.validator.store.recorded(f"ckpt-g2-s1.{model.NATIVE_SESSION}") == ArtifactRef.of(archive)
    assert _committed(pub)["secret_redactions"] == 0


def test_a_checkpoint_without_native_material_records_no_redaction_count(root):
    pub = make(root)
    assert pub.publish(*checkpoint(1)).promoted
    assert "secret_redactions" not in _committed(pub)


def test_publication_adds_its_count_to_the_capture_count(root):
    pub = make(root)
    doc, materials = checkpoint(1)
    doc["secret_redactions"] = 2
    materials[model.NATIVE_SESSION] = _native({"history.txt": f"token {TOKEN}\n"})
    assert pub.publish(doc, materials).promoted
    assert _committed(pub)["secret_redactions"] == 3


@pytest.mark.parametrize(("slack", "promoted"), [(0, True), (-1, False)])
def test_native_material_grown_past_the_bound_by_redaction_is_refused(root, monkeypatch, slack, promoted):
    text = json.dumps({"type": "user", "t": "中" * 40 + " " + TOKEN}, ensure_ascii=False) + "\n"
    grown, _ = redaction.scrub(text.encode())
    assert len(grown) > len(text.encode())
    monkeypatch.setattr(native, "ARCHIVE_BYTES", len(grown) + slack)
    pub = make(root)
    doc, materials = checkpoint(1)
    materials[model.NATIVE_SESSION] = _native({"projects/s-1.jsonl": text})
    result = pub.publish(doc, materials)
    assert (result.promoted, result.reason) == (promoted, "promoted" if promoted else "native_too_large")


def test_native_material_holding_a_key_without_its_end_is_refused_before_any_upload(root):
    pub = make(root)
    doc, materials = checkpoint(1)
    opened = json.dumps({"t": "-----BEGIN RSA PRIVATE" + " KEY-----\nAAAA"})
    materials[model.NATIVE_SESSION] = _native({"projects/s-1.jsonl": opened + "\n"})
    result = pub.publish(doc, materials)
    assert (result.promoted, result.reason) == (False, "secret_unredactable")
    assert files(root) == {}
