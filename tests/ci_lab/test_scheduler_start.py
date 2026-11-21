from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.ci_lab import relay, scheduler
from scripts.ci_lab.delivery import CLASSES
from tests.ci_lab.test_scheduler import APP_SETTINGS


def refused(name: str) -> Callable[..., Any]:
    """Return a stand in for a dependency the scheduler must not build before its GitHub App settings hold."""

    def build(*_arguments: Any, **_options: Any) -> Any:
        raise AssertionError(f"the scheduler built its {name} before checking its GitHub App settings")

    return build


@pytest.mark.feature("lab-scheduler-github-app-settings-on-start")
@pytest.mark.parametrize(
    ("settings", "missing"),
    [
        ({}, "GITHUB_APP_ID, GITHUB_APP_INSTALLATION_ID, GITHUB_APP_PRIVATE_KEY"),
        ({"GITHUB_APP_INSTALLATION_ID": "2"}, "GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY"),
        ({"GITHUB_APP_ID": "1", "GITHUB_APP_PRIVATE_KEY": "fixture"}, "GITHUB_APP_INSTALLATION_ID"),
        (APP_SETTINGS | {"GITHUB_APP_ID": ""}, "GITHUB_APP_ID"),
    ],
)
def test_main_refuses_to_start_naming_each_missing_github_app_setting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: dict[str, str],
    missing: str,
) -> None:
    import boto3  # pyright: ignore[reportMissingTypeStubs]
    import redis

    from scripts.events import factory

    runtime = tmp_path / "runtime.yaml"
    runtime.write_text(
        yaml.safe_dump(
            {
                "classes": list(CLASSES),
                "budget": {"coreMinutesPerDay": 600, "shed": {"backfill": 0.5}},
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
    } | settings
    monkeypatch.setattr(redis.Redis, "from_url", refused("Redis client"))
    monkeypatch.setattr(boto3, "client", refused("object store client"))
    monkeypatch.setattr(factory, "open_bus", refused("event bus"))
    monkeypatch.setattr(scheduler, "build", refused("scheduler"))
    monkeypatch.setattr(relay, "build", refused("relay"))

    assert scheduler.main([], environ) == 1

    assert capsys.readouterr().err == f"ci-lab scheduler refuses to start: {missing} not set\n"
