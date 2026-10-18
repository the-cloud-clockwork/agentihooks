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


def test_prepare_clients_keeps_the_live_store_and_legacy_clients(tmp_path, redis_source):
    from scripts import operator_env

    home = tmp_path / "agentihooks"
    stack = home / "swarm-stack"
    stack.mkdir(parents=True)
    bundle = stack_env.settings(stack / ".env")
    redis_source.acl_setuser(
        "swarm", enabled=True, passwords=["+" + bundle["SWARM_REDIS_PASSWORD"]], keys=["*"], commands=["+@all"]
    )
    redis_source.config_set("aclfile", "/data/users.acl")
    redis_source.set("test-stage:inbox:retained", "pending")
    environ = {"HOME": str(tmp_path), "AGENTIHOOKS_HOME": str(home)}
    out = io.StringIO()

    def no_docker(*args, **kwargs):
        pytest.fail("client preparation must not change containers")

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
    assert bundle["SWARM_REDIS_PASSWORD"] not in out.getvalue()
    assert stat.S_IMODE((home / "swarm-client.env").stat().st_mode) == 0o600
    assert stack_init.init(["--prepare-clients"], environ, no_docker, no_docker, out) == 0, out.getvalue()
    assert operator_env.values(environ)["AGENTIHOOKS_SWARM_REDIS_URL"] == loaded["AGENTIHOOKS_SWARM_REDIS_URL"]
