from collections.abc import Mapping
from pathlib import Path
from threading import Event, Thread

import pytest

from scripts.ci_lab import cache, contract, scheduler

FIRST_POD = "11111111-1111-4111-8111-111111111111"
SECOND_POD = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    root = tmp_path / "cache"
    root.mkdir(mode=0o750)
    monkeypatch.setattr(cache, "_pid", lambda: 1)
    return {
        cache.ROOT: str(root),
        cache.LIMIT: str(1024 * 1024),
        cache.POD_UID: FIRST_POD,
        contract.GIT_MIRROR: str(tmp_path / "old-node"),
        "CI_LAB_MIRROR_STORE": str(tmp_path / "old-store"),
        "preserved": "value",
    }


def capture_environment(calls: dict[str, object], found: Mapping[str, str]) -> Mapping[str, str]:
    calls["bus_environ"] = found
    return found


def startup_environments(
    environ: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> tuple[dict[str, str], dict[str, str]]:
    if not enabled:
        return environ, environ
    root = tmp_path / "cache"
    root.mkdir(mode=0o750)
    requested = environ | {
        cache.ROOT: str(root),
        cache.LIMIT: str(1024 * 1024),
        cache.POD_UID: FIRST_POD,
    }
    generation = root / FIRST_POD
    resolved = requested | {
        contract.GIT_MIRROR: str(generation / "node"),
        "CI_LAB_MIRROR_STORE": str(generation / "store"),
    }
    monkeypatch.setattr(cache, "_pid", lambda: 1)
    return requested, resolved


@pytest.mark.feature("lab-scheduler-cache-policy")
@pytest.mark.parametrize("empty", [False, True])
def test_disabled_policy_preserves_mapping_identity_and_callback_result(empty: bool) -> None:
    environ = {"preserved": "value"}
    if empty:
        environ |= dict.fromkeys((cache.ROOT, cache.LIMIT, cache.POD_UID), "")
    calls: list[Mapping[str, str]] = []

    def start(found: Mapping[str, str]) -> int:
        calls.append(found)
        return 23

    assert cache.run(start, environ) == 23
    assert len(calls) == 1 and calls[0] is environ


@pytest.mark.feature("lab-scheduler-cache-policy")
def test_disabled_policy_propagates_the_callback_exception() -> None:
    failure = RuntimeError("service failed")

    def start(_environ: Mapping[str, str]) -> int:
        raise failure

    with pytest.raises(RuntimeError) as raised:
        cache.run(start, {})
    assert raised.value is failure


@pytest.mark.feature("lab-scheduler-cache-policy")
@pytest.mark.parametrize("root", [None, ""])
def test_required_policy_refuses_an_absent_root_before_start_or_mutation(
    policy: dict[str, str], root: str | None
) -> None:
    directory = Path(policy[cache.ROOT])
    environ = {key: value for key, value in policy.items() if key != cache.ROOT}
    if root is not None:
        environ[cache.ROOT] = root
    started: list[Mapping[str, str]] = []
    with pytest.raises(cache.PolicyError):
        cache.run(lambda found: started.append(found) or 0, environ, required=True)
    assert started == [] and list(directory.iterdir()) == []


@pytest.mark.feature("lab-scheduler-cache-policy")
@pytest.mark.parametrize("missing", [cache.ROOT, cache.LIMIT, cache.POD_UID])
def test_partial_policy_refuses_before_start_or_mutation(policy: dict[str, str], missing: str) -> None:
    directory = Path(policy[cache.ROOT])
    environ = {key: value for key, value in policy.items() if key != missing}
    started: list[Mapping[str, str]] = []
    with pytest.raises(cache.PolicyError):
        cache.run(lambda found: started.append(found) or 0, environ)
    assert started == [] and list(directory.iterdir()) == []


@pytest.mark.feature("lab-scheduler-cache-policy")
@pytest.mark.parametrize(
    ("key", "value"),
    [(cache.LIMIT, "0"), (cache.LIMIT, "-1"), (cache.LIMIT, "many"), (cache.POD_UID, "../foreign")],
)
def test_invalid_policy_values_refuse_before_mutation(policy: dict[str, str], key: str, value: str) -> None:
    directory = Path(policy[cache.ROOT])
    with pytest.raises(cache.PolicyError):
        cache.run(lambda _found: 0, policy | {key: value})
    assert list(directory.iterdir()) == []


@pytest.mark.feature("lab-scheduler-cache-policy")
@pytest.mark.parametrize("kind", ["relative", "symlink", "file", "foreign", "writable"])
def test_unsafe_roots_refuse_without_removing_foreign_data(
    policy: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    directory = Path(policy[cache.ROOT])
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    retained = foreign / "retained"
    retained.write_text("foreign data")
    if kind == "relative":
        monkeypatch.chdir(tmp_path)
        policy[cache.ROOT] = "cache"
    elif kind == "symlink":
        link = tmp_path / "link"
        link.symlink_to(foreign, target_is_directory=True)
        policy[cache.ROOT] = str(link)
    elif kind == "file":
        policy[cache.ROOT] = str(retained)
    elif kind == "foreign":
        (directory / "unowned").write_text("keep")
    else:
        directory.chmod(0o777)
    with pytest.raises(cache.PolicyError):
        cache.run(lambda _found: 0, policy)
    assert retained.read_text() == "foreign data"
    if kind == "foreign":
        assert (directory / "unowned").read_text() == "keep"


@pytest.mark.feature("lab-scheduler-cache-policy")
def test_enabled_policy_requires_pid_one_before_mutation(
    policy: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cache, "_pid", lambda: 2)
    with pytest.raises(cache.PolicyError):
        cache.run(lambda _found: 0, policy)
    assert list(Path(policy[cache.ROOT]).iterdir()) == []


@pytest.mark.feature("lab-scheduler-cache-policy")
def test_enabled_policy_requires_the_roots_owner_before_mutation(
    policy: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = Path(policy[cache.ROOT]).stat().st_uid
    monkeypatch.setattr(cache, "_uid", lambda: owner + 1)
    with pytest.raises(cache.PolicyError):
        cache.run(lambda _found: 0, policy)
    assert list(Path(policy[cache.ROOT]).iterdir()) == []


@pytest.mark.feature("lab-scheduler-cache-policy")
def test_same_pod_reuses_its_cache_and_retains_healthy_exit(policy: dict[str, str]) -> None:
    original = dict(policy)
    seen: list[Path] = []

    def first(found: Mapping[str, str]) -> int:
        node = Path(found[contract.GIT_MIRROR])
        store = Path(found["CI_LAB_MIRROR_STORE"])
        assert node.is_dir() and store.is_dir()
        assert node.parent == store.parent and node != store
        assert store.parent.name == FIRST_POD
        assert found["preserved"] == "value" and found is not policy
        (store / "kept").write_text("cached object")
        seen.append(store)
        return 17

    def second(found: Mapping[str, str]) -> int:
        store = Path(found["CI_LAB_MIRROR_STORE"])
        assert store == seen[0]
        assert (store / "kept").read_text() == "cached object"
        return 19

    assert cache.run(first, policy, required=True) == 17
    assert cache.run(second, policy, required=True) == 19
    assert policy == original


@pytest.mark.feature("lab-scheduler-cache-policy")
def test_replacement_pod_gets_a_fresh_cache_and_reclaims_the_old_owned_generation(
    policy: dict[str, str],
) -> None:
    previous: list[Path] = []

    def old(found: Mapping[str, str]) -> int:
        store = Path(found["CI_LAB_MIRROR_STORE"])
        assert store.is_dir()
        (store / "old").write_text("old object")
        previous.append(store.parent)
        return 0

    def new(found: Mapping[str, str]) -> int:
        store = Path(found["CI_LAB_MIRROR_STORE"])
        assert store.parent.name == SECOND_POD
        assert list(store.iterdir()) == []
        assert not previous[0].exists()
        return 0

    cache.run(old, policy)
    cache.run(new, policy | {cache.POD_UID: SECOND_POD})


@pytest.mark.feature("lab-scheduler-cache-policy")
def test_a_second_owner_refuses_while_the_first_consumer_is_running(policy: dict[str, str]) -> None:
    entered = Event()
    release = Event()

    def hold(_found: Mapping[str, str]) -> int:
        entered.set()
        assert release.wait(10)
        return 0

    thread = Thread(target=cache.run, args=(hold, policy))
    thread.start()
    try:
        assert entered.wait(10)
        with pytest.raises(cache.PolicyError):
            cache.run(lambda _found: 0, policy | {cache.POD_UID: SECOND_POD})
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()


@pytest.mark.feature("lab-scheduler-cache-policy")
def test_allocated_bytes_count_hardlinks_once_and_do_not_follow_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "measure"
    root.mkdir()
    data = root / "data"
    data.write_bytes(b"x" * 65536)
    (root / "alias").hardlink_to(data)
    outside = tmp_path / "outside"
    outside.write_bytes(b"y" * 1048576)
    link = root / "link"
    link.symlink_to(outside)
    sparse = root / "sparse"
    with sparse.open("wb") as stream:
        stream.truncate(1048576)
    expected = sum(path.stat(follow_symlinks=False).st_blocks * 512 for path in (root, data, link, sparse))
    assert expected > 0
    assert cache.allocated_bytes(root) == expected


@pytest.mark.feature("lab-scheduler-cache-policy")
def test_over_budget_cache_is_invalidated_and_reset_on_same_pod_restart(
    policy: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    exited = Event()
    statuses: list[int] = []
    monkeypatch.setattr(cache, "SCAN_SECONDS", 0.01)

    def exit_process(status: int) -> None:
        statuses.append(status)
        exited.set()

    monkeypatch.setattr(cache, "_terminate", exit_process)

    def exceed(found: Mapping[str, str]) -> int:
        store = Path(found["CI_LAB_MIRROR_STORE"])
        assert store.is_dir()
        (store / "oversize").write_bytes(b"x" * (2 * 1024 * 1024))
        assert exited.wait(10)
        assert (store.parent / ".invalid").is_file()
        return 0

    def restarted(found: Mapping[str, str]) -> int:
        store = Path(found["CI_LAB_MIRROR_STORE"])
        assert list(store.iterdir()) == []
        assert not (store.parent / ".invalid").exists()
        return 0

    cache.run(exceed, policy)
    assert statuses and all(status != 0 for status in statuses)
    cache.run(restarted, policy)


@pytest.mark.feature("lab-scheduler-cache-policy")
def test_main_keeps_missing_app_refusal_before_the_cache_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    called: list[Mapping[str, str]] = []

    def policy_run(_start: object, found: Mapping[str, str], *, required: bool = False) -> int:
        called.append(found)
        raise AssertionError(required)

    monkeypatch.setattr(cache, "run", policy_run)
    assert scheduler.main([], {cache.ROOT: str(tmp_path / "absent")}) == 1
    assert called == []
    assert "GITHUB_APP_ID" in capsys.readouterr().err
    assert not (tmp_path / "absent").exists()


@pytest.mark.feature("lab-scheduler-cache-policy")
def test_main_required_policy_refuses_before_runtime_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def runtime(_path: Path) -> None:
        raise AssertionError("runtime was constructed")

    monkeypatch.setattr(scheduler, "load_runtime", runtime)
    environ: dict[str, str] = dict.fromkeys(scheduler.GITHUB_APP_SETTINGS, "fixture")
    environ[contract.RUNTIME_CONFIG] = str(tmp_path / "not-read")
    assert scheduler.main(["--require-cache-policy"], environ) == 1
    assert cache.ROOT in capsys.readouterr().err


@pytest.mark.feature("lab-scheduler-cache-policy")
def test_scan_failure_invalidates_the_generation_before_fatal_exit(
    policy: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    exited = Event()
    started = Event()
    statuses: list[int] = []
    original = cache.allocated_bytes
    monkeypatch.setattr(cache, "SCAN_SECONDS", 0.01)

    def measure(root: Path) -> int:
        if started.is_set():
            raise PermissionError("cache cannot be measured")
        return original(root)

    def terminate(status: int) -> None:
        statuses.append(status)
        exited.set()

    def consume(found: Mapping[str, str]) -> int:
        started.set()
        assert exited.wait(10)
        assert (Path(found["CI_LAB_MIRROR_STORE"]).parent / ".invalid").is_file()
        return 0

    monkeypatch.setattr(cache, "allocated_bytes", measure)
    monkeypatch.setattr(cache, "_terminate", terminate)
    cache.run(consume, policy)
    assert statuses == [1]


@pytest.mark.feature("lab-scheduler-cache-policy")
def test_owned_root_cleanup_preserves_foreign_directories_and_symlink_targets(
    policy: dict[str, str], tmp_path: Path
) -> None:
    cache.run(lambda _found: 0, policy)
    root = Path(policy[cache.ROOT])
    foreign = root / "foreign"
    foreign.mkdir()
    retained = foreign / "kept"
    retained.write_text("foreign")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "kept").write_text("outside")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    cache.run(lambda _found: 0, policy | {cache.POD_UID: SECOND_POD})
    assert retained.read_text() == "foreign"
    assert (outside / "kept").read_text() == "outside"
    assert (root / "linked").is_symlink()


@pytest.mark.feature("lab-scheduler-cache-policy")
def test_consumer_exception_releases_the_lock_and_retains_healthy_cache(policy: dict[str, str]) -> None:
    failure = RuntimeError("consumer failed")

    def fail(found: Mapping[str, str]) -> int:
        (Path(found["CI_LAB_MIRROR_STORE"]) / "retained").write_text("healthy")
        raise failure

    def restart(found: Mapping[str, str]) -> int:
        assert (Path(found["CI_LAB_MIRROR_STORE"]) / "retained").read_text() == "healthy"
        return 29

    with pytest.raises(RuntimeError) as raised:
        cache.run(fail, policy)
    assert raised.value is failure
    assert cache.run(restart, policy) == 29


@pytest.mark.feature("lab-scheduler-cache-policy")
def test_allocated_bytes_tolerates_a_file_renamed_during_the_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "measure"
    root.mkdir()
    vanished = root / "vanished"
    vanished.write_bytes(b"x" * 8192)
    original = cache.os.stat

    def race(
        path: str | Path | int, *, dir_fd: int | None = None, follow_symlinks: bool = True
    ) -> cache.os.stat_result:
        if path == "vanished" and dir_fd is not None:
            vanished.unlink(missing_ok=True)
            raise FileNotFoundError("renamed during scan")
        return original(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(cache.os, "stat", race)
    assert cache.allocated_bytes(root) == root.lstat().st_blocks * 512


@pytest.mark.feature("lab-scheduler-cache-policy")
@pytest.mark.parametrize("kind", ["lock-link", "lock-file", "generation-link", "generation-foreign", "store-link"])
def test_unsafe_owned_entries_refuse_without_following_symlinks(
    policy: dict[str, str], tmp_path: Path, kind: str
) -> None:
    cache.run(lambda _found: 0, policy)
    root = Path(policy[cache.ROOT])
    generation = root / FIRST_POD
    outside = tmp_path / "outside"
    outside.mkdir()
    retained = outside / "retained"
    retained.write_text("foreign")
    if kind == "lock-link":
        (root / ".lock").unlink()
        (root / ".lock").symlink_to(retained)
    elif kind == "lock-file":
        (root / ".lock").chmod(0o666)
    elif kind == "generation-link":
        policy[cache.POD_UID] = SECOND_POD
        (root / SECOND_POD).symlink_to(outside, target_is_directory=True)
    elif kind == "generation-foreign":
        (generation / ".owner").unlink()
    else:
        (generation / "store").rmdir()
        (generation / "store").symlink_to(outside, target_is_directory=True)
    with pytest.raises(cache.PolicyError):
        cache.run(lambda _found: 0, policy)
    assert retained.read_text() == "foreign"
