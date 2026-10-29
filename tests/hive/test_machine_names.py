import json
import subprocess

import pytest

from scripts.hive import auth, cli, machine, registry, remove

pytestmark = pytest.mark.unit

URL = "https://hive.example:8770"
PUBLIC = "redis://hive.example:6380/2"
CLOUD = "ssh://worker@203.0.113.9:22"


@pytest.fixture
def admin():
    import fakeredis

    redis = fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    registry.update(redis, "root", ["ui=yes"])
    return redis


class Run:
    def __init__(self, saved=()):
        self.saved = list(saved)
        self.calls = []

    def __call__(self, argv, **kwargs):
        if argv == list(machine.LIST):
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.saved), "")
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")


def _members(redis):
    return [f"{auth.PREFIX}:member:{member}" for member in sorted(auth.members(redis))]


def _add(redis, run, target=CLOUD, label="swarm-hive-001"):
    return machine.add(redis, "root", target, label, machine.Delivery(URL, PUBLIC), run)


def test_each_machine_keeps_the_lowest_free_ordinal_it_was_given(admin):
    assert machine.name(admin, "worker-001") == "swarm-hive-001"
    assert machine.name(admin, "worker-002") == "swarm-hive-002"
    assert machine.name(admin, "worker-001") == "swarm-hive-001"

    assert admin.hgetall(machine.MACHINES) == {"worker-001": "swarm-hive-001", "worker-002": "swarm-hive-002"}


def test_a_new_machine_takes_the_lowest_ordinal_left_free(admin):
    admin.hset(machine.MACHINES, mapping={"worker-002": "swarm-hive-002", "worker-003": "swarm-hive-003"})

    assert machine.name(admin, "worker-009") == "swarm-hive-001"
    assert machine.name(admin, "worker-010") == "swarm-hive-004"


def test_a_name_taken_while_it_was_chosen_is_chosen_again(admin, monkeypatch):
    real = admin.pipeline

    def racing(*args, **kwargs):
        pipe = real(*args, **kwargs)
        execute = pipe.execute

        def raced(*inner, **options):
            if not admin.hexists(machine.MACHINES, "worker-009"):
                admin.hset(machine.MACHINES, "worker-009", "swarm-hive-001")
            return execute(*inner, **options)

        pipe.execute = raced
        return pipe

    monkeypatch.setattr(admin, "pipeline", racing)

    assert machine.name(admin, "worker-001") == "swarm-hive-002"
    assert admin.hgetall(machine.MACHINES) == {"worker-009": "swarm-hive-001", "worker-001": "swarm-hive-002"}


def test_a_target_saved_under_another_label_is_not_added_to_herdr_again(admin):
    run = Run([{"id": "a1", "label": "anton-2", "target": CLOUD}])

    machine.save(admin, "root", CLOUD, "swarm-hive-001", run)

    assert run.calls == []


def test_a_name_only_looked_up_writes_nothing(admin):
    assert machine.name(admin, "worker-001", assign=False) == "swarm-hive-001"
    assert admin.hgetall(machine.MACHINES) == {}

    machine.name(admin, "worker-001")
    assert machine.name(admin, "worker-002", assign=False) == "swarm-hive-002"
    assert machine.name(admin, "worker-001", assign=False) == "swarm-hive-001"


@pytest.mark.parametrize(
    ("target", "host"),
    [
        ("ssh://worker@10.10.30.207:2225", "10.10.30.207"),
        ("worker@cloud.example", "cloud.example"),
        ("cloud.example", "cloud.example"),
    ],
)
def test_the_machine_of_an_ssh_target_is_its_host(target, host):
    assert machine.host(target) == host


def test_add_for_a_machine_that_has_its_hive_keeps_the_member_and_herdr_entry(admin):
    first = _add(admin, Run())
    run = Run([{"id": "a1", "label": "swarm-hive-001", "target": CLOUD}])

    again = _add(admin, run)

    assert again == {"id": first["id"], "name": "swarm-hive-001"}
    assert run.calls == []
    assert _members(admin) == [f"{auth.PREFIX}:member:{first['id']}"]


def test_a_saved_label_without_a_member_is_not_added_to_herdr_again(admin):
    run = Run([{"id": "a1", "label": "swarm-hive-001", "target": "ssh://worker@203.0.113.9:2200"}])

    grant = _add(admin, run)

    assert [argv[0] for argv in run.calls] == ["ssh"]
    assert _members(admin) == [f"{auth.PREFIX}:member:{grant['id']}"]


def test_a_hive_whose_herdr_entry_is_gone_is_saved_again_with_its_member(admin):
    first = _add(admin, Run())
    run = Run()

    again = _add(admin, run)

    assert again == {"id": first["id"], "name": "swarm-hive-001"}
    assert run.calls == [["herdr", "machine", "add", "--label", "swarm-hive-001", "--remote-session", "default", CLOUD]]
    assert _members(admin) == [f"{auth.PREFIX}:member:{first['id']}"]


def test_a_saved_machine_without_a_host_does_not_stop_the_add(admin):
    run = Run([{"id": "a1", "label": "broken", "target": "ssh://"}, {"id": "a2", "label": "bare"}])

    grant = _add(admin, run)

    assert [argv[0] for argv in run.calls] == ["herdr", "ssh"]
    assert _members(admin) == [f"{auth.PREFIX}:member:{grant['id']}"]


def test_a_target_without_a_host_is_refused():
    with pytest.raises(auth.HiveError) as refused:
        machine.host("ssh://")

    assert str(refused.value) == "the SSH target 'ssh://' names no host"


def test_a_saved_machine_without_a_target_is_not_taken_for_the_added_host(admin):
    run = Run([{"id": "a1", "label": "bare"}])

    grant = _add(admin, run, target="ssh://worker@xxxx:22")

    assert grant["name"] == "swarm-hive-001"
    assert [argv[0] for argv in run.calls] == ["herdr", "ssh"]


def test_a_second_hive_on_a_machine_that_has_one_is_refused(admin):
    run = Run([{"id": "a1", "label": "swarm-hive-1792604647", "target": "ssh://worker@203.0.113.9:2222"}])

    with pytest.raises(auth.HiveError) as refused:
        _add(admin, run)

    assert str(refused.value) == (
        "203.0.113.9 already runs hive swarm-hive-1792604647; one machine runs one hive, "
        "so grow it with more max agents"
    )
    assert run.calls == []
    assert _members(admin) == []


def test_cli_add_names_the_hive_after_its_machine_on_every_run(admin, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli, "redis_client", lambda: admin)
    monkeypatch.setattr(machine, "add", lambda *args, **kwargs: calls.append(args[3]) or {"id": "ab12"})
    monkeypatch.setattr("scripts.swarm.commands.hive_id", lambda: "root")
    monkeypatch.setattr(cli.ledger_ca, "served", lambda: None)

    assert cli.main(["add", CLOUD, "--url", URL]) == 0
    assert cli.main(["add", "ssh://worker@198.51.100.4:22", "--url", URL]) == 0
    assert cli.main(["add", CLOUD, "--url", URL]) == 0

    assert calls == ["swarm-hive-001", "swarm-hive-002", "swarm-hive-001"]
    assert (
        capsys.readouterr().out.splitlines()[0] == f"added {CLOUD} to herdr as swarm-hive-001; its hive member is ab12"
    )


def test_remove_deletes_the_files_of_a_hive_that_hive_create_named():
    owned = [
        "k8s/argocd/apps/swarm-hive-1792865339.yaml",
        "k8s/manifests/swarm-hive-1792865339/hive.yaml",
        "k8s/values-overlays/swarm-hive-1792865339/values.yaml",
    ]
    tree = "".join(f"{path}\n" for path in [*owned, "k8s/manifests/swarm-hive-001/hive.yaml"])

    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, tree, "")

    plan = remove.Plan("m1", "swarm-hive-1792865339", "k8s/x", dry_run=True)

    assert remove.unmanifest(plan, run) == [
        f"would open a pull request into the-cloud-clockwork/antoncore dev deleting {', '.join(owned)}"
    ]


def test_a_new_machine_skips_every_name_a_live_hive_already_holds(admin):
    admin.hset(machine.MACHINES, "worker-001", "swarm-hive-001")

    assert machine.name(admin, "worker-002", taken=lambda: {"swarm-hive-002", "anton-9"}) == "swarm-hive-003"
    assert admin.hgetall(machine.MACHINES) == {"worker-001": "swarm-hive-001", "worker-002": "swarm-hive-003"}


def test_a_known_machine_keeps_its_name_even_when_it_is_taken(admin):
    admin.hset(machine.MACHINES, "worker-002", "swarm-hive-002")

    assert machine.name(admin, "worker-002", taken=lambda: pytest.fail("a known machine reads no taken names")) == (
        "swarm-hive-002"
    )
