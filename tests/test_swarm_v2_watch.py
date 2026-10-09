import pytest

from scripts.swarm_v2.kubernetes import watch
from scripts.swarm_v2.kubernetes.watch import CursorExpired, Pod, Reconciler, labels

pytestmark = pytest.mark.unit

OWNER = "agentihooks-swarm-fixture"


def managed(name, execution_id, uid=None, deleting=False):
    return Pod(name, uid or f"uid-{name}", labels(OWNER, execution_id), deleting)


class Source:
    def __init__(self, pods, version="10"):
        self.pods, self.version = {pod.uid: pod for pod in pods}, version
<<<<<<< HEAD
        self.events, self.expire, self.lists = [], False, 0
=======
        self.events, self.expire, self.lists, self.watches = [], False, 0, []
>>>>>>> origin/dev

    def list_pods(self, selector):
        self.lists += 1
        return list(self.pods.values()), self.version

    def watch_pods(self, selector, resource_version):
<<<<<<< HEAD
=======
        self.watches.append((selector, resource_version))
>>>>>>> origin/dev
        if self.expire:
            self.expire = False
            raise CursorExpired(resource_version)
        yield from self.events
        self.events = []


def fixture_pods():
    return [
        managed("eng-1-a", "exec-a"),
        managed("eng-2-orphan", "exec-orphan"),
        Pod("eng-2-orphan-copy", "uid-foreign", {"app": "eng"}),
        Pod("eng-9-other", "uid-other", labels("someone-else", "exec-x")),
        Pod("eng-3-blank", "uid-blank", {watch.OWNER_LABEL: OWNER}),
    ]


def test_labels_carry_owner_and_immutable_execution_identity():
    assert labels(OWNER, "exec-a") == {watch.OWNER_LABEL: OWNER, watch.EXECUTION_LABEL: "exec-a"}


def test_classification_separates_every_orphan_class():
    plan = Reconciler(OWNER, cleanup=True).plan({"exec-a", "exec-b"}, fixture_pods())
    assert plan.matched == {"exec-a": "uid-eng-1-a"}
    assert plan.missing_pods == ["exec-b"]
    assert [pod.name for pod in plan.delete] == ["eng-2-orphan"]
    assert sorted(pod.name for pod in plan.quarantine) == ["eng-2-orphan-copy", "eng-3-blank", "eng-9-other"]
    assert plan.counts() == {
        "managed_orphan": 1,
        "missing_pod": 1,
        "foreign": 2,
        "ambiguous": 1,
        "terminating": 0,
        "superseded": 0,
    }


def test_superseded_generation_pod_is_kept_and_not_matched():
    plan = Reconciler(OWNER, cleanup=True).plan({"exec-b"}, [managed("a", "exec-a")], {"exec-a"})
    assert plan.delete == [] and plan.matched == {} and plan.quarantine == []
    assert plan.missing_pods == ["exec-b"]
    assert plan.counts()["superseded"] == 1
    assert plan.counts()["managed_orphan"] == 0


def test_foreign_lookalike_is_never_deleted_even_with_cleanup():
    plan = Reconciler(OWNER, cleanup=True).plan(set(), [Pod("eng-2-orphan", "uid-x", {"app": "eng"})])
    assert plan.delete == []
    assert [pod.uid for pod in plan.quarantine] == ["uid-x"]


def test_orphan_cleanup_disables_independently_of_status():
    plan = Reconciler(OWNER, cleanup=False).plan({"exec-a"}, fixture_pods())
    assert plan.delete == []
    assert plan.matched == {"exec-a": "uid-eng-1-a"}
    assert plan.counts()["managed_orphan"] == 1


def test_duplicate_execution_pods_are_ambiguous_and_never_matched():
    pods = [managed("a1", "exec-a"), managed("a2", "exec-a")]
    plan = Reconciler(OWNER, cleanup=True).plan({"exec-a"}, pods)
    assert plan.matched == {}
    assert plan.delete == []
    assert sorted(pod.name for pod in plan.quarantine) == ["a1", "a2"]
    assert plan.missing_pods == []


def test_terminating_pod_is_neither_matched_nor_relaunched_until_gone():
    plan = Reconciler(OWNER, cleanup=True).plan({"exec-a"}, [managed("a", "exec-a", deleting=True)])
    assert plan.matched == {}
    assert plan.missing_pods == []
    assert plan.delete == []
    assert plan.counts()["terminating"] == 1


def test_expired_cursor_relists_and_converges():
    source = Source(fixture_pods())
    view = watch.PodView(source)
    view.sync()
    source.pods.pop("uid-eng-2-orphan")
    source.version, source.expire = "20", True
    view.sync()
    assert source.lists == 2
    assert view.resource_version == "20"
    assert "uid-eng-2-orphan" not in {pod.uid for pod in view.pods()}


def test_delayed_deletion_of_an_old_incarnation_keeps_the_new_one():
    source = Source([managed("a", "exec-a", uid="old")])
    view = watch.PodView(source)
    view.sync()
    source.events = [
        ("ADDED", managed("a", "exec-a", uid="new"), "11"),
        ("DELETED", managed("a", "exec-a", uid="old"), "12"),
    ]
    view.sync()
    assert [pod.uid for pod in view.pods()] == ["new"]
    assert view.resource_version == "12"
<<<<<<< HEAD
=======
    assert source.watches == [(watch.OWNER_LABEL, "10")]


def test_every_class_accumulates_across_pods():
    pods = [
        Pod("blank-1", "uid-blank-1", {watch.OWNER_LABEL: OWNER}),
        Pod("blank-2", "uid-blank-2", {watch.OWNER_LABEL: OWNER}),
        managed("twin-1", "exec-t"),
        managed("twin-2", "exec-t"),
        managed("gone-1", "exec-d1", deleting=True),
        managed("gone-2", "exec-d2", deleting=True),
        managed("old-1", "exec-s1"),
        managed("old-2", "exec-s2"),
        managed("orphan-1", "exec-o1"),
        managed("orphan-2", "exec-o2"),
    ]
    plan = Reconciler(OWNER, cleanup=True).plan(set(), pods, {"exec-s1", "exec-s2"})
    assert plan.counts() == {
        "managed_orphan": 2,
        "missing_pod": 0,
        "foreign": 0,
        "ambiguous": 4,
        "terminating": 2,
        "superseded": 2,
    }
>>>>>>> origin/dev


def test_restart_converges_to_the_same_plan():
    source = Source(fixture_pods())
    journals = {"exec-a", "exec-b"}
    first = Reconciler(OWNER, cleanup=True).plan(journals, watch.PodView(source).sync().pods())
    second = Reconciler(OWNER, cleanup=True).plan(journals, watch.PodView(source).sync().pods())
    assert first.missing_pods == second.missing_pods == ["exec-b"]
    assert first.matched == second.matched
