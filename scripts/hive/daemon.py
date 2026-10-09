import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from redis import Redis

from hooks.context import account_sessions, broadcast, quota_usage
from scripts import claude_quota_balancer as balancer
from scripts import codex_router, session_bands
from scripts.hive import registry
from scripts.routing import claude_api, codex_api
from scripts.swarm.commands import hive_id

BEAT_SECONDS = 15
TTL_SECONDS = BEAT_SECONDS * 3
UNIT = "agentihooks-hive.service"


def beat(redis: Redis, local_id: str) -> None:
    report = observe()
    key = registry.key(local_id)
    with redis.pipeline() as pipe:
        for field, value in registry._default(local_id).items():
            pipe.hsetnx(key, field, registry._encode(field, value))
        fields = {name: json.dumps(report[name]) for name in ("slots", "interactive", "repos")}
        fields["heartbeat_at"] = str(int(time.time() * 1000))
        pipe.hset(key, mapping=fields)
        pipe.sadd(registry.INDEX, local_id)
        for field in ("sessions", "quota"):
            pipe.set(f"{key}:{field}", json.dumps(report[field]), ex=TTL_SECONDS)
        pipe.execute()


def run(redis: Redis) -> None:
    local_id = hive_id()
    while True:
        started = time.monotonic()
        beat(redis, local_id)
        time.sleep(max(0, BEAT_SECONDS - (time.monotonic() - started)))


def unit(binary: str) -> str:
    return (
        "[Unit]\nDescription=agentihooks hive heartbeat\n\n"
        "[Service]\nType=simple\n"
        "Environment=PATH=%h/.local/bin:%h/.cargo/bin:/usr/local/bin:/usr/bin:/bin\n"
        "EnvironmentFile=-%h/.agentihooks/.env\n"
        "EnvironmentFile=-%h/.agentihooks/hive.env\n"
        f'ExecStart="{binary}" hive run\n'
        "Restart=always\nRestartSec=5\n\n"
        "[Install]\nWantedBy=default.target\n"
    )


def install(binary: str | None = None, unit_dir: Path | None = None, run: Callable = subprocess.run) -> bool:
    binary = binary or shutil.which("agentihooks")
    if not binary:
        raise registry.HiveError("agentihooks executable not found")
    unit_dir = unit_dir or Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    path = unit_dir / UNIT
    text = unit(binary)
    if not path.exists() or path.read_text() != text:
        path.write_text(text)
        run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True, timeout=30, check=True)
    result = run(["systemctl", "--user", "enable", "--now", UNIT], capture_output=True, text=True, timeout=30)
    return result.returncode == 0


def _reading(five: balancer.QuotaWindow, week: balancer.QuotaWindow, observed_at: float | None) -> dict:
    return {
        "five_hour": {"used": five.used, "resets_at": five.resets_at},
        "seven_day": {"used": week.used, "resets_at": week.resets_at},
        "observed_at": observed_at,
    }


def _claude_snapshots(observed: dict) -> None:
    live = account_sessions.live_sessions()
    for session, info in broadcast._load_sessions().items():
        account = live.get(info.get("pid"))
        if account is None:
            continue
        try:
            data = json.loads(quota_usage._snapshot_path(session).read_text())
            at = float(data["updated_at"])
            windows = data["rate_limits"]
            five = windows.get("five_hour", {})
            week = windows.get("seven_day", {})
            result = balancer.ProbeResult(
                account,
                "allowed",
                "UNKNOWN",
                None,
                balancer.QuotaWindow(five.get("used_percentage"), five.get("resets_at")),
                balancer.QuotaWindow(week.get("used_percentage"), week.get("resets_at")),
            )
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if at > observed.get(account, (0, None))[0]:
            observed[account] = (at, result)


def claude_signed_in() -> bool:
    try:
        result = subprocess.run(["claude", "auth", "status", "--json"], capture_output=True, text=True, timeout=5)
        status = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and status.get("loggedIn") is True and status.get("authMethod") == "claude.ai"


def _subscriptions(environ, counts: dict, now: float) -> tuple[list, dict]:
    observed = {result.account: (at, result) for at, result in balancer.cached_observations(environ=environ)}
    _claude_snapshots(observed)
    claude = {
        name.removeprefix(balancer.TOKEN_PREFIX)
        for name in environ
        if name.startswith(balancer.TOKEN_PREFIX) and environ[name]
    }
    claude.update(counts["claude"])
    claude.discard(account_sessions.API_ACCOUNT)
    if claude_signed_in():
        claude.add(account_sessions.UNROUTED)
    slots, quota = [], {}
    for account in sorted(claude):
        at, result = observed.get(account, (None, None))
        cap = balancer.account_cap(result, now) if result and session_bands.fresh(at, now) else None
        kind = "interactive" if account == account_sessions.UNROUTED else "subscription"
        slots.append({"harness": "claude", "account": account, "kind": kind, "cap": cap or 0})
        quota[f"claude:{account}"] = (
            _reading(result.five_hour, result.seven_day, at)
            if result
            else _reading(balancer.QuotaWindow(), balancer.QuotaWindow(), None)
        )
    pool = [account for account in codex_router.accounts(environ) if account.signed_in]
    readings = codex_router.quotas(pool, environ)
    for account in pool:
        reading = readings.get(account.name)
        cap = codex_router.account_cap(reading, now)
        slots.append(
            {
                "harness": "codex",
                "account": account.name,
                "kind": "subscription" if account.is_token else "interactive",
                "cap": cap or 0,
            }
        )
        quota[f"codex:{account.name}"] = (
            _reading(reading.five_hour, reading.seven_day, reading.observed_at)
            if reading
            else _reading(balancer.QuotaWindow(), balancer.QuotaWindow(), None)
        )
    return slots, quota


def repositories() -> list[str]:
    root = Path(os.environ.get("HIVE_REPO_ROOT") or Path.home() / "dev").expanduser()
    if not root.is_dir():
        return []
    candidates = list(root.iterdir())
    nested = [
        child
        for parent in candidates
        if parent.is_dir() and not (parent / ".git").exists()
        for child in parent.iterdir()
    ]
    return sorted(str(path) for path in candidates + nested if (path / ".git").exists())


def observe() -> dict:
    environ = os.environ
    now = time.time()
    counts = {
        "claude": account_sessions.sessions_by_account(),
        "codex": account_sessions.codex_sessions_by_account(),
    }
    slots, quota = _subscriptions(environ, counts, now)
    for source in (claude_api.ClaudeApiSource(counts["claude"]), codex_api.CodexApiSource(counts["codex"])):
        for slot in source.slots(environ, now):
            slots.append(
                {
                    "harness": slot.harness,
                    "account": slot.account,
                    "kind": slot.kind,
                    "cap": slot.cap,
                    "provider": slot.provider,
                }
            )
    interactive = {
        harness: next(
            (slot["account"] for slot in slots if slot["harness"] == harness and slot["kind"] == "interactive"), ""
        )
        for harness in counts
    }
    sessions = {
        f"{slot['harness']}:{slot['account']}": counts[slot["harness"]].get(slot["account"], 0) for slot in slots
    }
    return {"slots": slots, "interactive": interactive, "repos": repositories(), "sessions": sessions, "quota": quota}
