"""`python -m scripts.codex_session serve`: route the account, render the profile, then supervise one app-server."""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path

from scripts import codex_router, profile_telemetry, select_profile
from scripts.codex_session import supervisor
from scripts.codex_session.binding import BindingError
from scripts.routing import codex_api, envs
from scripts.swarm.store import AgentRecord, SwarmError, connect

IDENTITY_ENV = ("AGENTIHOOKS_SWARM", "AGENTIHOOKS_SWARM_LANE", "AGENTIHOOKS_SWARM_TASK", "AGENTIHOOKS_AGENT_NAME")
KEPT_FLAGS = ("-c", "--enable", "--disable")
INTERACTIVE = "interactive"
ROUTE_FLAG = "--route"
ROUTE_PREFIX = f"{ROUTE_FLAG}="
EFFORT_PREFIX = "model_reasoning_effort="
QUOTE = '"'


def take_route(args: list[str]) -> tuple[str, list[str]]:
    route, rest = "", []
    remaining = iter(args)
    for argument in remaining:
        if argument == ROUTE_FLAG and (value := next(remaining, None)) is not None:
            route = value
        elif argument.startswith(ROUTE_PREFIX):
            route = argument.removeprefix(ROUTE_PREFIX)
        else:
            rest.append(argument)
    return route, rest


def server_options(flags: list[str]) -> tuple[str, str, tuple]:
    model, effort, kept = "", "", []
    arguments = iter(flags)
    for argument in arguments:
        value = next(arguments, None)
        if value is None:
            raise ValueError(f"{argument} requires a value")
        if argument in ("-m", "--model"):
            model = value
        elif argument not in KEPT_FLAGS:
            raise ValueError(f"app-server takes only -m, -c, --enable and --disable; got {argument}")
        else:
            effort = value.removeprefix(EFFORT_PREFIX).strip(QUOTE) if value.startswith(EFFORT_PREFIX) else effort
            kept += [argument, value]
    return model, effort, (*(("-c", f"model={json.dumps(model)}") if model else ()), *kept)


def provider_args(account: codex_router.CodexAccount) -> tuple:
    if account.is_api:
        return tuple(codex_api.overrides(account.key_env, account.base_url))
    if account.bearer:
        return tuple(codex_router.bearer_overrides())
    return ()


def account_type(account: codex_router.CodexAccount, route: str) -> str:
    if account.is_api:
        return "apiKey"
    return "chatgpt" if route == INTERACTIVE or not (account.is_token or account.bearer) else ""


def _opening(prompt_file: str) -> str:
    if not prompt_file:
        return ""
    return Path(prompt_file).read_bytes().decode().replace("\r\n", "\n").replace("\r", "\n")


def route_account(environ: Mapping[str, str], route: str, exclude: str) -> tuple[codex_router.CodexAccount, str, dict]:
    if route == INTERACTIVE:
        from scripts.routing import interactive

        account = codex_router.CodexAccount(interactive.account("codex", environ, subprocess.run).slug)
        return account, "forced", envs.codex_interactive_child(environ, account.name)
    account, placement, _, _ = codex_router._route(environ, route, subprocess.run, exclude)
    account = codex_router.credential(account, environ)
    return account, placement, codex_router.child_environment(account, environ)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.codex_session")
    parser.add_argument("command", choices=("serve",))
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--seat", required=True)
    parser.add_argument("--overlay", action="append", default=[])
    parser.add_argument("--bundle-revision", default="")
    parser.add_argument("--exclude", default="")
    parser.add_argument("--report", default="")
    parser.add_argument("--previous-execution", default="")
    parser.add_argument("--resume", default="")
    parser.add_argument("--prompt-file", default="")
    parser.add_argument("--startup-timeout", type=float, default=supervisor.STARTUP_TIMEOUT_S)
    return parser


def prepare(args: argparse.Namespace, agent_args: list[str], environ: dict[str, str]) -> supervisor.Launch:
    missing = [name for name in IDENTITY_ENV if not environ.get(name)]
    if missing:
        raise ValueError(f"the app-server transport runs only inside a swarm execution; missing {missing}")
    slug, lane, task, name = (environ[name] for name in IDENTITY_ENV)
    route, agent_args = take_route(agent_args)
    environ = {**environ, select_profile.BUNDLE_REVISION: args.bundle_revision}
    profile_env, flags = select_profile.prepare(args.profile, "codex", "", "", agent_args, environ, args.overlay)
    model, effort, server_args = server_options(flags)
    environ.update(profile_env)
    account, placement, child = route_account(environ, route, args.exclude)
    child.update(profile_telemetry.agentihooks_langfuse_keys())
    codex_router._report(args.report, status="routed", account=account.name, placement=placement)
    agent = AgentRecord(
        name,
        lane,
        task,
        seat=args.seat,
        account=account.name,
        started_at=int(time.time() * 1000),
        model=model,
        effort=effort,
        profile=args.profile,
        overlays=list(args.overlay),
    )
    return supervisor.Launch(
        slug=slug,
        agent=agent,
        cwd=args.cwd,
        codex_home=profile_env["CODEX_HOME"],
        environ=child,
        server_args=(*provider_args(account), *server_args),
        previous_execution_id=args.previous_execution,
        resume_thread=args.resume,
        opening=_opening(args.prompt_file),
        account_type=account_type(account, route),
        allowed_tokens=frozenset(name for name in (account.env_name, account.key_env) if name),
        bundle_revision=args.bundle_revision,
        startup_timeout_s=args.startup_timeout,
    )


def main(
    argv: list[str] | None = None, environ: dict[str, str] | None = None, deps: supervisor.Deps | None = None
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    split = arguments.index("--") if "--" in arguments else len(arguments)
    args = _parser().parse_args(arguments[:split])
    active = dict(os.environ if environ is None else environ)
    try:
        spec = prepare(args, arguments[split + 1 :], active)
    except (OSError, ValueError, codex_router.RoutingError) as exc:
        print(f"agentihooks codex-session: {exc}", file=sys.stderr)
        codex_router._report(args.report, status="failed", error=str(exc))
        return 2
    store = connect(active)
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, _exit_on_signal)
    try:
        started = supervisor.launch(store, spec, deps or supervisor.Deps())
    except (supervisor.LaunchFailure, SwarmError, BindingError) as exc:
        print(f"agentihooks codex-session: launch refused: {exc}", file=sys.stderr)
        return 3
    record = started.record
    print(
        f"[agentihooks codex-session] execution={record.execution_id} thread={record.thread_id} "
        f"endpoint={record.endpoint_ref} state={record.state}",
        flush=True,
    )
    pump = None
    try:
        pump = subprocess.Popen(
            # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args
            [sys.executable, "-m", "scripts.inbox.codex", spec.slug, record.execution_id],
            cwd=spec.cwd,
            env=spec.environ,
        )
        while pump.poll() is None:
            try:
                started.process.wait(timeout=supervisor.POLL_S)
                break
            except subprocess.TimeoutExpired:
                pass
        else:
            return pump.returncode or 3
    finally:
        if pump is not None:
            supervisor.stop(pump)
        supervisor.stop(started.process)
        supervisor.finish(store, spec.slug, started, int(time.time() * 1000))
    return started.process.returncode or 0


def _exit_on_signal(signum: int, frame: object) -> None:
    raise SystemExit(128 + signum)
