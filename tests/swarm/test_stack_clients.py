import io
import stat
import urllib.parse

import pytest

from scripts.swarm import stack_env, stack_init

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def redis_source(monkeypatch):
    import fakeredis
    import redis

    server = fakeredis.FakeServer()

    def connect(url, **kwargs):
        parsed = urllib.parse.urlsplit(url)
        assert parsed.hostname == "127.0.0.1"
        assert parsed.port == 6379
        return fakeredis.FakeRedis(
            server=server,
            decode_responses=True,
            protocol=2,
            username=parsed.username,
            password=parsed.password,
        )

    monkeypatch.setattr(redis.Redis, "from_url", connect)
    return connect("redis://127.0.0.1:6379/0")


@pytest.fixture
def prepared(tmp_path, redis_source):
    home = tmp_path / "agentihooks"
    stack = home / "swarm-stack"
    stack.mkdir(parents=True)
    bundle = stack_env.settings(stack / ".env")
    redis_source.acl_setuser(
        "swarm", enabled=True, passwords=["+" + bundle["SWARM_REDIS_PASSWORD"]], keys=["*"], commands=["+@all"]
    )
    redis_source.config_set("aclfile", "/data/users.acl")
    environ = {"HOME": str(tmp_path), "AGENTIHOOKS_HOME": str(home)}
    return home, bundle, environ, io.StringIO()


def no_docker(*args, **kwargs):
    pytest.fail("client preparation must not change containers")


def test_prepare_clients_keeps_the_live_store_and_legacy_clients(prepared, redis_source):
    import redis

    from scripts import operator_env

    home, bundle, environ, out = prepared
    redis_source.set("test-stage:inbox:retained", "pending")
    assert stack_init.init(["--prepare-clients"], environ, no_docker, no_docker, out) == 0, out.getvalue()
    loaded = operator_env.values(environ)
    endpoint = urllib.parse.urlsplit(loaded["AGENTIHOOKS_SWARM_REDIS_URL"])
    assert (endpoint.hostname, endpoint.port, endpoint.username, endpoint.password) == (
        "127.0.0.1",
        6379,
        "swarm",
        bundle["SWARM_REDIS_PASSWORD"],
    )
    assert "AGENTIHOOKS_DEPLOYMENT" not in loaded
    assert "LEDGER_URL" not in loaded
    assert redis_source.ping() is True
    assert redis_source.get("test-stage:inbox:retained") == "pending"
    named = redis.Redis.from_url(loaded["AGENTIHOOKS_SWARM_REDIS_URL"])
    assert named.acl_whoami() == "swarm"
    assert named.get("test-stage:inbox:retained") == "pending"
    assert bundle["SWARM_REDIS_PASSWORD"] not in out.getvalue()
    assert stat.S_IMODE((home / "swarm-client.env").stat().st_mode) == 0o600
    assert (
        out.getvalue()
        == "Named Redis clients activated on the existing store; services, data and ports are unchanged.\n"
    )
    assert stack_init.init(["--prepare-clients"], environ, no_docker, no_docker, out) == 0, out.getvalue()
    assert operator_env.values(environ)["AGENTIHOOKS_SWARM_REDIS_URL"] == loaded["AGENTIHOOKS_SWARM_REDIS_URL"]


@pytest.mark.parametrize(
    "problem,message",
    [
        ("missing_password", "Run agentihooks swarm init before preparing existing clients.\n"),
        ("foreign_source", "Client preparation requires the existing localhost Redis endpoint.\n"),
        ("wrong_scheme", "Client preparation requires the existing localhost Redis endpoint.\n"),
        ("missing_aclfile", "Deploy the persistent Redis ACL file before activating clients.\n"),
        ("passwordless", "The named swarm user must require its bundle password.\n"),
        ("missing_user", "Client preparation failed (AuthenticationError); existing client settings are unchanged.\n"),
    ],
)
def test_failed_preparation_preserves_previous_activation(prepared, redis_source, problem, message):
    home, bundle, environ, out = prepared
    target = home / "swarm-client.env"
    stack_env.write_private(target, "PREVIOUS=kept\n")
    if problem == "missing_password":
        stack_env.write_private(home / "swarm-stack" / ".env", "SWARM_PORT=8766\n")
    elif problem == "foreign_source":
        environ["AGENTIHOOKS_SWARM_REDIS_URL"] = "redis://remote.example:6379/0"
    elif problem == "wrong_scheme":
        environ["AGENTIHOOKS_SWARM_REDIS_URL"] = "http://127.0.0.1:6379/0"
    elif problem == "missing_aclfile":
        redis_source.config_set("aclfile", "")
    elif problem == "passwordless":
        redis_source.acl_setuser("swarm", nopass=True)
    else:
        redis_source.acl_deluser("swarm")
    assert stack_init.init(["--prepare-clients"], environ, no_docker, no_docker, out) == 1
    assert out.getvalue() == message
    assert target.read_text() == "PREVIOUS=kept\n"
    assert bundle["SWARM_REDIS_PASSWORD"] not in out.getvalue()


def test_failed_atomic_publication_preserves_previous_activation(prepared):
    from unittest.mock import patch

    from scripts.swarm import stack_clients

    home, bundle, environ, out = prepared
    target = home / "swarm-client.env"
    stack_env.write_private(target, "PREVIOUS=kept\n")
    with patch.object(stack_clients.os, "replace", side_effect=OSError):
        assert stack_init.init(["--prepare-clients"], environ, no_docker, no_docker, out) == 1
    assert out.getvalue() == "Client preparation failed (OSError); existing client settings are unchanged.\n"
    assert target.read_text() == "PREVIOUS=kept\n"
    assert stat.S_IMODE((home / "swarm-client.env.next").stat().st_mode) == 0o600
    assert bundle["SWARM_REDIS_PASSWORD"] not in out.getvalue()
