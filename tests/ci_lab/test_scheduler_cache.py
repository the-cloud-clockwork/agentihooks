import multiprocessing
from collections.abc import Callable, Mapping
from io import StringIO
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
    if kind != "foreign":
        original_iterdir = Path.iterdir
        rejected = Path(policy[cache.ROOT])

        def iterdir(path: Path) -> list[Path]:
            if path == rejected:
                raise AssertionError("Unsafe root was enumerated")
            return list(original_iterdir(path))

        monkeypatch.setattr(Path, "iterdir", iterdir)
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
        with pytest.raises(cache.PolicyError, match="(?i)owner"):
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
    names = {"first", "second", "third"}
    for name in names:
        (root / name).write_bytes(b"x" * 65536)
    original = cache.os.stat
    removed: list[str] = []

    def race(
        path: str | Path | int, *, dir_fd: int | None = None, follow_symlinks: bool = True
    ) -> cache.os.stat_result:
        if isinstance(path, str) and path in names and dir_fd is not None and not removed:
            (root / path).unlink()
            removed.append(path)
            raise FileNotFoundError("renamed during scan")
        return original(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(cache.os, "stat", race)
    allocated = cache.allocated_bytes(root)
    expected = root.lstat().st_blocks * 512 + sum(
        (root / name).lstat().st_blocks * 512 for name in names if name not in removed
    )
    assert len(removed) == 1 and allocated == expected


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


@pytest.mark.feature("lab-scheduler-cache-policy")
def test_logging_failure_cannot_prevent_invalid_marker_or_termination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenStderr(StringIO):
        def write(self, _text: str) -> int:
            raise OSError("stderr unavailable")

    statuses: list[int] = []
    monkeypatch.setattr(cache.sys, "stderr", BrokenStderr())
    monkeypatch.setattr(cache, "_terminate", statuses.append)
    with pytest.raises(OSError, match="stderr unavailable"):
        cache._invalidate(tmp_path, "budget exceeded")
    assert (tmp_path / ".invalid").read_text() == "budget exceeded"
    assert statuses == [1]


@pytest.mark.feature("lab-scheduler-cache-policy")
def test_unmarked_excess_is_reset_before_restart_consumers(policy: dict[str, str]) -> None:
    def exceed(found: Mapping[str, str]) -> int:
        (Path(found["CI_LAB_MIRROR_STORE"]) / "excess").write_bytes(b"x" * (2 * 1024 * 1024))
        return 0

    def restart(found: Mapping[str, str]) -> int:
        assert list(Path(found["CI_LAB_MIRROR_STORE"]).iterdir()) == []
        return 31

    cache.run(exceed, policy)
    assert cache.run(restart, policy) == 31


@pytest.mark.feature("lab-scheduler-cache-policy")
def test_startup_measurement_failure_refuses_before_consumers(
    policy: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[Mapping[str, str]] = []

    def failed(_root: Path) -> int:
        raise PermissionError("cannot measure cache")

    monkeypatch.setattr(cache, "allocated_bytes", failed)
    with pytest.raises(cache.PolicyError, match="(?i)generation"):
        cache.run(lambda found: started.append(found) or 0, policy)
    assert started == []


@pytest.mark.feature("lab-scheduler-cache-policy")
def test_invalid_marker_failure_still_terminates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    statuses: list[int] = []

    def failed(_path: Path, *_args: object, **_kwargs: object) -> None:
        raise OSError("filesystem full")

    monkeypatch.setattr(Path, "open", failed)
    monkeypatch.setattr(cache, "_terminate", statuses.append)
    with pytest.raises(OSError, match="filesystem full"):
        cache._invalidate(tmp_path, "budget exceeded")
    assert statuses == [1]


@pytest.mark.feature("lab-scheduler-cache-policy")
def test_monitor_shutdown_failure_terminates_before_releasing_ownership(
    policy: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    statuses: list[int] = []

    class FailedMonitor:
        def __init__(self, callback: Callable[[], None], *, every: float) -> None:
            del callback
            assert every == cache.SCAN_SECONDS

        def start(self) -> None:
            calls.append("started")

        def close(self) -> None:
            calls.append("closed")
            raise RuntimeError("monitor did not stop")

    def terminate(status: int) -> None:
        statuses.append(status)
        with pytest.raises(cache.PolicyError):
            cache.run(lambda _found: 0, policy)

    monkeypatch.setattr(cache, "Interval", FailedMonitor)
    monkeypatch.setattr(cache, "_terminate", terminate)
    assert cache.run(lambda _found: 0, policy) == 0
    assert calls == ["started", "closed"]
    assert statuses == [1]
    assert (Path(policy[cache.ROOT]) / FIRST_POD / ".invalid").is_file()


@pytest.mark.feature("lab-scheduler-cache-policy")
def test_an_interrupted_empty_root_with_only_its_lock_can_restart(policy: dict[str, str]) -> None:
    root = Path(policy[cache.ROOT])
    (root / ".lock").touch(mode=0o600)
    assert cache.run(lambda _found: 37, policy) == 37


@pytest.mark.feature("lab-scheduler-cache-policy")
def test_fresh_cache_entries_are_private_even_with_a_permissive_umask(policy: dict[str, str]) -> None:
    previous = cache.os.umask(0)

    def consume(found: Mapping[str, str]) -> int:
        store = Path(found["CI_LAB_MIRROR_STORE"])
        for directory in (store.parent, store, Path(found[contract.GIT_MIRROR])):
            assert directory.stat().st_mode & 0o707 == 0o700
        assert (Path(policy[cache.ROOT]) / ".lock").stat().st_mode & 0o777 == 0o600
        return 0

    try:
        cache.run(consume, policy)
    finally:
        cache.os.umask(previous)


@pytest.mark.feature("lab-scheduler-cache-policy")
@pytest.mark.parametrize("entry", ["root", "node", "store", "lock"])
@pytest.mark.parametrize("write_bit", [0o020, 0o002])
def test_group_or_world_writable_owned_entries_refuse_start(policy: dict[str, str], entry: str, write_bit: int) -> None:
    cache.run(lambda _found: 0, policy)
    root = Path(policy[cache.ROOT])
    target = {
        "root": root,
        "node": root / FIRST_POD / "node",
        "store": root / FIRST_POD / "store",
        "lock": root / ".lock",
    }[entry]
    target.chmod(target.stat().st_mode | write_bit)
    started: list[Mapping[str, str]] = []
    with pytest.raises(cache.PolicyError) as raised:
        cache.run(lambda found: started.append(found) or 0, policy)
    assert started == []
    reason = (
        "owned private directory"
        if entry == "root"
        else "ownership lock"
        if entry == "lock"
        else "unsafe cache directory"
    )
    assert reason in str(raised.value).casefold()


@pytest.mark.feature("lab-scheduler-cache-policy")
@pytest.mark.parametrize("entry", ["root", "node", "store", "lock"])
def test_compatible_owned_permissions_are_retained(policy: dict[str, str], entry: str) -> None:
    cache.run(lambda _found: 0, policy)
    root = Path(policy[cache.ROOT])
    target = {
        "root": root,
        "node": root / FIRST_POD / "node",
        "store": root / FIRST_POD / "store",
        "lock": root / ".lock",
    }[entry]
    target.chmod(target.stat().st_mode | 0o001)
    assert cache.run(lambda _found: 41, policy) == 41


@pytest.mark.feature("lab-scheduler-cache-policy")
def test_a_hardlinked_lock_refuses_without_removing_its_other_name(policy: dict[str, str], tmp_path: Path) -> None:
    cache.run(lambda _found: 0, policy)
    other = tmp_path / "other-lock"
    other.hardlink_to(Path(policy[cache.ROOT]) / ".lock")
    with pytest.raises(cache.PolicyError, match="(?i)lock"):
        cache.run(lambda _found: 0, policy)
    assert other.is_file()


@pytest.mark.feature("lab-scheduler-cache-policy")
@pytest.mark.parametrize("kind", ["wrong-marker", "marker-link", "generation-link"])
@pytest.mark.parametrize("current", [False, True])
def test_foreign_uuid_generations_are_never_claimed_or_reclaimed(
    policy: dict[str, str], tmp_path: Path, kind: str, current: bool
) -> None:
    cache.run(lambda _found: 0, policy)
    root = Path(policy[cache.ROOT])
    outside = tmp_path / "foreign"
    outside.mkdir()
    (outside / ".owner").write_text((root / FIRST_POD / ".owner").read_text())
    retained = outside / "retained"
    retained.write_text("foreign data")
    candidate = root / SECOND_POD
    if kind == "generation-link":
        candidate.symlink_to(outside, target_is_directory=True)
    else:
        candidate.mkdir()
        (candidate / "retained").write_text("foreign data")
        if kind == "marker-link":
            (candidate / ".owner").symlink_to(outside / ".owner")
        else:
            (candidate / ".owner").write_text("unowned marker")
    if current:
        with pytest.raises(cache.PolicyError, match="(?i)generation"):
            cache.run(lambda _found: 0, policy | {cache.POD_UID: SECOND_POD})
    else:
        cache.run(lambda _found: 0, policy)
    assert candidate.exists()
    assert (candidate / "retained").read_text() == "foreign data"
    assert retained.read_text() == "foreign data"


@pytest.mark.feature("lab-scheduler-cache-policy")
@pytest.mark.parametrize("marker", ["regular", "link", "dangling"])
def test_a_small_invalid_generation_is_reset_without_touching_external_targets(
    policy: dict[str, str], tmp_path: Path, marker: str
) -> None:
    cache.run(lambda _found: 0, policy)
    generation = Path(policy[cache.ROOT]) / FIRST_POD
    (generation / "store" / "kept").write_text("small cache")
    external = tmp_path / "external"
    external.write_text("external data")
    invalid = generation / ".invalid"
    if marker == "regular":
        invalid.write_text("invalidated")
    else:
        invalid.symlink_to(external if marker == "link" else tmp_path / "absent")

    def restart(found: Mapping[str, str]) -> int:
        assert list(Path(found["CI_LAB_MIRROR_STORE"]).iterdir()) == []
        assert not invalid.exists() and not invalid.is_symlink()
        return 0

    assert cache.allocated_bytes(Path(policy[cache.ROOT])) < int(policy[cache.LIMIT])
    cache.run(restart, policy)
    assert external.read_text() == "external data"


@pytest.mark.feature("lab-scheduler-cache-policy")
def test_one_byte_is_valid_but_too_small_for_the_cache_metadata(policy: dict[str, str]) -> None:
    with pytest.raises(cache.PolicyError, match="(?i)exceeds"):
        cache.run(lambda _found: 0, policy | {cache.LIMIT: "1"})


@pytest.mark.feature("lab-scheduler-cache-policy")
def test_foreign_allocated_data_can_refuse_start_but_is_never_deleted(policy: dict[str, str]) -> None:
    cache.run(lambda _found: 0, policy)
    foreign = Path(policy[cache.ROOT]) / "foreign"
    foreign.mkdir()
    data = foreign / "data"
    data.write_bytes(b"x" * (2 * 1024 * 1024))
    started: list[Mapping[str, str]] = []
    with pytest.raises(cache.PolicyError, match="(?i)exceeds"):
        cache.run(lambda found: started.append(found) or 0, policy)
    assert started == []
    assert data.stat().st_size == 2 * 1024 * 1024


def changed_stat(info: cache.os.stat_result, field: int, value: int) -> cache.os.stat_result:
    values = list(info)
    values[field] = value
    return cache.os.stat_result(values)


@pytest.mark.feature("lab-scheduler-cache-policy")
@pytest.mark.parametrize("entry", ["root", "node", "store", "lock"])
def test_owned_entries_with_foreign_uid_metadata_refuse_before_start(
    policy: dict[str, str], monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    cache.run(lambda _found: 0, policy)
    root = Path(policy[cache.ROOT])
    target = {
        "root": root,
        "node": root / FIRST_POD / "node",
        "store": root / FIRST_POD / "store",
        "lock": root / ".lock",
    }[entry]
    original_lstat = Path.lstat
    original_fstat = cache.os.fstat
    identity = target.stat()

    def lstat(path: Path) -> cache.os.stat_result:
        info = original_lstat(path)
        return changed_stat(info, 4, info.st_uid + 1) if path == target else info

    def fstat(descriptor: int) -> cache.os.stat_result:
        info = original_fstat(descriptor)
        return (
            changed_stat(info, 4, info.st_uid + 1)
            if (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino)
            else info
        )

    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(cache.os, "fstat", fstat)
    started: list[Mapping[str, str]] = []
    with pytest.raises(cache.PolicyError) as raised:
        cache.run(lambda found: started.append(found) or 0, policy)
    assert started == []
    reason = (
        "owned private directory"
        if entry == "root"
        else "ownership lock"
        if entry == "lock"
        else "unsafe cache directory"
    )
    assert reason in str(raised.value).casefold()


@pytest.mark.feature("lab-scheduler-cache-policy")
@pytest.mark.parametrize("entry", ["generation", "marker"])
def test_stale_generations_with_foreign_uid_metadata_are_preserved(
    policy: dict[str, str], monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    cache.run(lambda _found: 0, policy)
    root = Path(policy[cache.ROOT])
    candidate = root / SECOND_POD
    candidate.mkdir()
    marker = candidate / ".owner"
    marker.write_text((root / FIRST_POD / ".owner").read_text())
    retained = candidate / "retained"
    retained.write_text("foreign data")
    target = candidate if entry == "generation" else marker
    original = Path.lstat

    def lstat(path: Path) -> cache.os.stat_result:
        info = original(path)
        return changed_stat(info, 4, info.st_uid + 1) if path == target else info

    monkeypatch.setattr(Path, "lstat", lstat)
    assert cache.run(lambda _found: 43, policy) == 43
    assert retained.read_text() == "foreign data"


@pytest.mark.feature("lab-scheduler-cache-policy")
def test_a_nonregular_open_lock_refuses_before_consumers(
    policy: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    cache.run(lambda _found: 0, policy)
    target = (Path(policy[cache.ROOT]) / ".lock").stat()
    original = cache.os.fstat

    def fstat(descriptor: int) -> cache.os.stat_result:
        info = original(descriptor)
        if (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
            return changed_stat(info, 0, cache.stat.S_IFIFO | cache.stat.S_IMODE(info.st_mode))
        return info

    monkeypatch.setattr(cache.os, "fstat", fstat)
    with pytest.raises(cache.PolicyError, match="(?i)lock"):
        cache.run(lambda _found: 0, policy)


def manual_intervals(monkeypatch: pytest.MonkeyPatch) -> list[Callable[[], None]]:
    callbacks: list[Callable[[], None]] = []

    class ManualInterval:
        def __init__(self, callback: Callable[[], None], *, every: float) -> None:
            del every
            callbacks.append(callback)

        def start(self) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(cache, "Interval", ManualInterval)
    return callbacks


@pytest.mark.feature("lab-scheduler-cache-policy")
def test_exact_allocated_limit_retains_cache_at_start_and_during_service(
    policy: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    cache.run(lambda _found: 0, policy)
    root = Path(policy[cache.ROOT])
    retained = root / FIRST_POD / "store" / "retained"
    retained.write_text("healthy cache")
    limit = cache.allocated_bytes(root)
    callbacks = manual_intervals(monkeypatch)
    statuses: list[int] = []
    monkeypatch.setattr(cache, "_terminate", statuses.append)

    def consume(found: Mapping[str, str]) -> int:
        assert retained.read_text() == "healthy cache"
        assert cache.allocated_bytes(root) == limit
        callbacks[0]()
        assert not (Path(found["CI_LAB_MIRROR_STORE"]).parent / ".invalid").exists()
        return 47

    assert cache.run(consume, policy | {cache.LIMIT: str(limit)}) == 47
    assert statuses == []


@pytest.mark.feature("lab-scheduler-cache-policy")
def test_owned_reset_accepts_an_exact_limit_including_preserved_foreign_bytes(
    policy: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    cache.run(lambda _found: 0, policy)
    root = Path(policy[cache.ROOT])
    foreign = root / "foreign"
    foreign.mkdir()
    retained = foreign / "retained"
    retained.write_bytes(b"y" * 65536)
    limit = cache.allocated_bytes(root)
    (root / FIRST_POD / "store" / "excess").write_bytes(b"x" * 131072)
    manual_intervals(monkeypatch)

    def consume(found: Mapping[str, str]) -> int:
        assert list(Path(found["CI_LAB_MIRROR_STORE"]).iterdir()) == []
        assert cache.allocated_bytes(root) == limit
        assert retained.read_bytes() == b"y" * 65536
        return 53

    assert cache.run(consume, policy | {cache.LIMIT: str(limit)}) == 53


@pytest.mark.feature("lab-scheduler-cache-policy")
def test_fatal_budget_reason_is_persisted_and_flushed_before_termination(
    policy: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    callbacks = manual_intervals(monkeypatch)
    synced: list[str] = []
    statuses: list[int] = []
    original = cache.os.fsync

    class BufferedError(StringIO):
        flushed = ""

        def flush(self) -> None:
            self.flushed = self.getvalue()

    stderr = BufferedError()

    def fsync(descriptor: int) -> None:
        synced.append(cache.os.readlink(f"/proc/self/fd/{descriptor}"))
        original(descriptor)

    def terminate(status: int) -> None:
        statuses.append(status)
        marker = Path(policy[cache.ROOT]) / FIRST_POD / ".invalid"
        assert str(marker) in synced
        assert "allocated" in marker.read_text()
        assert "allocated" in stderr.flushed
        assert policy[cache.LIMIT] in stderr.flushed

    def consume(found: Mapping[str, str]) -> int:
        (Path(found["CI_LAB_MIRROR_STORE"]) / "excess").write_bytes(b"x" * (2 * 1024 * 1024))
        callbacks[0]()
        return 0

    monkeypatch.setattr(cache.os, "fsync", fsync)
    monkeypatch.setattr(cache.sys, "stderr", stderr)
    monkeypatch.setattr(cache, "_terminate", terminate)
    cache.run(consume, policy)
    assert statuses == [1]


@pytest.mark.feature("lab-scheduler-cache-policy")
@pytest.mark.parametrize("vanished", [False, True])
def test_directory_walk_errors_propagate_but_vanished_directories_do_not_hide_siblings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, vanished: bool
) -> None:
    root = tmp_path / "measure"
    root.mkdir()
    missing = root / "unreadable"
    missing.mkdir()
    retained = root / "retained"
    retained.write_bytes(b"x" * 65536)
    original = cache.os.open
    failure = FileNotFoundError("directory vanished") if vanished else PermissionError("directory unreadable")

    def open_directory(path: str | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if path == "unreadable" and dir_fd is not None:
            if vanished:
                missing.rmdir()
            raise failure
        return original(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(cache.os, "open", open_directory)
    if vanished:
        assert cache.allocated_bytes(root) >= retained.lstat().st_blocks * 512
        assert not missing.exists()
    else:
        with pytest.raises(PermissionError) as raised:
            cache.allocated_bytes(root)
        assert raised.value is failure


@pytest.mark.feature("lab-scheduler-cache-policy")
def test_allocated_bytes_refuses_a_symlink_as_its_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "retained").write_bytes(b"x" * 65536)
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        cache.allocated_bytes(linked)
    assert (outside / "retained").stat().st_size == 65536


@pytest.mark.feature("lab-scheduler-cache-policy")
@pytest.mark.parametrize("kind", ["missing-root", "invalid-limit", "non-pid-one"])
def test_main_policy_refusals_name_the_failed_setting(
    policy: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    environ = policy | dict.fromkeys(scheduler.GITHUB_APP_SETTINGS, "fixture")
    if kind == "missing-root":
        environ[cache.ROOT] = str(Path(policy[cache.ROOT]) / "absent")
    elif kind == "invalid-limit":
        environ[cache.LIMIT] = "invalid"
    else:
        monkeypatch.setattr(cache, "_pid", lambda: 2)
    assert scheduler.main(["--require-cache-policy"], environ) == 1
    setting = cache.LIMIT if kind == "invalid-limit" else cache.ROOT
    assert setting in capsys.readouterr().err


@pytest.mark.feature("lab-scheduler-cache-policy")
def test_a_lock_without_write_access_refuses_before_consumers(policy: dict[str, str]) -> None:
    cache.run(lambda _found: 0, policy)
    lock = Path(policy[cache.ROOT]) / ".lock"
    lock.chmod(0o400)
    writable = True
    try:
        descriptor = cache.os.open(lock, cache.os.O_RDWR)
    except PermissionError:
        writable = False
    else:
        cache.os.close(descriptor)
    if writable:
        assert cache.run(lambda _found: 59, policy) == 59
    else:
        with pytest.raises(cache.PolicyError, match="(?i)lock"):
            cache.run(lambda _found: 0, policy)


@pytest.mark.feature("lab-scheduler-cache-policy")
def test_allocated_bytes_refuses_a_fifo_without_waiting_for_a_writer(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert cache.allocated_bytes(empty) == empty.stat().st_blocks * 512
    fifo = tmp_path / "fifo"
    cache.os.mkfifo(fifo)

    def inspect_fifo() -> None:
        try:
            cache.allocated_bytes(fifo)
        except NotADirectoryError:
            return
        raise SystemExit(1)

    process = multiprocessing.get_context("fork").Process(target=inspect_fifo)
    process.start()
    try:
        process.join(timeout=10)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=10)
        process.close()
