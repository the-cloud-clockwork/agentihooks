from pathlib import Path

import pytest

from scripts.swarm_v2.artifacts import base, publication
from scripts.swarm_v2.artifacts.local import LocalBackend
from scripts.swarm_v2.artifacts.publication import PAUSED, PUBLISHED, Publication
from tests.test_swarm_v2_artifacts import bound
from tests.test_swarm_v2_cache import stamped

pytestmark = pytest.mark.unit

DATA = b"uncommitted diff\n"


@pytest.fixture
def world(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "diff.patch").write_bytes(DATA)
    (worktree / "src.py").write_text("print(1)\n")
    shared = tmp_path / "shared"
    shared.mkdir()
    return worktree, shared, bound(LocalBackend(shared))


def lose(shared: Path) -> Path:
    lost = shared.with_name("shared.lost")
    shared.rename(lost)
    shared.write_text("unmounted")
    return lost


def restore(shared: Path, lost: Path) -> None:
    shared.unlink()
    lost.rename(shared)


def test_a_healthy_backend_publishes_the_checkpoint(world):
    worktree, _, store = world
    result = publication.publish(store, "ckpt-1", worktree / "diff.patch")
    assert result == Publication(PUBLISHED, base.ArtifactRef.of(DATA), "")
    assert store.get_range(result.ref) == DATA


def test_lost_shared_storage_pauses_publication_and_leaves_the_worktree_untouched(world):
    worktree, shared, store = world
    before = stamped(worktree)
    lose(shared)
    result = publication.publish(store, "ckpt-1", worktree / "diff.patch")
    assert result.state == PAUSED and result.ref is None
    assert result.reason.startswith("artifact storage is unavailable (")
    assert result.reason.endswith("), so publication is paused and the attempt keeps its files")
    assert stamped(worktree) == before


def test_a_retry_after_the_storage_returns_commits_once(world):
    worktree, shared, store = world
    lost = lose(shared)
    assert publication.publish(store, "ckpt-1", worktree / "diff.patch").state == PAUSED
    restore(shared, lost)
    first = publication.publish(store, "ckpt-1", worktree / "diff.patch")
    keys = store.backend.keys("")
    again = publication.publish(store, "ckpt-1", worktree / "diff.patch")
    assert first == again == Publication(PUBLISHED, base.ArtifactRef.of(DATA), "")
    assert store.backend.keys("") == keys
    assert keys == ["s1/t1/artifacts/ckpt-1.json", f"s1/t1/objects/{base.ArtifactRef.of(DATA).sha256}"]


def test_a_content_conflict_is_refused_instead_of_paused(world):
    worktree, _, store = world
    publication.publish(store, "ckpt-1", worktree / "diff.patch")
    with pytest.raises(base.ArtifactError) as error:
        publication.publish(store, "ckpt-1", worktree / "src.py")
    assert str(error.value) == "artifact ckpt-1 is committed with other content"


def test_a_missing_source_file_is_an_error_of_the_attempt(world):
    worktree, _, store = world
    with pytest.raises(FileNotFoundError):
        publication.publish(store, "ckpt-1", worktree / "absent")


def test_a_paused_reason_names_the_failure_without_its_path(world):
    worktree, _, _ = world

    class Down:
        kind = "local"

        def size(self, key):
            return None

        def write(self, key, data):
            raise ConnectionResetError(104, "Connection reset by peer", "/secret/path")

        def remove(self, key):
            return None

    result = publication.publish(bound(Down()), "ckpt-1", worktree / "diff.patch")
    assert result == Publication(
        PAUSED,
        None,
        "artifact storage is unavailable (Connection reset by peer), so publication is paused and the attempt keeps its files",
    )


def test_a_failure_without_a_message_is_named_by_its_type(world):
    worktree, _, _ = world

    class Down:
        kind = "local"

        def size(self, key):
            raise TimeoutError

    result = publication.publish(bound(Down()), "ckpt-1", worktree / "diff.patch")
    assert result.reason == (
        "artifact storage is unavailable (TimeoutError), so publication is paused and the attempt keeps its files"
    )


def test_package_cases_pass_on_the_isolated_fixture():
    from tests.contracts.storage_layout.cases import case_a, case_b, case_c

    assert [case()["passed"] for case in (case_a, case_b, case_c)] == [True, True, True]
