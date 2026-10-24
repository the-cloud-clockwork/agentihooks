import json
import subprocess
import sys
import threading
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.swarm import controller, lease, promotion_watch
from scripts.swarm.ledger_client import LedgerClient, LedgerRefused
from scripts.swarm.store import RedisStore, SwarmConfig

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))

from scripts.swarm_ledger import ledger_core, new_ledger  # noqa: E402
from scripts.swarm_ledger.api import schemas  # noqa: E402

pytestmark = pytest.mark.unit

WATCHED = "owner-proof"
DOCTOR = "owner-proof-doctor"
NOW = 1_000_000
RED = {
    "id": 900,
    "conclusion": "failure",
    "head_sha": "a" * 40,
    "html_url": "https://github.com/o/r/actions/runs/900",
}


class SchemaLedger(LedgerClient):
    def __init__(self, slugs):
        super().__init__()
        self.docs = {slug: new_ledger.build_doc({"title": "Promotion owner proof"}, "swarm") for slug in slugs}
        self.meta = {}
        self.lock = threading.RLock()
        self.operations = []
        self.refuse = False
        self.leases = None
        self.epochs = []

    def _call(self, slug, ops=None):
        with self.lock:
            doc = self.docs[slug]
            if ops is not None:
                epoch = lease.EPOCH.get()
                for op in ops:
                    self.epochs.append((slug, op["op"], epoch))
                    if self.leases is not None and epoch is not None:
                        live = lease.current(self.leases, slug)
                        if live is None or live.epoch != epoch:
                            raise LedgerRefused(f"ledger {slug}: server refused: 409 stale_controller")
                checked = schemas.check_operations(
                    {"operation_id": ops[0]["id"], "ops": ops, "guards": {}},
                    ledger_core,
                    tuple(task["id"] for task in doc["tasks"]),
                )
                if self.refuse:
                    raise LedgerRefused("persistence refused")
                ctx = ledger_core.Context(
                    self.meta.setdefault(slug, {"rev": 0, "stamps": {}, "members": {}}), NOW, slug
                )
                for op in checked:
                    assert ledger_core.apply_op(doc, op, ctx)
                assert not ctx.refused
                self.operations.extend((slug, op) for op in checked)
            return deepcopy(doc)


@pytest.fixture
def linked():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    for slug in (WATCHED, DOCTOR):
        store.create(SwarmConfig(slug, "/repo", 1, 1))
    store.set_peer(WATCHED, DOCTOR)
    store.set_peer(DOCTOR, WATCHED)
    return store, SchemaLedger((WATCHED, DOCTOR))


def github(argv, **kwargs):
    body = [{"name": "coverage-ratchet", "conclusion": "failure", "steps": []}] if "/jobs" in argv[2] else [RED]
    return subprocess.CompletedProcess(argv, 0, "\n".join(json.dumps(row) for row in body), "")


def observe(store, ledger, slug, now=NOW):
    return promotion_watch.refresh(slug, store.config(slug), store, ledger, ledger.state(slug), now, github)


def test_linked_swarms_file_one_fixer_in_the_watched_ledger_even_when_doctor_observes_first(linked):
    store, ledger = linked
    observe(store, ledger, DOCTOR)
    observe(store, ledger, WATCHED)
    observe(store, ledger, DOCTOR, NOW + promotion_watch.REFRESH_MS)
    assert [(slug, task["id"]) for slug, doc in ledger.docs.items() for task in doc["tasks"]] == [
        (WATCHED, "promo-900-0")
    ]
    assert [(slug, op["kind"], op["lane"]) for slug, op in ledger.operations if op["op"] == "task_add"] == [
        (WATCHED, "ci", "ci")
    ]
    assert ledger.docs[DOCTOR].get("alerts", []) == []


@pytest.mark.parametrize("departure", ["stopped", "removed"])
def test_owner_departure_hands_over_without_recreating_an_existing_fixer_or_changing_its_proof(linked, departure):
    store, ledger = linked
    observe(store, ledger, WATCHED)
    ledger.docs[WATCHED]["tasks"][0]["proof"] = {"run": "existing repair proof"}
    original = deepcopy(ledger.docs[WATCHED]["tasks"])
    if departure == "removed":
        store.remove(WATCHED)
    else:
        store.update(WATCHED, state="stopped")
    observe(store, ledger, DOCTOR, NOW + promotion_watch.REFRESH_MS)
    assert ledger.docs[DOCTOR]["tasks"] == []
    assert ledger.docs[WATCHED]["tasks"] == original


@pytest.mark.parametrize("failure", ["ledger", "checkpoint", "job"])
def test_failed_persistence_then_owner_departure_retries_without_duplicate_intake(linked, monkeypatch, failure):
    from scripts.swarm import promotion_owner

    store, ledger = linked
    record = promotion_owner.key("/repo")
    original_hset = store.redis.hset

    def hset(name, key=None, value=None, mapping=None, **kwargs):
        if (name == record and key == "seen") or (failure == "job" and name == promotion_watch.key(WATCHED, "jobs")):
            raise RuntimeError("checkpoint refused")
        return original_hset(name, key, value, mapping=mapping, **kwargs)

    if failure == "ledger":
        ledger.refuse = True
        error = LedgerRefused
    else:
        monkeypatch.setattr(store.redis, "hset", hset)
        error = RuntimeError
    with pytest.raises(error):
        observe(store, ledger, WATCHED)
    assert store.redis.hget(record, "seen") is None
    ledger.refuse = False
    monkeypatch.setattr(store.redis, "hset", original_hset)
    store.update(WATCHED, state="stopped")
    observe(store, ledger, DOCTOR, NOW + promotion_watch.REFRESH_MS)
    assert sum(len(doc["tasks"]) for doc in ledger.docs.values()) == 1
    assert store.redis.hget(record, "seen") == "900"


def test_handover_on_a_new_failed_run_preserves_the_existing_active_repair(linked, monkeypatch):
    store, ledger = linked
    observe(store, ledger, WATCHED)
    ledger.docs[WATCHED]["tasks"][0]["proof"] = {"run": "retained proof"}
    original = deepcopy(ledger.docs[WATCHED]["tasks"])
    store.update(WATCHED, state="stopped")
    monkeypatch.setitem(RED, "id", 901)
    observe(store, ledger, DOCTOR, NOW + promotion_watch.REFRESH_MS)
    assert ledger.docs[DOCTOR]["tasks"] == []
    assert ledger.docs[WATCHED]["tasks"] == original


def test_handover_reconciles_prior_repairs_on_green_and_allows_the_next_real_failure(linked, monkeypatch):
    store, ledger = linked
    observe(store, ledger, WATCHED)
    store.update(WATCHED, state="stopped")
    monkeypatch.setitem(RED, "id", 901)
    monkeypatch.setitem(RED, "conclusion", "success")
    observe(store, ledger, DOCTOR, NOW + promotion_watch.REFRESH_MS)
    assert ledger.docs[WATCHED]["tasks"][0]["state"] == "done"
    assert all(alert["state"] == "done" for alert in ledger.docs[WATCHED]["alerts"])
    monkeypatch.setitem(RED, "id", 902)
    monkeypatch.setitem(RED, "conclusion", "failure")
    observe(store, ledger, DOCTOR, NOW + promotion_watch.REFRESH_MS * 2)
    assert [task["id"] for task in ledger.docs[DOCTOR]["tasks"]] == ["promo-902-0"]


def test_a_doctor_tick_closes_watched_repairs_on_green_under_the_watched_lease(linked, monkeypatch):
    store, ledger = linked
    observe(store, ledger, WATCHED)
    watched = lease.acquire(store, WATCHED, "watched-hive")
    store.redis.set(store.key(DOCTOR, "control-epoch"), watched.epoch + 5)
    doctor = lease.acquire(store, DOCTOR, "doctor-hive")
    store.update(WATCHED, state="stopped")
    ledger.leases = store
    monkeypatch.setitem(RED, "id", 901)
    monkeypatch.setitem(RED, "conclusion", "success")
    tick = controller.FencedLedger(store, DOCTOR, doctor, ledger)
    later = NOW + promotion_watch.REFRESH_MS
    promotion_watch.refresh(DOCTOR, store.config(DOCTOR), store, tick, tick.state(DOCTOR), later, github)
    assert ledger.docs[WATCHED]["tasks"][0]["state"] == "done"
    assert {(slug, epoch) for slug, kind, epoch in ledger.epochs if kind == "task_update"} == {(WATCHED, watched.epoch)}
    assert store.redis.get(promotion_watch.key(DOCTOR, "seen")) == "901"


def test_a_green_run_releases_a_fixer_the_master_kept_blocked_so_the_next_red_files_again(linked, monkeypatch):
    store, ledger = linked
    observe(store, ledger, WATCHED)
    ledger.update_task(WATCHED, "promo-900-0", {"state": "blocked"}, by="master@323133-0181")
    store.update(WATCHED, state="stopped")
    monkeypatch.setitem(RED, "id", 901)
    monkeypatch.setitem(RED, "conclusion", "success")
    observe(store, ledger, DOCTOR, NOW + promotion_watch.REFRESH_MS)
    assert ledger.docs[WATCHED]["tasks"][0]["state"] == "blocked"
    monkeypatch.setitem(RED, "id", 902)
    monkeypatch.setitem(RED, "conclusion", "failure")
    observe(store, ledger, DOCTOR, NOW + promotion_watch.REFRESH_MS * 2)
    assert [task["id"] for task in ledger.docs[DOCTOR]["tasks"]] == ["promo-902-0"]


def test_concurrent_linked_observers_share_one_intake_and_retry_after_lock_contention(linked, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    store, ledger = linked
    entered, release = threading.Event(), threading.Event()
    call = ledger._call

    def blocked(slug, ops=None):
        if ops:
            entered.set()
            assert release.wait(10)
        return call(slug, ops)

    monkeypatch.setattr(ledger, "_call", blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(observe, store, ledger, WATCHED)
        try:
            assert entered.wait(10)
            assert observe(store, ledger, DOCTOR) == []
            assert observe(store, ledger, WATCHED) == []
        finally:
            release.set()
        assert pending.result(10)
    observe(store, ledger, DOCTOR, NOW + promotion_watch.REFRESH_MS)
    assert [(slug, task["id"]) for slug, doc in ledger.docs.items() for task in doc["tasks"]] == [
        (WATCHED, "promo-900-0")
    ]


def test_independent_repositories_each_retain_their_own_failed_check_intake(linked):
    store, ledger = linked
    other = "other-owner"
    store.create(SwarmConfig(other, "/other-repo", 1, 1))
    ledger.docs[other] = new_ledger.build_doc({"title": "Other promotion"}, "swarm")
    observe(store, ledger, WATCHED)
    observe(store, ledger, other)
    assert [(slug, task["id"]) for slug, doc in ledger.docs.items() for task in doc["tasks"]] == [
        (WATCHED, "promo-900-0"),
        (other, "promo-900-0"),
    ]


def test_a_running_owner_remains_stable_when_a_preferred_watched_swarm_returns(linked, monkeypatch):
    store, ledger = linked
    store.update(WATCHED, state="stopped")
    observe(store, ledger, DOCTOR)
    store.update(WATCHED, state="running")
    monkeypatch.setitem(RED, "id", 901)
    observe(store, ledger, WATCHED, NOW + promotion_watch.REFRESH_MS)
    observe(store, ledger, DOCTOR, NOW + promotion_watch.REFRESH_MS)
    assert ledger.docs[WATCHED]["tasks"] == []
    assert [task["id"] for task in ledger.docs[DOCTOR]["tasks"]] == ["promo-900-0"]


def test_initial_owner_prefers_the_watched_swarm_even_when_its_doctor_sorts_first(linked):
    from scripts.doctor.priming import doctor_slug

    store, _ = linked
    store.remove(WATCHED)
    store.remove(DOCTOR)
    watched = "z" * 48
    doctor = doctor_slug(watched)
    assert doctor < watched
    for slug in (watched, doctor):
        store.create(SwarmConfig(slug, "/repo", 1, 1))
    store.set_peer(watched, doctor)
    store.set_peer(doctor, watched)
    ledger = SchemaLedger((watched, doctor))
    observe(store, ledger, doctor)
    observe(store, ledger, watched)
    assert ledger.docs[doctor]["tasks"] == []
    assert [task["id"] for task in ledger.docs[watched]["tasks"]] == ["promo-900-0"]


@pytest.mark.parametrize("alias", ["symlink", "worktree"])
def test_repository_aliases_share_the_same_intake_owner(linked, tmp_path, alias):
    store, ledger = linked
    repo, other = tmp_path / "repository", tmp_path / "alias"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    if alias == "symlink":
        other.symlink_to(repo, target_is_directory=True)
    else:
        other.mkdir()
        metadata = repo / ".git" / "worktrees" / "alias"
        metadata.mkdir(parents=True)
        (metadata / "commondir").write_text("../..")
        (metadata / "gitdir").write_text(str(other / ".git"))
        (metadata / "HEAD").write_text("ref: refs/heads/alias")
        (other / ".git").write_text(f"gitdir: {metadata}")
    store.update(WATCHED, repo=str(repo))
    store.update(DOCTOR, repo=str(other))
    observe(store, ledger, DOCTOR)
    observe(store, ledger, WATCHED)
    assert ledger.docs[DOCTOR]["tasks"] == []
    assert [task["id"] for task in ledger.docs[WATCHED]["tasks"]] == ["promo-900-0"]


def test_handover_records_the_previous_owner_and_retains_earlier_history(linked):
    from scripts.swarm import promotion_owner

    store, ledger = linked
    observe(store, ledger, WATCHED)
    record = promotion_owner.key("/repo")
    assert store.redis.hget(record, "owner") == WATCHED
    assert store.redis.hget(record, "previous") == ""
    store.update(WATCHED, state="stopped")
    observe(store, ledger, DOCTOR, NOW + promotion_watch.REFRESH_MS)
    assert store.redis.hget(record, "owner") == DOCTOR
    assert store.redis.hget(record, "previous") == WATCHED
    assert promotion_owner.history("/repo", store.redis) == {WATCHED, DOCTOR}


def test_newer_local_checkpoint_survives_an_older_shared_checkpoint(linked, monkeypatch):
    from scripts.swarm import promotion_owner

    store, ledger = linked
    record = promotion_owner.key("/repo")
    store.redis.hset(record, mapping={"owner": WATCHED, "seen": 900})
    store.redis.set(promotion_watch.key(WATCHED, "seen"), 901)
    monkeypatch.setitem(RED, "id", 901)
    assert observe(store, ledger, WATCHED) == []
    assert ledger.docs[WATCHED]["tasks"] == []
    assert store.redis.hget(record, "seen") == "901"


def test_real_git_repositories_remain_independent_with_bounded_identity_reads(linked, tmp_path):
    from scripts.swarm import promotion_owner

    store, ledger = linked
    run, reads = subprocess.run, []

    def bounded(argv, **kwargs):
        assert kwargs["timeout"] == 10
        reads.append(argv[-1])
        return run(argv, **kwargs)

    for slug in (WATCHED, DOCTOR):
        repo = tmp_path / slug
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
        store.update(slug, repo=str(repo))
        assert promotion_owner.repository(str(repo), bounded) == str((repo / ".git").resolve())
    observe(store, ledger, WATCHED)
    observe(store, ledger, DOCTOR)
    assert [task["id"] for task in ledger.docs[WATCHED]["tasks"]] == ["promo-900-0"]
    assert [task["id"] for task in ledger.docs[DOCTOR]["tasks"]] == ["promo-900-0"]
    assert reads == ["--git-common-dir", "--git-common-dir"]


def test_repository_uses_resolved_checkout_path_when_git_is_missing(tmp_path):
    from scripts.swarm import promotion_owner

    def missing_git(*args, **kwargs):
        raise FileNotFoundError("git")

    repo = tmp_path / "checkout" / ".." / "repository"
    assert promotion_owner.repository(str(repo), missing_git) == str(tmp_path / "repository")


def test_linked_swarms_retain_one_promotion_fixer_without_git_on_path(linked, tmp_path, monkeypatch):
    store, ledger = linked
    monkeypatch.setenv("PATH", str(tmp_path))

    observe(store, ledger, DOCTOR)
    observe(store, ledger, WATCHED)

    assert ledger.docs[DOCTOR]["tasks"] == []
    assert [task["id"] for task in ledger.docs[WATCHED]["tasks"]] == ["promo-900-0"]


def test_first_owner_lookup_after_watcher_import_does_not_capture_a_temporary_runner(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    code = """
import sys
from unittest.mock import patch
from scripts.swarm import promotion_watch

with patch("subprocess.run", side_effect=AssertionError("temporary runner retained")):
    from scripts.swarm import promotion_owner
    print(promotion_owner.repository(sys.argv[1]))
"""
    result = subprocess.run([sys.executable, "-c", code, str(repo)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str((repo / ".git").resolve())


def test_intake_leaves_patched_subprocess_and_lua_scripts_untouched(linked, monkeypatch):
    store, ledger = linked

    def refused(*args, **kwargs):
        raise AssertionError(f"intake reached the patched subprocess with {args}")

    monkeypatch.setattr(subprocess, "run", refused)
    monkeypatch.setattr(store.redis, "lock", refused)
    monkeypatch.setattr(store.redis, "evalsha", refused)
    observe(store, ledger, DOCTOR)
    observe(store, ledger, WATCHED)
    assert [(slug, task["id"]) for slug, doc in ledger.docs.items() for task in doc["tasks"]] == [
        (WATCHED, "promo-900-0")
    ]


def test_only_doctors_remaining_elect_one_stable_owner_instead_of_the_current_observer(linked):
    from scripts.doctor.priming import doctor_slug

    store, _ = linked
    store.remove(WATCHED)
    store.remove(DOCTOR)
    watchers = ("a-watch", "z-watch")
    doctors = tuple(doctor_slug(watched) for watched in watchers)
    for watched, doctor in zip(watchers, doctors, strict=True):
        store.create(SwarmConfig(watched, "/repo", 1, 1, state="stopped"))
        store.create(SwarmConfig(doctor, "/repo", 1, 1))
        store.set_peer(doctor, watched)
        store.set_peer(watched, doctor)
    ledger = SchemaLedger((*watchers, *doctors))
    observe(store, ledger, doctors[1])
    observe(store, ledger, doctors[0])
    assert ledger.docs[doctors[1]]["tasks"] == []
    assert [task["id"] for task in ledger.docs[doctors[0]]["tasks"]] == ["promo-900-0"]


def test_proof_swarm_cannot_own_intake_that_it_is_exempt_from_running(linked):
    store, _ = linked
    store.remove(WATCHED)
    store.remove(DOCTOR)
    proof, watched = "proof-323133-cxc9-1", "z-watch"
    for slug in (proof, watched):
        store.create(SwarmConfig(slug, "/repo", 1, 1))
    ledger = SchemaLedger((proof, watched))
    observe(store, ledger, proof)
    observe(store, ledger, watched)
    assert ledger.docs[proof]["tasks"] == []
    assert [task["id"] for task in ledger.docs[watched]["tasks"]] == ["promo-900-0"]
