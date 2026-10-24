import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "deploy/compose"


@pytest.fixture
def tailnet(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_REDIS_PASSWORD", secrets.token_urlsafe())
    stack = tmp_path / "stack"
    stack.mkdir()
    shutil.copyfile(ROOT / "compose.yaml", stack / "compose.yaml")
    shutil.copytree(ASSETS, stack / "deploy/compose")
    (stack / ".env").write_text(
        "SWARM_ADOPT_HOME=/fixture\nSWARM_ADOPT_UID=1000\nSWARM_ADOPT_GID=1000\n"
        "SWARM_ADOPT_CODE=/fixture/code\nSWARM_ADOPT_LEDGER_DIR=/fixture/ledger\n"
        "SWARM_ADOPT_REDIS_URL=redis://127.0.0.1:48181/0\nSWARM_TAILNET_IP=100.64.0.99\n"
    )
    binary = tmp_path / "bin"
    binary.mkdir()
    cli = binary / "tailscale"
    cli.write_text('#!/bin/sh\nprintf "%s\\n" "$FIXTURE_TAILNET_IP"\n')
    cli.chmod(0o755)
    return stack, binary


@pytest.mark.parametrize("address", ["100.96.208.118", "100.101.12.34"])
def test_tailnet_start_renders_all_fronts_with_the_current_address(tailnet, address):
    stack, binary = tailnet
    result = subprocess.run(
        ["bash", str(stack / "deploy/compose/start-tailnet.sh"), "config", "--format", "json"],
        env={**os.environ, "PATH": f"{binary}:{os.environ['PATH']}", "FIXTURE_TAILNET_IP": address},
        capture_output=True,
        text=True,
        check=True,
    )
    services = json.loads(result.stdout)["services"]
    ledger = services["swarm-tailnet"]
    assert ledger["environment"]["SWARM_TAILNET_IP"] == address
    assert ledger["environment"]["NGINX_ENVSUBST_FILTER"] in ("^SWARM_TAILNET_IP$", "^SWARM_TAILNET_IP$$")
    assert any(v["target"] == "/etc/nginx/templates/default.conf.template" for v in ledger["volumes"])
    assert services["hive-tailnet"]["command"][3] == f"--host={address}"
    assert f"--redis-url=redis://{address}:6379/0" in services["hive-tailnet"]["command"]
    assert services["redis-tailnet"]["command"] == [
        f"TCP-LISTEN:6379,bind={address},fork,reuseaddr",
        "TCP:127.0.0.1:6379",
    ]
    for name in ("swarm-tailnet", "hive-tailnet", "redis-tailnet"):
        assert services[name]["network_mode"] == "host"
    assert "100.64.0.99" not in json.dumps(services)


@pytest.mark.parametrize("address", ["", "garbage", "0.0.0.0", "100.96.208.999", "100.96.208.1\n100.96.208.2"])
def test_tailnet_start_refuses_an_unavailable_address_before_running_docker(tailnet, address):
    stack, binary = tailnet
    docker = binary / "docker"
    docker.write_text('#!/bin/sh\necho "docker must not run" >&2\nexit 99\n')
    docker.chmod(0o755)
    result = subprocess.run(
        ["bash", str(stack / "deploy/compose/start-tailnet.sh"), "config"],
        env={**os.environ, "PATH": f"{binary}:{os.environ['PATH']}", "FIXTURE_TAILNET_IP": address},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "No current tailnet IPv4 address" in result.stderr
    assert "docker must not run" not in result.stderr


def test_tailnet_assets_ship_with_the_stack_and_template_the_ledger_listener():
    for name in ("tailnet.yaml", "start-tailnet.sh", "ledger-tailnet.conf.template"):
        assert (ASSETS / name).read_bytes() == (ROOT / "scripts/swarm/stack/deploy/compose" / name).read_bytes()
    template = (ASSETS / "ledger-tailnet.conf.template").read_text()
    assert "listen ${SWARM_TAILNET_IP}:8765 ssl;" in template
    assert "proxy_pass http://127.0.0.1:8765;" in template
