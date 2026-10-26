import os
import subprocess
from pathlib import Path
from tempfile import TemporaryFile
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from redis import Redis


def checked(probe: Callable[[], object]) -> bool:
    try:
        return bool(probe())
    except Exception:
        return False


def github_ready() -> bool:
    return checked(
        lambda: (
            subprocess.run(
                [
                    "gh",
                    "api",
                    "installation/repositories" if os.environ.get("HIVE_GITHUB_AUTH_MODE", "app") == "app" else "user",
                    "--silent",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            ).returncode
            == 0
        )
    )


def transcript_ready(folder: str) -> bool:
    if not folder:
        return False
    with TemporaryFile(dir=folder):
        return True


def settings_ready(context: dict) -> bool:
    from scripts.swarm.live_binding import hooks

    return hooks(Path(context["home"]), context["harness"])


def pane_ready(pane: str) -> bool:
    from scripts.swarm.runtime import herdr_call

    return bool(pane) and pane in {row["pane_id"] for row in herdr_call(["pane", "list"])["panes"]}


def _status(redis, store, slug):
    redis.get(store.key(slug, "published"))
    redis.hgetall(store.key(slug, "agents"))
    return True


def _verdict(redis, store, slug, execution):
    key, field = store.key(slug, "findings"), f"join-check-{execution}"
    try:
        redis.hset(key, field, "join")
        return redis.hget(key, field) in ("join", b"join")
    finally:
        redis.hdel(key, field)


def redis_checks(redis: "Redis", slug: str, execution: str) -> dict:
    from scripts.swarm.store import RedisStore

    store = RedisStore(redis)
    task = f"join-check-{execution}"
    claimed = checked(lambda: store.claim(slug, task, execution, 60_000))
    released = checked(lambda: store.release(slug, task, execution))
    checked(lambda: redis.delete(store.key(slug, "claim", task, "generation")))
    return {
        "redis_claim": claimed,
        "redis_release": released,
        "redis_status": checked(lambda: _status(redis, store, slug)),
        "redis_verdict": checked(lambda: _verdict(redis, store, slug, execution)),
    }


def cheap(redis: "Redis", report: dict) -> dict:
    from hooks.context.profile_chain import rendered_overlays
    from scripts.swarm.launch_check import chain

    context = report["context"]
    return {
        **redis_checks(redis, report["slug"], report["execution_id"]),
        "transcript": checked(lambda: transcript_ready(context["transcript_dir"])),
        "profile": checked(lambda: context["profile"] in chain(context["home"])),
        "settings": checked(lambda: settings_ready(context)),
        "pane": checked(lambda: pane_ready(context["pane"])),
        "started_at": report["started_at"] > 0,
        "brain": checked(lambda: "brain" in rendered_overlays(Path(context["home"]))),
    }


def ledger_checks(report: dict) -> dict:
    from scripts.swarm.ledger_client import LedgerClient, _op

    ledger = LedgerClient()
    checks = report.get("checks", {})
    slug, name = report["slug"], report["agent"]
    return {
        "ledger_join": checks.get("ledger_join") is True or checked(lambda: ledger.join(slug, name, "member") is None),
        "ledger_write": checks.get("ledger_write") is True
        or checked(lambda: ledger._call(slug, [_op("ack", name, rev=0)])),
    }


def startup(redis: "Redis", report: dict) -> dict:
    return {**cheap(redis, report), **ledger_checks(report), "github": github_ready()}
