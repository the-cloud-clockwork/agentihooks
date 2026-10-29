import os
import socket
import stat

import pytest

from scripts.swarm import stack_env

PASSWORD = "pw-test"


@pytest.fixture
def fixed(monkeypatch):
    monkeypatch.setattr(stack_env.secrets, "token_urlsafe", lambda: PASSWORD)
    monkeypatch.setattr(stack_env, "free_port", lambda preferred: preferred + 1)


def test_first_settings_are_generated_private(tmp_path, fixed):
    env = tmp_path / ".env"
    values = stack_env.settings(env)
    assert env.read_text() == (
        "AGENTIHOOKS_DEPLOYMENT=compose\n"
        "SWARM_BIND=127.0.0.1\n"
        "SWARM_HIVE_ID=compose-controller\n"
        "SWARM_IMAGE_TAG=release\n"
        f"SWARM_REDIS_PASSWORD={PASSWORD}\n"
        "SWARM_PORT=8766\n"
        "SWARM_REDIS_PORT=6380\n"
    )
    assert values["SWARM_PORT"] == "8766"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_complete_settings_are_kept_untouched_and_made_private(tmp_path, monkeypatch, fixed):
    env = tmp_path / ".env"
    stack_env.settings(env)
    env.chmod(0o644)
    os.utime(env, ns=(1, 1))
    monkeypatch.setattr(stack_env.secrets, "token_urlsafe", lambda: "other")
    monkeypatch.setattr(stack_env, "free_port", lambda preferred: pytest.fail("ports are chosen once"))
    assert stack_env.settings(env)["SWARM_REDIS_PASSWORD"] == PASSWORD
    assert env.stat().st_mtime_ns == 1
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_missing_settings_are_filled_in(tmp_path, fixed):
    env = tmp_path / ".env"
    env.write_text("# SWARM_BIND=old\njunk\nSWARM_PORT = 9200\nSWARM_HIVE_ID=a=b\nSWARM_REDIS_PASSWORD=\n")
    stack_env.settings(env)
    assert env.read_text() == (
        "SWARM_PORT=9200\n"
        "SWARM_HIVE_ID=a=b\n"
        f"SWARM_REDIS_PASSWORD={PASSWORD}\n"
        "AGENTIHOOKS_DEPLOYMENT=compose\n"
        "SWARM_BIND=127.0.0.1\n"
        "SWARM_IMAGE_TAG=release\n"
        "SWARM_REDIS_PORT=6380\n"
    )


def test_client_settings_follow_the_stack_settings(tmp_path):
    values = {"SWARM_REDIS_PASSWORD": PASSWORD, "SWARM_REDIS_PORT": "6380", "SWARM_PORT": "8766"}
    url = "swarm:" + PASSWORD + "@127.0.0.1:6380/0"
    assert stack_env.client_env(values, tmp_path) == (
        f"AGENTIHOOKS_DEPLOYMENT=compose\nLEDGER_URL=http://127.0.0.1:8766\nAGENTIHOOKS_SWARM_REDIS_URL=redis://{url}\n"
    )
    tls = stack_env.client_env({**values, "SWARM_REDIS_SCHEME": "rediss"}, tmp_path)
    assert tls.endswith(f"=rediss://{url}\nSSL_CERT_FILE={tmp_path}/deploy/compose/tls/ca.pem\n")
    moved = stack_env.client_env({**values, "SWARM_REDIS_SCHEME": "rediss", "SWARM_TLS_DIR": "/certs/swarm"}, tmp_path)
    assert moved.endswith("\nSSL_CERT_FILE=/certs/swarm/ca.pem\n")


def test_write_private_replaces_a_longer_file_and_makes_it_private(tmp_path):
    path = tmp_path / "client.env"
    path.write_text("a much longer earlier text\n")
    path.chmod(0o644)
    stack_env.write_private(path, "short\n")
    assert path.read_text() == "short\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    fresh = tmp_path / "fresh.env"
    stack_env.write_private(fresh, "x\n")
    assert stat.S_IMODE(fresh.stat().st_mode) == 0o600


def test_write_private_creates_the_file_private_before_any_chmod(tmp_path, monkeypatch):
    monkeypatch.setattr(stack_env.os, "fchmod", lambda fd, mode: None)
    previous = os.umask(0)
    try:
        stack_env.write_private(tmp_path / "fresh.env", "x\n")
    finally:
        os.umask(previous)
    assert stat.S_IMODE((tmp_path / "fresh.env").stat().st_mode) == 0o600


def test_free_port_prefers_the_default_and_falls_back_to_any_free_port():
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen()
        busy = held.getsockname()[1]
        other = stack_env.free_port(busy)
        assert other >= 1024 and other != busy
    assert stack_env.free_port(busy) == busy
