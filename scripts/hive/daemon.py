import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from hooks.context import account_sessions, broadcast, profile_chain
from scripts import claude_quota_balancer as balancer
from scripts import codex_router, github_app_cli, master_claude_login, session_bands
from scripts.hive import quota_refresh, registry
from scripts.routing import claude_api, codex_api
from scripts.scheduler.process_timer import wait as scheduled_wait
from scripts.swarm.commands import hive_id

if TYPE_CHECKING:
    from redis import Redis

    from scripts.hive.orders import Consumer
    from scripts.swarm.runtime import HerdrRuntime
    from scripts.swarm_v2.runtime.local import LocalHerdrRuntime

BEAT_SECONDS = 15
TTL_SECONDS = BEAT_SECONDS * 3
# A launch inside a beat can run for minutes, so headroom and the heartbeat publish on their own thread well
# inside their stale windows.
HEADROOM_SECONDS = 10
RETRY_SECONDS = (1, 2, 4, 8, 16, 30)
CONNECT_TIMEOUT_S = 3
READ_TIMEOUT_S = 5
# The wait client times out at BEAT_SECONDS, so a scheduled wake must land before it.
WAKE_SLACK_S = 2
UNIT = "agentihooks-hive.service"
ORIGIN = re.compile(r'\[remote "origin"\][^\[]*?\burl\s*=\s*(\S+)')
REMOTE = re.compile(r"(?:[a-z+]+://)?(?:[^@/]+@)?([^/:]+)[:/](.+?)(?:\.git)?/?")


def beat(redis: "Redis", local_id: str) -> None:
    from scripts.hive import reports
    from scripts.hive.orders import Consumer, alive

    alive(redis, local_id)
    consumer = Consumer.local(redis, local_id)
    fenced = fence(redis, local_id, consumer.runtime)
    processes = account_sessions.local_processes()
    executions = _executions(redis, local_id, processes)
    report = observe(processes=processes)
    adopt_caps(redis, report)
    if executions:
        report["sessions"]["_executions"] = executions
    key = registry.key(local_id)
    # The member Redis user cannot write the hive index or launch slots; join, revoke and the controller maintain
    # them. A member misdetected as the controller gets ACL's NoPermissionError on that write; it is never fatal,
    # since the hub already indexes the member at join, refresh and revoke.
    member = local_id == os.environ.get("AGENTIHOOKS_HIVE_ID")
    registry.migrate(redis, local_id)
    if not member:
        _index_hive(redis, local_id)
    with redis.pipeline() as pipe:
        registry.seed(pipe, local_id)
        fields = {name: json.dumps(report[name]) for name in ("slots", "interactive", "repos")}
        fields["master_login"] = json.dumps({"claude": master_claude_login.signed_in()})
        fields["master_login_enabled"] = "no" if master_claude_login.unusable() else "yes"
        beat_ms = int(time.time() * 1000)
        fields["heartbeat_at"] = str(beat_ms)
        pipe.hset(key, mapping=fields)
        for field in ("sessions", "quota"):
            pipe.set(f"{key}:{field}", json.dumps(report[field]), ex=TTL_SECONDS)
        pipe.execute()
    from scripts.hive import headroom, join_report

    headroom.publish(redis, local_id, beat_ms)
    if not member:
        _refresh_launch_slots(redis, executions)
    adopt_capabilities(redis, local_id)
    if not fenced:
        poll_orders(consumer)
    consumer.poll_commands()
    consumer.sync_workspaces(TTL_SECONDS)
    join_report.heartbeat(redis, local_id, int(time.time() * 1000))
    hosted = execution_report(redis, local_id, consumer.runtime.herdr)
    redis.set(reports.key(local_id), json.dumps(hosted), ex=TTL_SECONDS)
    from scripts.hive.execution_events import observe as publish_executions

    publish_executions(local_id, hosted)
    from scripts.hive import account_slots

    try:
        account_slots.publish(local_id, report, hosted)
    except _slot_errors() as exc:
        print(f"hive account slot heartbeat failed ({type(exc).__name__}: {exc}); retrying next beat", file=sys.stderr)
    publish_config(redis, local_id, report["repos"])
    close_dead_tabs(set(hosted["names"]))


def poll_orders(consumer: "Consumer") -> None:
    from scripts.swarm_ledger import authority_context

    member = consumer.hive == os.environ.get("AGENTIHOOKS_HIVE_ID")
    pending = not member and consumer.store.redis.xlen(consumer.orders.stream(consumer.hive))
    secret = authority_context.hosted() if pending else None
    if not secret:
        consumer.poll()
        return
    kept = os.environ.get("SWARM_SECRET")
    try:
        with authority_context.bound(secret):
            consumer.poll()
    finally:
        if kept is not None:
            os.environ["SWARM_SECRET"] = kept


def fill_operator_env() -> None:
    from scripts import operator_env

    if hive_id() != os.environ.get("AGENTIHOOKS_HIVE_ID"):
        operator_env.fill(os.environ)


def _index_hive(redis: "Redis", local_id: str) -> None:
    from redis.exceptions import NoPermissionError

    try:
        redis.sadd(registry.INDEX, local_id)
    except NoPermissionError:
        pass


def _refresh_launch_slots(redis: "Redis", executions: dict) -> None:
    from redis.exceptions import NoPermissionError

    from scripts.routing.reservation import LaunchSlots

    try:
        LaunchSlots(redis).refresh([execution for ids in executions.values() for execution in ids])
    except NoPermissionError:
        pass


def _slot_errors() -> tuple[type[Exception], ...]:
    """Evaluated only once a publish raised, so a hive without the nats extra never imports it."""
    from nats.errors import Error as NatsError

    from scripts.hive import account_slots

    return NatsError, TimeoutError, account_slots.Unavailable


def adopt_capabilities(redis: "Redis", local_id: str) -> None:
    """Launches inherit the daemon environment, so a render sees the capabilities this hive's record declares."""
    os.environ.pop(profile_chain.HIVE_CAPABILITIES, None)
    declared = json.loads(redis.hget(registry.key(local_id), "capabilities") or "[]")
    os.environ[profile_chain.HIVE_CAPABILITIES] = ",".join(declared)


def adopt_caps(redis: "Redis", report: dict) -> None:
    """A subscription slot takes the cap of a newer reading another live hive published, since quota is per account."""
    now = int(time.time() * 1000)
    for hive in registry.hives(redis):
        if not registry.live(hive, now):
            continue
        quotas = json.loads(redis.get(f"{registry.key(hive['id'])}:quota") or "{}")
        caps = {f"{slot['harness']}:{slot['account']}": slot["cap"] for slot in hive["slots"] if "cap" in slot}
        for slot in report["slots"]:
            name = f"{slot['harness']}:{slot['account']}"
            reading = quotas.get(name)
            if slot["kind"] == "subscription" and name in caps and _newer(reading, report["quota"][name]):
                slot["cap"] = caps[name]
                report["quota"][name] = reading


def _newer(reading: dict | None, mine: dict) -> bool:
    if reading is None or reading["observed_at"] is None:
        return False
    return mine["observed_at"] is None or reading["observed_at"] > mine["observed_at"]


def publish_config(redis: "Redis", local_id: str, repos: list[str]) -> None:
    from scripts.hive import config

    config.publish(redis, local_id, repos)


def fence(redis: "Redis", local_id: str, runtime: "LocalHerdrRuntime") -> bool:
    """Terminate the superseded executions the controller fenced on this hive; True while any still runs."""
    from scripts.swarm import master_move
    from scripts.swarm.store import RedisStore, SwarmError

    store, key = RedisStore(redis), master_move.fenced(local_id)
    for execution_id, raw in redis.hgetall(key).items():
        try:
            agent = store.execution(json.loads(raw)["slug"], execution_id)
        except SwarmError:
            redis.hdel(key, execution_id)
            continue
        if runtime.terminate(agent).ok:
            redis.hdel(key, execution_id)
    return redis.hlen(key) > 0


def publish_headroom(redis: "Redis", waiter: "Redis", local_id: str, stop: threading.Event) -> None:
    from redis.exceptions import ConnectionError, RedisError, TimeoutError

    from scripts.hive import headroom, orders

    while not stop.is_set():
        try:
            scheduled_wait(HEADROOM_SECONDS, redis=waiter, timeout=HEADROOM_SECONDS + WAKE_SLACK_S)
        except (ConnectionError, TimeoutError):
            return
        if stop.is_set():
            return
        try:
            now_ms = int(time.time() * 1000)
            headroom.publish(redis, local_id, now_ms)
            registry.touch(redis, local_id, now_ms)
            orders.alive(redis, local_id)
        except (RedisError, ValueError, KeyError) as exc:
            print(f"hive headroom publish failed ({type(exc).__name__}: {exc})", file=sys.stderr)


def publish_herdr_stats(redis: "Redis", waiter: "Redis", local_id: str, stop: threading.Event) -> None:
    """Only the hive that hosts the operator's interface runs the master herdr client, so only it publishes."""
    from redis.exceptions import ConnectionError, RedisError, TimeoutError

    from scripts.hive import herdr_stats

    while not stop.is_set():
        try:
            scheduled_wait(herdr_stats.EVERY_S, redis=waiter, timeout=herdr_stats.EVERY_S + WAKE_SLACK_S)
        except (ConnectionError, TimeoutError):
            return
        if stop.is_set():
            return
        try:
            if (registry.show(redis, local_id) or {}).get("ui") == "yes":
                herdr_stats.publish(redis, local_id, int(time.time() * 1000))
        except (RedisError, ValueError, KeyError) as exc:
            print(f"hive herdr stats publish failed ({type(exc).__name__}: {exc})", file=sys.stderr)


def run(connect: Callable[[], tuple["Redis", "Redis"]]) -> None:
    from redis.exceptions import ConnectionError, TimeoutError

    lost = (ConnectionError, TimeoutError)
    fill_operator_env()
    quota_refresh.refresh(os.environ)
    worker, attempted_at = None, time.time()
    renewed = time.time()
    failures = 0
    while True:
        stop = threading.Event()
        try:
            redis, waiter = connect()
            local_id = hive_id()
            threading.Thread(target=publish_headroom, args=(redis, waiter, local_id, stop), daemon=True).start()
            threading.Thread(target=publish_herdr_stats, args=(redis, waiter, local_id, stop), daemon=True).start()
            while True:
                started = time.monotonic()
                worker, attempted_at = quota_refresh.start(os.environ, worker, attempted_at)
                renewed = github_app_cli.renew(renewed, time.time())
                beat(redis, local_id)
                elapsed = time.monotonic() - started
                delay = max(0, BEAT_SECONDS - max(WAKE_SLACK_S, elapsed))
                scheduled_wait(delay, redis=waiter, timeout=max(delay, BEAT_SECONDS - elapsed - WAKE_SLACK_S / 2))
                failures = 0
        except lost as exc:
            stop.set()
            _back_off(exc, failures)
            failures += 1
        finally:
            stop.set()


def _back_off(exc: Exception, failures: int) -> None:
    delay = RETRY_SECONDS[min(failures, len(RETRY_SECONDS) - 1)]
    print(f"hive Redis connection lost ({type(exc).__name__}: {exc}); reconnecting in {delay}s", file=sys.stderr)
    time.sleep(delay)


def clients(source: "Redis") -> tuple["Redis", "Redis"]:
    from redis import ConnectionPool, Redis
    from redis.backoff import NoBackoff
    from redis.retry import Retry

    pool = source.connection_pool
    options = {
        **pool.connection_kwargs,
        "socket_connect_timeout": CONNECT_TIMEOUT_S,
        "socket_keepalive": True,
        "health_check_interval": BEAT_SECONDS,
        "retry": Retry(NoBackoff(), 0),
    }
    source.close()
    return tuple(
        Redis(
            connection_pool=ConnectionPool(
                connection_class=pool.connection_class, **options | {"socket_timeout": timeout}
            )
        )
        for timeout in (READ_TIMEOUT_S, BEAT_SECONDS)
    )


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
    from hooks.context import quota_usage

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
    claude = {credential.account for credential in balancer.discover_credentials(environ)}
    claude.update(_native_accounts(environ, observed))
    claude.update(counts["claude"])
    claude.discard(account_sessions.API_ACCOUNT)
    if claude_signed_in():
        claude.add(account_sessions.UNROUTED)
    slots, quota = [], {}
    for account in sorted(claude):
        at, result = observed.get(account, (None, None))
        cap = (
            balancer.account_cap(result, now, ceiling=session_bands.hive_ceiling(environ))
            if result and session_bands.fresh(at, now)
            else None
        )
        kind = "interactive" if account == account_sessions.UNROUTED else "subscription"
        slots.append({"harness": "claude", "account": account, "kind": kind, "cap": cap or 0})
        quota[f"claude:{account}"] = (
            _reading(result.five_hour, result.seven_day, at)
            if result
            else _reading(balancer.QuotaWindow(), balancer.QuotaWindow(), None)
        )
    pool = codex_router.routed(
        [
            account
            for account in codex_router.accounts(environ)
            if account.signed_in and not codex_router.refusal(account, environ)
        ],
        environ,
    )
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
            if reading and not reading.expired
            else _reading(balancer.QuotaWindow(), balancer.QuotaWindow(), None)
        )
    return slots, quota


def _native_accounts(environ, observed: dict) -> set[str]:
    from hooks.context import quota_usage
    from scripts.routing import master_account

    accounts = {account for account in master_claude_login.logins().values() if account}
    declared = master_account.load(environ).get("claude")
    if declared:
        accounts.add(declared.slug)
    for account in accounts:
        snapshot = quota_usage.latest_snapshot(account)
        if not snapshot:
            continue
        at, windows = snapshot
        if at <= observed.get(account, (0, None))[0]:
            continue
        five, week = windows.get("five_hour") or {}, windows.get("seven_day") or {}
        observed[account] = (
            at,
            balancer.ProbeResult(
                account,
                "allowed",
                "UNKNOWN",
                None,
                balancer.QuotaWindow(five.get("used_percentage"), five.get("resets_at")),
                balancer.QuotaWindow(week.get("used_percentage"), week.get("resets_at")),
            ),
        )
    return accounts


def repositories() -> list[str]:
    checkouts = _checkouts()
    identities = {origin(path) for path in checkouts} - {""}
    return sorted(identities | {str(path) for path in checkouts})


def local_checkout(repo: str) -> str:
    matches = sorted(str(path) for path in _checkouts() if origin(path) == repo)
    return matches[0] if matches else repo


def _checkouts() -> list[Path]:
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
    return [path for path in candidates + nested if (path / ".git").exists()]


def origin(checkout: Path) -> str:
    """The checkout's origin remote as a lowercase host/owner/name, the name a hosted swarm gives its repo."""
    try:
        found = ORIGIN.search((_common_gitdir(checkout) / "config").read_text())
    except OSError:
        return ""
    remote = found and REMOTE.fullmatch(found.group(1).strip())
    return f"{remote.group(1)}/{remote.group(2)}".lower() if remote else ""


def _common_gitdir(checkout: Path) -> Path:
    git = checkout / ".git"
    if git.is_file():
        git = checkout / git.read_text().removeprefix("gitdir:").strip()
        if (git / "commondir").is_file():
            git = git / (git / "commondir").read_text().strip()
    return git


def observe(processes: dict[str, dict[int, str]] | None = None) -> dict:
    from collections import Counter

    environ = os.environ
    now = time.time()
    if processes is None:
        counts = {
            "claude": account_sessions.sessions_by_account(local=True),
            "codex": account_sessions.codex_sessions_by_account(local=True),
        }
    else:
        counts = {harness: dict(Counter(found.values())) for harness, found in processes.items()}
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
    from scripts.routing import reservation

    for slot in slots:
        count = reservation.interactive_sessions(slot["harness"], slot["account"])
        if count:
            sessions[f"interactive:{slot['harness']}:{slot['account']}"] = count
    return {"slots": slots, "interactive": interactive, "repos": repositories(), "sessions": sessions, "quota": quota}


def _executions(redis: "Redis", local_id: str, processes: dict[str, dict[int, str]]) -> dict[str, list[str]]:
    from scripts.swarm import live_binding
    from scripts.swarm.store import RedisStore
    from scripts.swarm_v2.registry import CLOSED, decode
    from scripts.terminate_agent import sessions

    store = RedisStore(redis)
    local = sessions()
    found: dict[str, list[str]] = {}
    counted: set[tuple[str, int]] = set()
    for swarm in store.slugs():
        agents = {agent.execution_id: agent for agent in store.agents(swarm)}
        for raw in redis.hvals(store.key(swarm, "fleet-sessions")):
            record = decode(raw)
            agent = agents.get(record.execution_id)
            if record.state == CLOSED or agent is None or (agent.hive or record.scope.host) != local_id:
                continue
            identity = record.harness, record.pid
            if identity in counted or record.generation != agent.generation:
                continue
            if processes.get(record.harness, {}).get(record.pid) != agent.account:
                continue
            counted.add(identity)
            found.setdefault(f"{record.harness}:{agent.account}", []).append(record.execution_id)
        for agent in agents.values():
            if not agent.execution_id or (agent.hive or agent.runtime_target.get("host_id")) != local_id:
                continue
            session = live_binding.bound_session(agent, local)
            if session is None or session.target != agent.harness:
                continue
            identity = session.target, session.process.pid
            if identity in counted or processes.get(session.target, {}).get(session.process.pid) != agent.account:
                continue
            counted.add(identity)
            found.setdefault(f"{agent.harness}:{agent.account}", []).append(agent.execution_id)
    return found


def _pane_ids(herdr: "HerdrRuntime") -> list[str] | None:
    try:
        return [pane["pane_id"] for pane in herdr.herdr(["pane", "list"])["panes"]]
    except (RuntimeError, OSError, subprocess.SubprocessError, KeyError, TypeError):
        return None


def execution_report(redis: "Redis", local_id: str, herdr: "HerdrRuntime") -> dict:
    from dataclasses import asdict

    from scripts.swarm.store import RedisStore

    store = RedisStore(redis)
    hosted = [
        (slug, agent)
        for slug in store.slugs()
        for agent in store.agents(slug)
        if agent.execution_id and agent.runtime_target.get("host_id") == local_id
    ]
    report = {"names": sorted(herdr.live_names()), "panes": {}, "executions": {}}
    if not hosted:
        return report
    from scripts.swarm_v2.runtime.process import local_namespace

    report["observed_at"] = int(time.time() * 1000)
    report["process_namespace"] = local_namespace()
    report["pane_ids"] = _pane_ids(herdr)
    facts = herdr.bindings([agent for _, agent in hosted])
    report["panes"] = herdr.conversations() or {}
    for slug, agent in hosted:
        report["executions"][agent.execution_id] = {
            "slug": slug,
            "name": agent.name,
            "generation": agent.generation,
            "reported": herdr.reported(agent),
            "binding": facts.get(agent.name),
            "pane": asdict(herdr.observe(agent)),
        }
    return report


def close_dead_tabs(live: set[str]) -> list[str]:
    from scripts import herdr_gc, herdr_host

    environ = dict(os.environ)
    if not herdr_host.binary() or not herdr_host.server_running(environ):
        return []
    try:
        return herdr_gc.close_dead_tabs(environ, int(time.time() * 1000), herdr_gc.Herdr(environ), live)
    except herdr_gc.READ_ERRORS:
        return []
