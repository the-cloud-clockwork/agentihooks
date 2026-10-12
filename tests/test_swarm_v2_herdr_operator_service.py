import pytest

from scripts.swarm_v2.herdr.endpoints import Endpoint, Scope
from tests import sv2_hdr03_cases as cases

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]
FX = cases.fixture()
HOST_KEY = cases.HOST_KEY
SWARMS = ("hive-a", "hive-b")


def store():
    import fakeredis

    from scripts.swarm.store import RedisStore, SwarmConfig

    made = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    for slug in SWARMS:
        made.create(SwarmConfig(slug, "agentihooks", 1, 0))
    return made


def worker(made, slug: str, backend: str = "kubernetes") -> tuple[str, int]:
    from scripts.swarm.store import AgentRecord

    seat = len(made.execution_occupants(slug)) + 1
    agent = AgentRecord(
        made.next_name(slug, "eng"),
        "eng",
        "task",
        seat=f"eng-{seat}@{slug}",
        runtime_backend=backend,
        runtime_target=(
            {"pod_namespace": FX["namespace"], "pod_name": "attempt"}
            if backend == "kubernetes"
            else {"server_id": "local", "pid": 1}
        ),
    )
    admitted = made.start_execution(slug, agent)
    return admitted.execution_id, admitted.generation


def publish(made, slug: str, execution_id: str, generation: int) -> None:
    from scripts.swarm_v2.kubernetes.terminal import HostKeys

    HostKeys(made).record(Scope("controller", slug, execution_id, generation), HOST_KEY)


def end(made, slug: str, execution_id: str) -> None:
    from scripts.swarm_v2.kubernetes.terminal import HostKeys

    HostKeys(made).forget(slug, execution_id)


def name(slug: str, execution_id: str, generation: int) -> str:
    return Endpoint(Scope("operator/operator", slug, execution_id, generation), FX["namespace"]).name


class Herdr(cases.Herdr):
    def __init__(self) -> None:
        super().__init__([FX["manual"]], set())

    def _add(self, command: list[str]):
        self.ready.add(command[-1])
        return super()._add(command)


def service(tmp_path, made, herdr):
    from scripts.swarm_v2.herdr.operator_service import Service
    from scripts.swarm_v2.herdr.upkeep import Upkeep

    terminal = cases.client_terminal(tmp_path / "client", herdr, FX["namespace"])
    return Service(Upkeep(terminal), lambda: made)


def test_a_pass_saves_the_published_workers_of_every_hive_into_the_operator_herdr(tmp_path):
    made, herdr = store(), Herdr()
    first, second = worker(made, "hive-a"), worker(made, "hive-b")
    publish(made, "hive-a", *first)
    publish(made, "hive-b", *second)

    report = service(tmp_path, made, herdr).run_pass()

    assert sorted(report.registered) == sorted([first[0], second[0]])
    assert report.refused == {}
    shown = cases._sidebar(herdr)
    assert shown[:2] == ["Local", "devbox"]
    assert sorted(shown[2:]) == sorted([name("hive-a", *first), name("hive-b", *second)])


def test_a_worker_that_ends_is_removed_on_the_next_pass(tmp_path):
    made, herdr = store(), Herdr()
    first, second = worker(made, "hive-a"), worker(made, "hive-a")
    publish(made, "hive-a", *first)
    publish(made, "hive-a", *second)
    running = service(tmp_path, made, herdr)
    running.run_pass()

    end(made, "hive-a", first[0])
    report = running.run_pass()

    assert list(report.registered) == [second[0]]
    assert report.retired == [first[0]]
    assert cases._sidebar(herdr) == ["Local", "devbox", name("hive-a", *second)]


def test_an_unchanged_hive_runs_no_herdr_command(tmp_path):
    made, herdr = store(), Herdr()
    publish(made, "hive-a", *worker(made, "hive-a"))
    running = service(tmp_path, made, herdr)
    running.run_pass()
    ran = len(herdr.commands)

    assert running.run_pass() is None
    assert len(herdr.commands) == ran


def test_only_kubernetes_workers_with_a_published_host_key_of_their_generation_are_saved(tmp_path):
    made, herdr = store(), Herdr()
    kept = worker(made, "hive-a")
    publish(made, "hive-a", *kept)
    worker(made, "hive-a")
    local = worker(made, "hive-a", backend="local")
    publish(made, "hive-a", *local)
    stale = worker(made, "hive-b")
    publish(made, "hive-b", stale[0], stale[1] + 1)

    report = service(tmp_path, made, herdr).run_pass()

    assert list(report.registered) == [kept[0]]


def test_a_refused_transport_saves_and_removes_nothing(tmp_path):
    from scripts.swarm_v2.herdr.operator_service import TRANSPORT, Service
    from scripts.swarm_v2.herdr.publications import TransportRefused
    from scripts.swarm_v2.herdr.upkeep import Upkeep

    herdr = Herdr()

    def refuse():
        raise TransportRefused("the private swarm Redis at swarm.example refused the connection: ConnectionError")

    terminal = cases.client_terminal(tmp_path / "client", herdr, FX["namespace"])
    report = Service(Upkeep(terminal), refuse).run_pass()

    assert report.refused == {
        TRANSPORT: "the private swarm Redis at swarm.example refused the connection: ConnectionError"
    }
    assert herdr.commands == []


JUMP_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJumpHostKey"
JUMP = {"AGENTIHOOKS_TERMINAL_JUMP": "swarm.example:2223", "AGENTIHOOKS_TERMINAL_JUMP_HOST_KEY": JUMP_KEY}


def placed(tmp_path):
    import json
    from pathlib import Path

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    root = tmp_path / "agentihooks"
    folder = root / "swarm-terminal"
    folder.mkdir(parents=True)
    key = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()
    )
    (folder / "user_ca").write_bytes(key)
    fixtures = Path(__file__).parent / "fixtures" / "swarm_v2"
    policy = {**json.loads((fixtures / "pod-policy.json").read_text()), "provider_accounts": []}
    policy["namespace"] = FX["namespace"]
    (folder / "pod-policy.json").write_text(json.dumps(policy))
    (folder / "terminal.env").write_text(
        f"AGENTIHOOKS_TERMINAL_CA_KEY={folder / 'user_ca'}\nAGENTIHOOKS_POD_POLICY_FILE={folder / 'pod-policy.json'}\n"
    )
    return root


def herdr_run(herdr):
    import subprocess

    from scripts.swarm_v2.herdr.attach import SSH

    def run(command):
        return subprocess.CompletedProcess(command, 0, "", "") if command[0] == SSH else herdr(command)

    return run


def test_the_installed_service_saves_a_published_worker_and_removes_it_when_it_ends(tmp_path):
    import io
    import json

    from scripts.swarm_v2.herdr import operator_setup
    from scripts.swarm_v2.herdr.attach import ClientHome
    from scripts.swarm_v2.kubernetes import operator_terminal

    made, herdr = store(), Herdr()
    started = worker(made, "hive-a")
    publish(made, "hive-a", *started)
    client = ClientHome(tmp_path / "client", jump=operator_terminal.jump(JUMP))
    running = operator_setup.service(JUMP, client, placed(tmp_path), herdr_run(herdr), lambda: made)
    out, slept = io.StringIO(), []

    def sleep(seconds):
        slept.append(seconds)
        end(made, "hive-a", started[0])

    assert operator_setup.serve(running, passes=2, sleep=sleep, out=out) == 0

    first, second = (json.loads(line) for line in out.getvalue().splitlines())
    assert list(first["registered"]) == [started[0]]
    assert second["retired"] == [started[0]]
    assert slept == [operator_setup.SYNC_SECONDS]
    assert cases._sidebar(herdr) == ["Local", "devbox"]
    block = client.block(Endpoint(Scope("operator/operator", "hive-a", *started), FX["namespace"]))
    assert "  ProxyJump swarm-terminal-jump" in block.read_text().splitlines()
    assert client.jump_known_hosts.read_text() == f"swarm.example {JUMP_KEY}\n"


def test_an_unchanged_pass_prints_nothing(tmp_path):
    import io

    from scripts.swarm_v2.herdr import operator_setup
    from scripts.swarm_v2.herdr.attach import ClientHome
    from scripts.swarm_v2.kubernetes import operator_terminal

    made, herdr = store(), Herdr()
    publish(made, "hive-a", *worker(made, "hive-a"))
    client = ClientHome(tmp_path / "client", jump=operator_terminal.jump(JUMP))
    running = operator_setup.service(JUMP, client, placed(tmp_path), herdr_run(herdr), lambda: made)
    out = io.StringIO()

    operator_setup.serve(running, passes=3, sleep=lambda seconds: None, out=out)

    assert len(out.getvalue().splitlines()) == 1


def test_the_service_runs_herdr_as_the_client_home_by_default(tmp_path, monkeypatch):
    from scripts.swarm_v2.herdr import operator_setup
    from scripts.swarm_v2.herdr.attach import ClientHome
    from scripts.swarm_v2.kubernetes import operator_terminal

    made, herdr, environs = store(), Herdr(), []
    publish(made, "hive-a", *worker(made, "hive-a"))

    def runner(environ):
        environs.append(environ)
        return herdr_run(herdr)

    monkeypatch.setattr(operator_setup, "runner", runner)
    client = ClientHome(tmp_path / "client", jump=operator_terminal.jump(JUMP))
    report = operator_setup.service({"PATH": "/usr/bin"}, client, placed(tmp_path), connect=lambda: made).run_pass()

    assert len(report.registered) == 1
    assert environs == [client.environment({"PATH": "/usr/bin"})]


def test_the_service_reads_the_transport_from_its_environment(tmp_path):
    import io
    import json

    from scripts.swarm_v2.herdr import operator_setup
    from scripts.swarm_v2.herdr.attach import ClientHome
    from scripts.swarm_v2.herdr.operator_service import TRANSPORT

    class Journal(io.StringIO):
        flushed = 0

        def flush(self):
            self.flushed += 1

    def sleep(seconds):
        raise AssertionError("a single pass never sleeps")

    herdr = Herdr()
    running = operator_setup.service({}, ClientHome(tmp_path / "client"), placed(tmp_path), herdr_run(herdr))
    out = Journal()

    assert operator_setup.serve(running, passes=1, sleep=sleep, out=out) == 0

    assert out.flushed == 1
    assert json.loads(out.getvalue())["refused"] == {
        TRANSPORT: "the publication transport needs AGENTIHOOKS_HIVE_REDIS_URL or AGENTIHOOKS_SWARM_REDIS_URL"
    }
    assert herdr.commands == []


def test_the_service_refuses_before_setup_has_placed_its_files(tmp_path):
    from scripts.swarm_v2.herdr import operator_setup
    from scripts.swarm_v2.herdr.attach import ClientHome

    with pytest.raises(operator_setup.SetupRefused) as refused:
        operator_setup.service({}, ClientHome(tmp_path / "client"), tmp_path / "agentihooks", herdr_run(Herdr()))
    assert str(refused.value) == "the terminal files are not placed: run agentihooks swarm-terminal setup first"


def test_a_pass_routes_through_the_jump_the_controller_recorded(tmp_path):
    from scripts.swarm_v2.herdr import operator_setup
    from scripts.swarm_v2.herdr.attach import ClientHome, Jump
    from scripts.swarm_v2.kubernetes import jump

    made, herdr = store(), Herdr()
    started = worker(made, "hive-a")
    publish(made, "hive-a", *started)
    jump.record(made, Jump("swarm.example", 2223, JUMP_KEY))
    running = operator_setup.service(
        {}, ClientHome(tmp_path / "client"), placed(tmp_path), herdr_run(herdr), lambda: made
    )

    report = running.run_pass()

    home = running.upkeep.terminal.home
    assert list(report.registered) == [started[0]]
    assert home == ClientHome(tmp_path / "client", jump=Jump("swarm.example", 2223, JUMP_KEY))
    block = home.block(Endpoint(Scope("operator/operator", "hive-a", *started), FX["namespace"]))
    assert "  ProxyJump swarm-terminal-jump" in block.read_text().splitlines()
    assert home.jump_known_hosts.read_text() == f"swarm.example {JUMP_KEY}\n"


def test_a_new_jump_record_rewrites_the_saved_workers(tmp_path):
    from scripts.swarm_v2.herdr import operator_setup
    from scripts.swarm_v2.herdr.attach import ClientHome, Jump
    from scripts.swarm_v2.kubernetes import jump

    made, herdr = store(), Herdr()
    publish(made, "hive-a", *worker(made, "hive-a"))
    jump.record(made, Jump("swarm.example", 2223, JUMP_KEY))
    running = operator_setup.service(
        {}, ClientHome(tmp_path / "client"), placed(tmp_path), herdr_run(herdr), lambda: made
    )
    running.run_pass()
    rotated = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIRotatedJumpKey"
    jump.record(made, Jump("swarm.example", 2223, rotated))

    assert running.run_pass() is not None

    assert running.upkeep.terminal.home.jump_known_hosts.read_text() == f"swarm.example {rotated}\n"
    assert running.run_pass() is None


def test_without_a_jump_record_the_client_keeps_its_own_jump(tmp_path):
    from scripts.swarm_v2.herdr import operator_setup
    from scripts.swarm_v2.herdr.attach import ClientHome
    from scripts.swarm_v2.kubernetes import operator_terminal

    made, herdr = store(), Herdr()
    publish(made, "hive-a", *worker(made, "hive-a"))
    client = ClientHome(tmp_path / "client", jump=operator_terminal.jump(JUMP))
    running = operator_setup.service(JUMP, client, placed(tmp_path), herdr_run(herdr), lambda: made)

    running.run_pass()

    assert running.upkeep.terminal.home == client
