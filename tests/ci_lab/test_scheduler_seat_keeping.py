"""Scheduler main wires the lease API's stale-restart seam with the wanted image and the pods port reader, and its shed
port with the daily core budget; kept out of tests/ci_lab/test_scheduler.py, which is at the file-lines cap."""

import ssl
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple

import pytest
import yaml

from scripts.ci_lab import budget, pods, relay, scheduler
from scripts.ci_lab.delivery import CLASSES
from scripts.ci_lab.leases import Leases
from tests.ci_lab.test_scheduler import APP_SETTINGS, Expander, Inbox, Poster, RelayBuilds, fake_redis

DAY_MS = 1_795_700_000_000


class Served(NamedTuple):
    leases: Leases
    spending: budget.Budget


def run_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, managed: pods.Pods, shed: dict[str, float], image: str = ""
) -> Served:
    """Run scheduler main with build and listen faked and its clock pinned to one UTC day; return the leases it serves
    and the budget it builds."""
    import boto3  # pyright: ignore[reportMissingTypeStubs]
    import redis

    from scripts.events import config, factory

    client = fake_redis()
    served: list[Leases] = []
    spent: list[budget.Budget] = []
    bus = SimpleNamespace(lab_heads=SimpleNamespace(consumer=lambda durable, subject: "consumer"))

    def build(environ: Any, redis_client: Any, objects: Any, consumer: Any, spending: Any) -> scheduler.Scheduler:
        spent.append(spending)
        ports = scheduler.Ports(Inbox(), Expander({}), lambda repo: Poster(), frozenset[str], managed)
        return scheduler.Scheduler(redis_client, objects, ports, scheduler.Settings(frozenset()), spending)

    def listen(found: Any, leases: Leases, *rest: Any) -> list[Any]:
        served.append(leases)
        return []

    runtime = tmp_path / "runtime.yaml"
    runtime.write_text(
        yaml.safe_dump(
            {
                "classes": list(CLASSES),
                "budget": {"coreMinutesPerDay": 600, "shed": shed},
                "backfill": {"auditShare": 0.1, "moduleSweeps": False},
            }
        )
    )
    environ = {
        scheduler.contract.REDIS_URL: "redis://lab",
        scheduler.contract.RUNTIME_CONFIG: str(runtime),
        scheduler.MINIO_ENDPOINT: "endpoint",
        scheduler.MINIO_ACCESS_KEY: "access",
        scheduler.MINIO_SECRET_KEY: "secret",
        scheduler.MINIO_BUCKET: "bucket",
        scheduler.contract.GIT_MIRROR: str(tmp_path / "node"),
    }
    if image:
        environ[scheduler.IMAGE] = image
    bound = config.BusConfig(backend="nats", url="nats://lab", provision=False)
    monkeypatch.setattr(redis.Redis, "from_url", lambda url, **options: client)
    monkeypatch.setattr(boto3, "client", lambda name, **options: "s3 client")
    monkeypatch.setattr(
        config.BusConfig, "from_environ", classmethod(lambda cls, found: config.BusConfig("nats", "nats://lab"))
    )
    monkeypatch.setattr(factory, "open_bus", lambda found: bus if found == bound else None)
    monkeypatch.setattr(scheduler, "_now_ms", lambda: DAY_MS)
    monkeypatch.setattr(scheduler, "build", build)
    monkeypatch.setattr(scheduler, "listen", listen)
    monkeypatch.setattr(scheduler, "serve", lambda *args: 7)
    monkeypatch.setattr(relay, "build", RelayBuilds())

    assert scheduler.main([], environ | APP_SETTINGS) == 7
    return Served(served[0], spent[0])


@pytest.mark.feature("lab-worker-seat-keeping-restart")
def test_main_builds_leases_with_the_wanted_image_and_the_pods_port_image_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kube = pods.KubePods("https://api:6443", "ci-lab", tmp_path / "token", ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))

    leases = run_main(tmp_path, monkeypatch, kube, {"backfill": 0.5}, "ghcr.io/lab-worker@sha256:wanted").leases

    assert leases.image == "ghcr.io/lab-worker@sha256:wanted"
    assert leases.images == kube.images


@pytest.mark.feature("lab-worker-seat-keeping-restart")
def test_main_gives_leases_the_unmanaged_image_reader_outside_a_cluster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    leases = run_main(tmp_path, monkeypatch, pods.unmanaged, {"backfill": 0.5}).leases

    assert leases.image == ""
    assert leases.images is pods.unread


@pytest.mark.feature("lab-lease-shed-class")
def test_main_gives_leases_the_classes_its_daily_core_budget_sheds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    leases, spending = run_main(tmp_path, monkeypatch, pods.unmanaged, {"backfill": 0.5, "promotion": 0.75})

    assert tuple(leases.shed()) == ()
    spending.charge(1, 300 * 60)
    assert tuple(leases.shed()) == ("backfill",)
    spending.charge(1, 150 * 60)
    assert tuple(leases.shed()) == ("backfill", "promotion")
