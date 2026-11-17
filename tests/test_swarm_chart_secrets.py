import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
DATABASE = {"name": "AGENTIHOOKS_SWARM_DATABASE_URL", "secretName": "swarm-ledger-database", "key": "url"}
TOKEN = {"name": "LEDGER_TOKEN", "secretName": "swarm-ledger-token", "key": "token"}
NONCE = {"name": "LEDGER_NONCE", "secretName": "swarm-ledger-token", "key": "nonce"}
TERMINAL = {"name": "AGENTIHOOKS_WORKER_TERMINAL", "secretName": "swarm-worker-terminal", "key": "setting"}


def _check(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    for name in ("chart_workers", "chart_secrets"):
        spec = importlib.util.spec_from_file_location(name, ROOT / "tests" / f"{name}.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
    return sys.modules["chart_secrets"]


def _env(entry: dict[str, str], name: str = "") -> dict[str, object]:
    reference = {"name": entry["secretName"], "key": entry["key"]}
    return {"name": name or entry["name"], "valueFrom": {"secretKeyRef": reference}}


def _workload(kind: str, name: str, container: str, env: list[dict[str, object]]) -> dict[str, object]:
    pod = {"containers": [{"name": container, "env": env}]}
    return {"kind": kind, "metadata": {"name": name}, "spec": {"template": {"spec": pod}}}


def _problems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entries: list[dict[str, str]], *extra: dict[str, object]
) -> list[str]:
    values = tmp_path / "values.yaml"
    values.write_text(yaml.safe_dump({"controller": {"secretEnv": [TERMINAL]}, "ledger": {"secretEnv": entries}}))
    ledger = _workload("StatefulSet", "fx-swarm-ledger", "ledger", [_env(entry) for entry in entries])
    controller = _workload("Deployment", "fx-swarm-controller", "controller", [_env(DATABASE), _env(TERMINAL)])
    return _check(monkeypatch).main([str(values)], yaml.safe_dump_all([ledger, controller, *extra]))


@pytest.mark.parametrize("entries", [[DATABASE, TOKEN], [DATABASE, TOKEN, NONCE]], ids=["one-each", "shared-secret"])
def test_the_secret_check_lets_the_ledger_api_mirror_every_ledger_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entries: list[dict[str, str]]
) -> None:
    api = _workload("Deployment", "fx-swarm-api", "api", [_env(entry) for entry in entries])

    assert _problems(tmp_path, monkeypatch, entries, api) == []


@pytest.mark.parametrize(
    ("workload", "flagged"),
    [
        (
            _workload("StatefulSet", "fx-swarm-api", "api", [_env(TOKEN)]),
            "StatefulSet fx-swarm-api container api references the ledger only Secret swarm-ledger-token",
        ),
        (
            _workload("Deployment", "fx-swarm-hub", "api", [_env(TOKEN)]),
            "Deployment fx-swarm-hub container api references the ledger only Secret swarm-ledger-token",
        ),
        (
            _workload("Deployment", "fx-swarm-api", "sidecar", [_env(TOKEN)]),
            "Deployment fx-swarm-api container sidecar references the ledger only Secret swarm-ledger-token",
        ),
        (
            _workload("Deployment", "fx-swarm-api", "api", [_env(TOKEN), _env(TOKEN, "COPIED_TOKEN")]),
            "Deployment fx-swarm-api container api references the ledger only Secret swarm-ledger-token",
        ),
        (
            _workload("Deployment", "fx-swarm-api", "api", [_env(TERMINAL)]),
            "Deployment fx-swarm-api container api references the controller only Secret swarm-worker-terminal",
        ),
    ],
    ids=["statefulset", "other-deployment", "other-container", "second-reference", "controller-secret"],
)
def test_the_secret_check_flags_a_ledger_api_reference_beyond_the_mirror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workload: dict[str, object], flagged: str
) -> None:
    assert _problems(tmp_path, monkeypatch, [DATABASE, TOKEN], workload) == [flagged]
