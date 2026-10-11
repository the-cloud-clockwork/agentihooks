import argparse
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from scripts import agent_choice, claude_trust, herdr_host, herdr_panes, operator_env
from scripts.swarm import effort_range
from scripts.targets.codex_target import codex_home, restore_hook_order


def _is_wsl(environ: dict[str, str]) -> bool:
    if environ.get("WSL_DISTRO_NAME") or environ.get("WSL_INTEROP"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except OSError:
        return False


def _resolve_directory(requested: str, environ: dict[str, str]) -> Path:
    home = Path(environ.get("HOME", str(Path.home()))).expanduser()
    base = Path(environ.get("RUN_CLAUDE_BASE_DIR", "")).expanduser() if environ.get("RUN_CLAUDE_BASE_DIR") else None
    if base is None:
        base = home / "dev" if (home / "dev").is_dir() else home
    if not requested:
        target = base
    elif requested == "~":
        target = home
    elif requested.startswith("~/"):
        target = home / requested[2:]
    else:
        candidate = Path(requested).expanduser()
        target = candidate if candidate.is_absolute() else base / candidate
    target = target.resolve()
    if not target.is_dir():
        raise ValueError(f"directory does not exist: {target}")
    return target


def _runtime_dir(environ: dict[str, str]) -> Path:
    root = herdr_panes.run_folder(environ)
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    return root


def _started_marker(launcher: Path) -> Path:
    return launcher.with_suffix(".started")


def _route_report(launcher: Path) -> Path:
    return launcher.with_suffix(".route")


def _read_route_report(path: Path) -> dict[str, str]:
    fields = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key:
            fields[key] = value
    return fields


def _take_route_report(path: Path) -> tuple[dict[str, str], int | None]:
    try:
        written = int(path.stat().st_mtime * 1000)
        return _read_route_report(path), written
    except FileNotFoundError:
        return {}, None
    finally:
        path.unlink(missing_ok=True)


@dataclass(frozen=True)
class AgentSpec:
    agent: str = "claude"
    exclude: str = ""
    fallback_bare: bool = True
    resume: str = ""
    profile: str = ""
    channel: bool = False
    overlays: tuple = ()
    bundle_revision: str = ""


SPAWN = "AGENTIHOOKS_SWARM_SPAWN"
PREDECESSOR = "AGENTIHOOKS_PREDECESSOR_SESSION"
SWARM_IDENTITY = (
    "AGENTIHOOKS_SWARM",
    "AGENTIHOOKS_SWARM_LANE",
    "AGENTIHOOKS_SWARM_TASK",
    "AGENTIHOOKS_SWARM_AUTONOMY",
    "AGENTIHOOKS_SWARM_LAUNCHER",
    effort_range.VARIABLE,
)


def _collector(environ: dict[str, str]) -> str:
    return environ.get("AGENTIHOOKS_OTEL_COLLECTOR", "").rstrip("/")


def _telemetry_exports(name: str, environ: dict[str, str]) -> str:
    collector = _collector(environ)
    if not collector:
        return ""
    attributes = [
        ("swarm", environ.get("AGENTIHOOKS_SWARM", "")),
        ("agent", name),
        ("lane", environ.get("AGENTIHOOKS_SWARM_LANE", "")),
        ("task", environ.get("AGENTIHOOKS_SWARM_TASK", "")),
    ]
    resource = ",".join(f"{key}={value}" for key, value in attributes if value)
    exports = {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_ENDPOINT": collector,
        "OTEL_RESOURCE_ATTRIBUTES": resource,
    }
    return "".join(f"export {key}={shlex.quote(value)}\n" for key, value in exports.items())


def _swarm_exports(environ: dict[str, str]) -> str:
    if not environ.get("AGENTIHOOKS_SWARM"):
        return ""
    names = ("AGENTIHOOKS_SWARM", "AGENTIHOOKS_SWARM_LANE", "AGENTIHOOKS_SWARM_TASK", "AGENTIHOOKS_COMPACT_LIMIT")
    exports = {name: environ[name] for name in names if environ.get(name)}
    exports["AGENTIHOOKS_LANGFUSE_ENABLED"] = "1"
    pin = "export AGENTIHOOKS_SWARM_LAUNCHER=$$\n"
    return "".join(f"export {key}={shlex.quote(value)}\n" for key, value in exports.items()) + pin


def _launch_environ(environ: dict[str, str], name: str, handoff: bool) -> dict[str, str]:
    keeps = environ.get(SPAWN) == "1" or handoff or name == environ.get("AGENTIHOOKS_AGENT_NAME")
    dropped = {SPAWN} if keeps else {SPAWN, *SWARM_IDENTITY}
    return {key: value for key, value in environ.items() if key not in dropped}


def _with_predecessor(environ: dict[str, str], handoff: bool) -> dict[str, str]:
    """Only a transfer names a predecessor: a quota handoff names this session, a tick spawn keeps what the tick named."""
    rest = {key: value for key, value in environ.items() if key != PREDECESSOR}
    if handoff:
        session = environ.get("CLAUDE_CODE_SESSION_ID")
    elif environ.get(SPAWN) == "1":
        session = environ.get(PREDECESSOR)
    else:
        return rest
    return {**rest, PREDECESSOR: session} if session else rest


def _predecessor_export(environ: dict[str, str]) -> str:
    session = environ.get(PREDECESSOR)
    return f"export {PREDECESSOR}={shlex.quote(session)}\n" if session else f"unset {PREDECESSOR}\n"


def _config_home_export(environ: dict[str, str]) -> str:
    # A herdr pane inherits the herdr server's environment, not the caller's, so trust lands where Claude reads only if pinned.
    home = environ.get("CLAUDE_CONFIG_DIR")
    return f"export CLAUDE_CONFIG_DIR={shlex.quote(home)}\n" if home else "unset CLAUDE_CONFIG_DIR\n"


def _codex_otel_args(environ: dict[str, str]) -> list[str]:
    collector = _collector(environ)
    if not collector:
        return []
    return ["-c", f'otel.exporter={{otlp-http={{endpoint="{collector}/v1/logs",protocol="binary"}}}}']


def _codex_trust_args(directory: Path, environ: dict[str, str]) -> list[str]:
    if not claude_trust.allowed(environ):
        return []
    # An inline table, because Codex splits a dotted -c key on every dot, path dots included.
    return ["-c", f'projects={{{json.dumps(str(directory))}={{trust_level="trusted"}}}}']


MODEL_DEFAULTS = {"claude": "opus", "codex": "gpt-6.1-sol"}
EFFORT_DEFAULT = "high"
AGENT_HELP = "Agent to open; default the harness of the account with a free session and the fewest live sessions"
LAUNCH_GRACE_S = 3


def _flag_value(args: list[str], names: tuple[str, ...]) -> str:
    for i, arg in enumerate(args):
        if arg in names and i + 1 < len(args):
            return args[i + 1]
        for name in names:
            if name.startswith("--") and arg.startswith(f"{name}="):
                return arg.split("=", 1)[1]
    return ""


def _handoff_agent(requested: str, environ: dict[str, str], agent_args: list[str]) -> str:
    source = environ.get("AGENTIHOOKS_TARGET") or "claude"
    target = requested or source
    if _flag_value(agent_args, ("--route",)) == "api":
        return target
    if "codex" in (requested, source) and target != source:
        raise ValueError(
            f"unsupported quota transfer: {source.capitalize()} cannot transfer to a {target.capitalize()} account"
        )
    return "codex" if source == "codex" else "claude"


def _handoff_exclude(agent: str, environ: dict[str, str]) -> str:
    """The account a quota handoff leaves; for Codex it comes from the live process because the
    operator environment can carry every AH_CX_TOKEN_*, which hides the session's own account."""
    from hooks.context.account_sessions import UNROUTED, environment_account
    from scripts.profiles import binding

    if agent == "codex":
        _, _, _, current = binding.process()
        return current
    current = environment_account(environ)
    return "" if current == UNROUTED else current


def model_effort(agent: str, agent_args: list[str], environ: dict[str, str]) -> tuple[str, str]:
    prefix = f"AGENTIHOOKS_{agent.upper()}"
    model = _flag_value(agent_args, ("--model", "-m")) or environ.get(f"{prefix}_MODEL") or MODEL_DEFAULTS[agent]
    effort = _flag_value(agent_args, ("--effort",))
    if agent == "codex":
        found = next((a for a in agent_args if a.startswith("model_reasoning_effort=")), "")
        effort = found.split("=", 1)[1].strip('"') if found else ""
    return model, effort or environ.get(f"{prefix}_EFFORT") or EFFORT_DEFAULT


def model_flags(agent: str, model: str = "", effort: str = "") -> list[str]:
    if agent == "codex":
        return (["-m", model] if model else []) + (["-c", f'model_reasoning_effort="{effort}"'] if effort else [])
    return (["--model", model] if model else []) + (["--effort", effort] if effort else [])


def _model_args(agent: str, agent_args: list[str], environ: dict[str, str]) -> list[str]:
    model, effort = model_effort(agent, agent_args, environ)
    has_model = any(a in ("--model", "-m") or a.startswith("--model=") for a in agent_args)
    if agent == "codex":
        has_effort = any("model_reasoning_effort" in a for a in agent_args)
    else:
        has_effort = any(a == "--effort" or a.startswith("--effort=") for a in agent_args)
    return model_flags(agent, "" if has_model else model, "" if has_effort else effort)


def _profile_command(command: list[str], spec: AgentSpec) -> list[str]:
    if not spec.profile:
        return command
    worn = [f"--overlay={overlay}" for overlay in spec.overlays]
    if spec.bundle_revision:
        worn.append(f"--bundle-revision={spec.bundle_revision}")
    return [command[0], "select-profile", spec.profile, *worn, "--agent", spec.agent, "--", *command[2:]]


def _agent_command(
    spec: AgentSpec, report: Path, name: str, agent_args: list[str], environ: dict[str, str], directory: Path
) -> tuple[list[str], str]:
    """(command, line run before it): Claude routes through `agentihooks claude`, Codex through `agentihooks codex`."""
    agentihooks_bin = shutil.which("agentihooks") or str(Path(sys.argv[0]).resolve())
    if spec.agent == "codex":
        command = [
            agentihooks_bin,
            "codex",
            *(["--agentihooks-exclude", spec.exclude] if spec.exclude else []),
            "--agentihooks-report",
            str(report),
            *(["resume", spec.resume] if spec.resume else []),
            *_codex_otel_args(environ),
            *_codex_trust_args(directory, environ),
            *_model_args("codex", agent_args, environ),
            *agent_args,
        ]
        return _profile_command(command, spec), ""
    command = [
        agentihooks_bin,
        "claude",
        *(["--agentihooks-exclude", spec.exclude] if spec.exclude else []),
        *(["--agentihooks-fallback-bare"] if spec.fallback_bare else []),
        "--agentihooks-report",
        str(report),
        "--name",
        name,
        *(["--resume", spec.resume] if spec.resume else []),
        *_model_args("claude", agent_args, environ),
        *agent_args,
    ]
    # A server on MCP revision 2026-07-28 never registers as a channel.
    before = "export MCP_PROTOCOL_NEGOTIATION=legacy\n" if spec.channel else ""
    return _profile_command(command, spec), before


def _write_launcher(
    directory: Path,
    name: str,
    prompt: str,
    claude_args: list[str],
    environ: dict[str, str],
    spec: AgentSpec = AgentSpec(),
) -> tuple[Path, Path | None]:
    root = _runtime_dir(environ)
    safe_name = "".join(character if character.isalnum() or character in "._-" else "_" for character in name)
    stamp = f"{time.strftime('%y%m%d-%H%M%S')}-{os.getpid()}"
    launcher = root / f"{safe_name}-{stamp}.sh"
    prompt_file = root / f"{safe_name}-{stamp}.prompt" if prompt else None
    if prompt_file is not None:
        prompt_file.write_text(prompt, encoding="utf-8")
        prompt_file.chmod(0o600)

    from scripts.profile_telemetry import installed_langfuse_env

    environ = {**installed_langfuse_env(spec.agent), **environ}
    langfuse = environ.get("AGENTIHOOKS_LANGFUSE_ENABLED")
    langfuse_export = f"export AGENTIHOOKS_LANGFUSE_ENABLED={shlex.quote(langfuse)}\n" if langfuse is not None else ""
    command, before = _agent_command(spec, _route_report(launcher), name, claude_args, environ, directory)
    if prompt_file is not None:
        command_text = f'{shlex.join(command)} "$(cat {shlex.quote(str(prompt_file))})"'
    else:
        command_text = shlex.join(command)
    cleanup = [
        str(launcher),
        str(_route_report(launcher)),
        *([str(prompt_file)] if prompt_file is not None else []),
    ]
    shell = environ.get("SHELL") or "/bin/bash"
    launcher.write_text(
        "#!/usr/bin/env bash\n"
        "set -u\n"
        f"cd {shlex.quote(str(directory))} || exit 1\n"
        f": > {shlex.quote(str(_started_marker(launcher)))}\n"
        f"{operator_env.source_line(environ)}"
        f"{_predecessor_export(environ)}"
        "export AGENTIHOOKS_TERMINAL_LAUNCH=1\n"
        f"export AGENTIHOOKS_AGENT_NAME={shlex.quote(name)}\n"
        f"{_config_home_export(environ) if spec.agent == 'claude' else ''}"
        f"{_telemetry_exports(name, environ)}"
        f"{_swarm_exports(environ)}"
        f"{langfuse_export}"
        f"{_binding_export(environ)}"
        f"sleep {LAUNCH_GRACE_S}\n"
        f"{before}{command_text}\n"
        f"rm -f {shlex.join(cleanup)}\n"
        f"[ -e {shlex.quote(str(root))}/closing-$$ ] && {{ rm -f {shlex.quote(str(root))}/closing-$$; exit 0; }}\n"
        "unset AGENTIHOOKS_AGENT_NAME\n"
        f"exec {shlex.quote(shell)} -l\n",
        encoding="utf-8",
    )
    launcher.chmod(0o700)
    return launcher, prompt_file


def _prepare_profile(args: argparse.Namespace, agent: str, flags: list[str], environ: dict[str, str]) -> list[str]:
    from scripts.profiles import binding
    from scripts.select_profile import BUNDLE_REVISION, OVERLAYS, prepare

    continuing = args.handoff or args.resume or "--resume" in flags or (agent == "codex" and flags[:1] == ["resume"])
    if not args.profile and continuing:
        args.profile = environ.get("AGENTIHOOKS_PROFILE")
        args.overlay = [o for o in environ.get(OVERLAYS, "").split(",") if o]
        args.bundle_revision = environ.get(BUNDLE_REVISION, "")
    if continuing and not args.profile:
        raise ValueError("unsupported continuation: original required profile is missing; pass --profile")
    if not args.profile:
        return flags
    if args.handoff:
        flags = binding.continuation(flags, agent, environ)
    environ[BUNDLE_REVISION] = args.bundle_revision
    profile_env, flags = prepare(args.profile, agent, "", "", flags, environ, args.overlay)
    environ.update(profile_env)
    return flags


def _binding_request(args, agent: str, prompt: str, environ: dict[str, str], flags: list[str]) -> str:
    from scripts.profiles import binding

    if not args.profile:
        return prompt
    home = Path(environ[binding.HOMES[agent]])
    report = _runtime_dir(environ) / f"profile-{os.getpid()}-{time.time_ns()}.json"
    binding.request(report, args.profile, agent, home)
    environ[binding.REPORT] = str(report)
    environ["AGENTIHOOKS_RUN_MODEL"], environ["AGENTIHOOKS_RUN_EFFORT"] = model_effort(agent, flags, environ)
    return f"{binding.PROMPT}\n\n{prompt}"


def _binding_result(environ: dict[str, str], timeout: float, route: dict) -> list[str]:
    from scripts.profiles import binding

    if not environ.get(binding.REPORT):
        return []
    result = binding.wait(Path(environ[binding.REPORT]), timeout)
    if route.get("account") and result["account"] != route["account"]:
        raise ValueError("live process account differs from requested route")
    return [
        "profile_validation=validated",
        f"profile_binding={json.dumps(result, separators=(',', ':'))}",
        *(f"{key}={result[key]}" for key in ("model", "effort") if result.get(key)),
    ]


def _selection_refused(environ: dict[str, str]) -> bool:
    from scripts.profiles import binding

    return bool(environ.get(binding.REPORT)) and binding.refused(Path(environ[binding.REPORT]))


def _binding_export(environ: dict[str, str]) -> str:
    names = (
        "AGENTIHOOKS_PROFILE_REPORT",
        "AGENTIHOOKS_HOME",
        "AGENTIHOOKS_PROFILE",
        "CODEX_HOME",
        "AGENTIHOOKS_RUN_MODEL",
        "AGENTIHOOKS_RUN_EFFORT",
        effort_range.VARIABLE,
    )
    exported = "".join(f"export {key}={shlex.quote(environ[key])}\n" for key in names if environ.get(key))
    worn = environ.get("AGENTIHOOKS_OVERLAYS", "") if environ.get("AGENTIHOOKS_PROFILE") else None
    return exported if worn is None else f"{exported}export AGENTIHOOKS_OVERLAYS={shlex.quote(worn)}\n"


def _linux_command(launcher: Path, directory: Path, title: str) -> list[str] | None:
    candidates = [
        ("xdg-terminal-exec", [f"--title={title}", f"--dir={directory}", "--", str(launcher)]),
        ("x-terminal-emulator", ["-T", title, "-e", str(launcher)]),
        ("gnome-terminal", ["--title", title, "--", str(launcher)]),
        ("konsole", ["--new-tab", "-p", f"tabtitle={title}", "-e", str(launcher)]),
        ("xfce4-terminal", [f"--title={title}", f"--command={launcher}"]),
        ("kitty", ["--title", title, "--directory", str(directory), str(launcher)]),
        ("wezterm", ["start", "--cwd", str(directory), "--", str(launcher)]),
        ("alacritty", ["--title", title, "--working-directory", str(directory), "-e", str(launcher)]),
    ]
    for executable, arguments in candidates:
        resolved = shutil.which(executable)
        if resolved:
            return [resolved, *arguments]
    return None


def _launch_command(
    launcher: Path,
    directory: Path,
    title: str,
    environ: dict[str, str],
) -> tuple[str, list[str]]:
    system = platform.system()
    if system == "Darwin":
        osascript = shutil.which("osascript")
        if not osascript:
            raise RuntimeError("osascript is unavailable on macOS")
        escaped = str(launcher).replace("\\", "\\\\").replace('"', '\\"')
        return "macos", [osascript, "-e", f'tell application "Terminal" to do script "{escaped}"']
    if system == "Linux" and _is_wsl(environ):
        wt = shutil.which("wt.exe")
        if not wt or not shutil.which("wsl.exe"):
            raise RuntimeError("WSL interop requires wt.exe and wsl.exe on PATH")
        if ";" in str(launcher):
            raise RuntimeError(f"launcher path contains ';', which wt.exe splits on: {launcher}")
        distro = environ.get("WSL_DISTRO_NAME", "")
        # wt.exe is a Windows process: the program must be a Windows-resolvable name, never a /mnt/c path.
        return "wsl", [
            wt,
            "-w",
            "0",
            "new-tab",
            "--title",
            title.replace(";", "_"),
            "wsl.exe",
            *(["-d", distro] if distro else []),
            "--",
            "bash",
            # Interactive, so ~/.bashrc exports the AH_CC_TOKEN_* accounts that --route resolves.
            "-lic",
            str(launcher),
        ]
    if system == "Linux":
        if not (environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY")):
            raise RuntimeError("native Linux has no DISPLAY or WAYLAND_DISPLAY")
        command = _linux_command(launcher, directory, title)
        if command is None:
            raise RuntimeError(
                "no supported Linux terminal found: xdg-terminal-exec, x-terminal-emulator, "
                "gnome-terminal, konsole, xfce4-terminal, kitty, wezterm, alacritty"
            )
        return "linux", command
    raise RuntimeError(f"unsupported host OS: {system}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Open a routed Claude session in a new terminal")
    parser.add_argument("--dir", default="", help="Target directory; relative paths resolve below the configured base")
    parser.add_argument("--name", default="", help="Claude session and terminal name")
    prompt = parser.add_mutually_exclusive_group()
    prompt.add_argument("--prompt", default="", help="Opening Claude prompt")
    prompt.add_argument("--prompt-file", default="", help="Read the opening prompt from this file")
    parser.add_argument("--dry-run", action="store_true", help="Print the launch without opening a terminal")
    parser.add_argument(
        "--start-timeout",
        type=float,
        default=30.0,
        help="Seconds to wait for the new terminal to start the launcher before failing",
    )
    parser.add_argument(
        "--route-timeout",
        type=float,
        default=150.0,
        help="Seconds to wait for the new session to report which account it was routed to",
    )
    parser.add_argument(
        "--handoff",
        action="store_true",
        help="Quota handoff: route to an account other than this session's, never fall back to bare "
        "Claude, and mark this session handed off once the new one is routed",
    )
    parser.add_argument(
        "--host",
        choices=("herdr", "native"),
        default="",
        help="Terminal host; default $AGENTIHOOKS_TERMINAL_HOST, else herdr when installed and enabled, else native",
    )
    parser.add_argument(
        "--placement",
        choices=herdr_host.PLACEMENTS,
        default="tab",
        help="herdr: a tab in the workspace (default), a split of the calling pane, or a new workspace",
    )
    parser.add_argument("--workspace", default="", help="herdr: workspace label to place the session in (crew)")
    parser.add_argument(
        "--agent",
        choices=agent_choice.AGENTS,
        default="",
        help=AGENT_HELP,
    )
    parser.add_argument("--profile", default="", help="Role profile for this run")
    parser.add_argument(
        "--overlay", action="append", default=[], help="Overlay the profile wears; repeat for up to three"
    )
    parser.add_argument("--bundle-revision", default="", help="Render the profile only from this bundle commit")
    parser.add_argument("--resume", default="", help="Reopen this conversation id (Claude --resume, Codex resume)")
    parser.add_argument(
        "--inbox-channel",
        action="store_true",
        help="Claude: load the agentihooks inbox channel and answer its development channels warning in herdr",
    )
    parser.add_argument("claude_args", nargs=argparse.REMAINDER, help="Arguments after -- pass through to Claude")
    return parser


def _herdr_enabled(environ: dict[str, str]) -> bool:
    home = Path(environ.get("AGENTIHOOKS_HOME") or Path(environ.get("HOME", str(Path.home()))) / ".agentihooks")
    try:
        state = json.loads((home / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    herdr = state.get("herdr") if isinstance(state, dict) else None
    return not (isinstance(herdr, dict) and herdr.get("enabled") is False)


def _select_host(requested: str, environ: dict[str, str]) -> tuple[str, bool]:
    """(host, explicit): the flag, then $AGENTIHOOKS_TERMINAL_HOST, then herdr when installed and enabled."""
    choice = requested or environ.get("AGENTIHOOKS_TERMINAL_HOST", "")
    if choice:
        return choice, True
    return ("herdr" if herdr_host.binary() and _herdr_enabled(environ) else "native"), False


def _start_herdr(launcher: Path, directory: Path, name: str, args, agent: str, environ: dict[str, str]) -> list[str]:
    started = herdr_host.ensure_server(environ)
    env = {"HERDR_AGENT": agent}
    placed = herdr_host.open_pane(directory, name, env, args.placement, args.workspace, environ)
    herdr_panes.record(placed, "init-agent", name, environ, int(time.time() * 1000))
    herdr_host.run(placed.pane_id, launcher, environ)
    return [
        f"workspace_id={placed.workspace_id}",
        f"tab_id={placed.tab_id}",
        f"pane_id={placed.pane_id}",
        *(["herdr_server=started", "attach=herdr"] if started else []),
    ]


CHANNEL_WARNING_MS = 60_000


def _inbox_channel_args() -> list[str]:
    from scripts.inbox import channel

    return channel.launch_args()


def _answer_channel_warning(pane: str, environ: dict[str, str]) -> str:
    from scripts.inbox import channel

    return "answered" if herdr_host.answer(pane, channel.WARNING, environ, CHANNEL_WARNING_MS) else "unseen"


def main(argv: list[str] | None = None, environ: dict[str, str] | None = None) -> int:
    args = _parser().parse_args(argv)
    active_env = dict(os.environ if environ is None else environ)
    active_env.pop("AGENTIHOOKS_PROFILE_REPORT", None)
    operator_env.fill(active_env)
    try:
        directory = _resolve_directory(args.dir, active_env)
        prompt = Path(args.prompt_file).expanduser().read_text(encoding="utf-8") if args.prompt_file else args.prompt
        name = args.name or f"s-{time.strftime('%y%m%d-%H%M%S')}"
        active_env = _launch_environ(_with_predecessor(active_env, args.handoff), name, args.handoff)
        claude_args = args.claude_args[1:] if args.claude_args[:1] == ["--"] else args.claude_args
        exclude = ""
        agent, reason = (
            (_handoff_agent(args.agent, active_env, claude_args), "handoff")
            if args.handoff
            else agent_choice.choose(args.agent, active_env)
        )
        agent = agent or "claude"
        if args.handoff:
            exclude = _handoff_exclude(agent, active_env)
            if not prompt:
                raise ValueError("--handoff needs the handoff document as --prompt-file")
        claude_args = _prepare_profile(args, agent, claude_args, active_env)
        claude_args = effort_range.launch_args(agent, claude_args, active_env)
        if not args.dry_run:
            prompt = _binding_request(args, agent, prompt, active_env, claude_args)
        channel = args.inbox_channel and agent == "claude"
        claude_args = [*_inbox_channel_args(), *claude_args] if channel else claude_args
        # A handoff must land on another account, so it never falls back to bare Claude.
        launcher, prompt_file = _write_launcher(
            directory,
            name,
            prompt,
            claude_args,
            active_env,
            AgentSpec(
                agent=agent,
                exclude=exclude,
                fallback_bare=not args.handoff,
                resume=args.resume,
                profile=args.profile,
                channel=channel,
                overlays=tuple(args.overlay),
                bundle_revision=args.bundle_revision,
            ),
        )
        host, explicit = _select_host(args.host, active_env)
        command: list[str] = []
        if host != "herdr":
            host, command = _launch_command(launcher, directory, name, active_env)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"agentihooks init-agent: {exc}", file=sys.stderr)
        return 2

    report = [
        f"agent={agent}",
        f"agent_reason={reason}",
        *([f"profile={args.profile}"] if args.profile else []),
        *([f"overlays={','.join(args.overlay)}"] if args.overlay else []),
        f"directory={directory}",
        f"name={name}",
        f"claude_args={shlex.join(claude_args)}",
        f"launcher={launcher}",
        f"prompt_file={prompt_file or 'none'}",
        *(f"{k}={v}" for k, v in zip(("model", "effort"), model_effort(agent, claude_args, active_env))),
    ]
    if args.dry_run:
        target = f"placement={args.placement}" if host == "herdr" else f"command={shlex.join(command)}"
        print("\n".join([f"host={host}", *report, target, "status=dry-run"]))
        return 0

    def discard() -> None:
        launcher.unlink(missing_ok=True)
        if prompt_file is not None:
            prompt_file.unlink(missing_ok=True)

    if agent == "claude":
        trust, why = claude_trust.ensure_trusted(directory, active_env)
        report.append(f"trust={trust}")
        if trust == "untrusted":
            print(
                f"agentihooks init-agent: Claude does not trust {directory} ({why}); {claude_trust.WAIT_NOTICE}",
                file=sys.stderr,
            )
    if agent == "codex":
        moved = restore_hook_order(codex_home(active_env))
        report.append(f"codex_hooks=restored:{','.join(moved)}" if moved else "codex_hooks=unchanged")
    marker = _started_marker(launcher)
    if host == "herdr":
        try:
            report += _start_herdr(launcher, directory, name, args, agent, active_env)
        except (herdr_host.HerdrError, OSError, subprocess.TimeoutExpired) as exc:
            if explicit:
                discard()
                print(f"agentihooks init-agent: {exc}", file=sys.stderr)
                return 2
            report.append(f"herdr_error={exc}")
            try:
                host, command = _launch_command(launcher, directory, name, active_env)
            except RuntimeError as fallback:
                discard()
                print(f"agentihooks init-agent: {exc}; native fallback: {fallback}", file=sys.stderr)
                return 2
    report.insert(0, f"host={host}")
    try:
        if host != "herdr":
            report.append(f"command={shlex.join(command)}")
            subprocess.Popen(command, env=active_env, start_new_session=True)
    except OSError as exc:
        discard()
        print(f"agentihooks init-agent: {exc}", file=sys.stderr)
        return 2
    deadline = time.monotonic() + args.start_timeout
    while not marker.exists():
        if time.monotonic() >= deadline:
            discard()
            print(
                f"agentihooks init-agent: the new terminal did not start the launcher within "
                f"{args.start_timeout:g}s; launch discarded\n{report[-1] if host == 'herdr' else 'command=' + shlex.join(command)}",
                file=sys.stderr,
            )
            return 2
        time.sleep(0.25)
    launcher_at = int(marker.stat().st_mtime * 1000)
    marker.unlink(missing_ok=True)
    report.append("status=started")
    report.append(f"launcher_at={launcher_at}")

    route_path = _route_report(launcher)
    deadline = time.monotonic() + args.route_timeout
    while not route_path.exists() and time.monotonic() < deadline:
        if _selection_refused(active_env):
            break
        time.sleep(0.25)
    route, harness_at = _take_route_report(route_path)
    report.append(f"route_status={route.get('status', 'pending')}")
    report.append(f"harness_at={harness_at or ''}")
    if route.get("account"):
        report.append(f"account={route['account']}")
    if route.get("placement"):
        report.append(f"placement={route['placement']}")
    if route.get("error"):
        report.append(f"route_error={route['error']}")
    pane = next((line.split("=", 1)[1] for line in report if line.startswith("pane_id=")), "")
    herdr_panes.mark(pane, active_env, route_status=route.get("status", "pending"))
    if pane and route.get("status") in ("routed", "bare", "direct"):
        renamed = herdr_host.rename_agent(pane, name, active_env)
        report.append(f"agent_name={herdr_host.agent_name(name) if renamed else 'unset'}")
        if channel:
            report.append(f"channel_warning={_answer_channel_warning(pane, active_env)}")
    if args.handoff and route.get("status") != "routed":
        print("\n".join([*report, "handoff=failed"]))
        print(
            "agentihooks init-agent: handoff failed; the new session was not routed to another account", file=sys.stderr
        )
        return 3
    try:
        report += _binding_result(active_env, args.route_timeout, route)
    except (OSError, ValueError) as exc:
        print("\n".join([*report, "profile_validation=failed"]))
        print(f"agentihooks init-agent: {exc}", file=sys.stderr)
        return 3
    if not args.handoff:
        print("\n".join(report))
        return 0

    from hooks.context.account_sessions import agent_pid
    from hooks.context.broadcast import mark_handed_off

    marked = mark_handed_off(agent_pid(), route["account"])
    print("\n".join([*report, "handoff=done", f"handed_off_sessions={','.join(marked) or 'none'}"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
