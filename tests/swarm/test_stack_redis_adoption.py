import os
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.swarm import stack_init

pytestmark = pytest.mark.xdist_group("fakeredis")

REDIS = stack_init.BUNDLED / "deploy" / "compose" / "redis"
TOKEN = secrets.token_urlsafe()


def rendered(tmp_path: Path, adopted: bool) -> list[str]:
    for folder in ("compose", "data", "bin"):
        (tmp_path / folder).mkdir()
    shutil.copyfile(REDIS / "users.acl.template", tmp_path / "compose" / "users.acl.template")
    entry = tmp_path / "bin" / "docker-entrypoint.sh"
    entry.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@"\n')
    entry.chmod(0o755)
    env = {
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "SWARM_REDIS_PASSWORD": TOKEN,
        "SWARM_REDIS_SCHEME": "redis",
        "SWARM_REDIS_ROOT": str(tmp_path),
    }
    if adopted:
        env["SWARM_REDIS_ADOPTED"] = "1"
    done = subprocess.run(["bash", str(REDIS / "start.sh")], env=env, capture_output=True, text=True, check=True)
    assert done.stdout.splitlines() == [
        "redis-server",
        "--appendonly",
        "yes",
        "--aclfile",
        f"{tmp_path}/data/users.acl",
    ]
    return (tmp_path / "data" / "users.acl").read_text().splitlines()


def swarm_client(lines: list[str]):
    import fakeredis
    import redis

    server = fakeredis.FakeServer()
    admin = fakeredis.FakeRedis(server=server)
    for line in lines:
        _, name, *rules = line.split()
        if name != "default":
            admin.execute_command("ACL", "SETUSER", name, *rules)
    client = fakeredis.FakeRedis(server=server, username="swarm", password=TOKEN)
    return client, redis.exceptions.NoPermissionError


def test_an_adopting_stack_refuses_every_flush_by_the_swarm_user(tmp_path):
    lines = rendered(tmp_path, adopted=True)
    assert lines[1].endswith(" ~* &* +@all -flushall -flushdb")
    client, refused = swarm_client(lines)
    client.set("swarm:kept", "1")
    for flush in (client.flushall, client.flushdb):
        with pytest.raises(refused):
            flush()
    assert client.get("swarm:kept") == b"1"


def test_a_stack_without_adoption_keeps_its_flush_commands(tmp_path):
    lines = rendered(tmp_path, adopted=False)
    assert lines[1].endswith(" ~* &* +@all")
    client, _ = swarm_client(lines)
    client.set("swarm:gone", "1")
    assert client.flushdb()
    assert client.get("swarm:gone") is None


def test_the_adoption_override_marks_the_stack_redis_adopted():
    import yaml

    override = yaml.safe_load((stack_init.BUNDLED / stack_init.ADOPTION).read_text())
    assert override["services"]["redis"] == {"environment": {"SWARM_REDIS_ADOPTED": "1"}}
