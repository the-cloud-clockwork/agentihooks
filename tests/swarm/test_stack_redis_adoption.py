import os
import secrets
import shutil
import subprocess
from pathlib import Path

from scripts.hive import auth
from scripts.swarm import stack_init
from tests.swarm.compose_loader import compose_file

REDIS = stack_init.BUNDLED / "deploy" / "compose" / "redis"
TOKEN = secrets.token_urlsafe()
HIVE = f"user hive-abc on #00 ~swarm:hive:abc:* &* {' '.join(auth.ACL_CATEGORIES)}"


def rendered(tmp_path: Path, adopted: bool) -> dict[str, list[str]]:
    for folder in ("compose", "data", "bin"):
        (tmp_path / folder).mkdir()
    shutil.copyfile(REDIS / "users.acl.template", tmp_path / "compose" / "users.acl.template")
    (tmp_path / "data" / "users.acl").write_text(HIVE + "\n")
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
    lines = (tmp_path / "data" / "users.acl").read_text().splitlines()
    return {line.split()[1]: line.split()[2:] for line in lines}


def flush_refused(rules: list[str]) -> bool:
    if "off" in rules:
        return True
    allowed = {"flushall": False, "flushdb": False}
    for rule in rules:
        for command in allowed:
            if rule in ("+@all", f"+{command}"):
                allowed[command] = True
            elif rule in ("-@all", "-@dangerous", f"-{command}"):
                allowed[command] = False
    return not any(allowed.values())


def test_an_adopting_stack_refuses_every_flush_by_every_user(tmp_path):
    users = rendered(tmp_path, adopted=True)
    assert set(users) == {"default", "swarm", "hive-abc"}
    assert users["swarm"][-3:] == ["+@all", "-flushall", "-flushdb"]
    assert all(flush_refused(rules) for rules in users.values()), users


def test_a_stack_without_adoption_keeps_the_swarm_user_flush(tmp_path):
    users = rendered(tmp_path, adopted=False)
    assert users["swarm"][-1] == "+@all"
    assert not flush_refused(users["swarm"])


def test_the_adoption_override_marks_the_stack_redis_adopted():
    override = compose_file(stack_init.BUNDLED / stack_init.ADOPTION)
    assert override["services"]["redis"] == {"environment": {"SWARM_REDIS_ADOPTED": "1"}}
