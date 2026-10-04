import argparse
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from scripts import agent_choice, herdr_host


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
    root = Path(environ.get("XDG_RUNTIME_DIR", tempfile.gettempdir())) / "agentihooks-claude-terminal"
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


@dataclass(frozen=True)
class AgentSpec:
    agent: str = "claude"
    exclude: str = ""
    fallback_bare: bool = True


def _agent_command(spec: AgentSpec, report: Path, name: str, agent_args: list[str]) -> tuple[list[str], str]:
    """(command, line run before it): Claude routes through `agentihooks claude`; Codex runs directly."""
    if spec.agent == "codex":
        return [
            shutil.which("codex") or "codex",
            *agent_args,
        ], f"printf 'status=direct\\n' > {shlex.quote(str(report))}\n"
    agentihooks_bin = shutil.which("agentihooks") or str(Path(sys.argv[0]).resolve())
    command = [
        agentihooks_bin,
        "claude",
        *(["--agentihooks-exclude", spec.exclude] if spec.exclude else []),
        *(["--agentihooks-fallback-bare"] if spec.fallback_bare else []),
        "--agentihooks-report",
        str(report),
        "--name",
        name,
        *agent_args,
    ]
    return command, ""


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

    command, before = _agent_command(spec, _route_report(launcher), name, claude_args)
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
        "export AGENTIHOOKS_TERMINAL_LAUNCH=1\n"
        f"export AGENTIHOOKS_AGENT_NAME={shlex.quote(name)}\n"
        f"{before}{command_text}\n"
        f"rm -f {shlex.join(cleanup)}\n"
        f"[ -e {shlex.quote(str(root))}/closing-$$ ] && {{ rm -f {shlex.quote(str(root))}/closing-$$; exit 0; }}\n"
        "unset AGENTIHOOKS_AGENT_NAME\n"
        f"exec {shlex.quote(shell)} -l\n",
        encoding="utf-8",
    )
    launcher.chmod(0o700)
    return launcher, prompt_file


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
        help="Agent to open; default the first in $AGENTIHOOKS_AGENT_PRIORITY (claude,codex) with quota left",
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
    herdr_host.run(placed.pane_id, launcher, environ)
    return [
        f"workspace_id={placed.workspace_id}",
        f"tab_id={placed.tab_id}",
        f"pane_id={placed.pane_id}",
        *(["herdr_server=started", "attach=herdr"] if started else []),
    ]


def main(argv: list[str] | None = None, environ: dict[str, str] | None = None) -> int:
    args = _parser().parse_args(argv)
    active_env = dict(os.environ if environ is None else environ)
    try:
        directory = _resolve_directory(args.dir, active_env)
        prompt = Path(args.prompt_file).expanduser().read_text(encoding="utf-8") if args.prompt_file else args.prompt
        name = args.name or f"s-{time.strftime('%y%m%d-%H%M%S')}"
        claude_args = args.claude_args[1:] if args.claude_args[:1] == ["--"] else args.claude_args
        exclude = ""
        if args.handoff and args.agent == "codex":
            raise ValueError("--handoff moves work to another Claude account; it cannot open codex")
        agent, reason = ("claude", "handoff") if args.handoff else agent_choice.choose(args.agent, active_env)
        if args.handoff:
            from hooks.context.account_sessions import UNROUTED, environment_account

            current = environment_account(active_env)
            exclude = "" if current == UNROUTED else current
            if not prompt:
                raise ValueError("--handoff needs the handoff document as --prompt-file")
        # A handoff must land on another account, so it never falls back to bare Claude.
        launcher, prompt_file = _write_launcher(
            directory,
            name,
            prompt,
            claude_args,
            active_env,
            AgentSpec(agent=agent, exclude=exclude, fallback_bare=not args.handoff),
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
        f"directory={directory}",
        f"name={name}",
        f"claude_args={shlex.join(claude_args)}",
        f"launcher={launcher}",
        f"prompt_file={prompt_file or 'none'}",
    ]
    if args.dry_run:
        target = f"placement={args.placement}" if host == "herdr" else f"command={shlex.join(command)}"
        print("\n".join([f"host={host}", *report, target, "status=dry-run"]))
        return 0

    def discard() -> None:
        launcher.unlink(missing_ok=True)
        if prompt_file is not None:
            prompt_file.unlink(missing_ok=True)

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
    marker.unlink(missing_ok=True)
    report.append("status=started")

    route_path = _route_report(launcher)
    deadline = time.monotonic() + args.route_timeout
    while not route_path.exists() and time.monotonic() < deadline:
        time.sleep(0.25)
    route = _read_route_report(route_path) if route_path.exists() else {}
    route_path.unlink(missing_ok=True)
    report.append(f"route_status={route.get('status', 'pending')}")
    if route.get("account"):
        report.append(f"account={route['account']}")
    if route.get("placement"):
        report.append(f"placement={route['placement']}")
    if route.get("error"):
        report.append(f"route_error={route['error']}")
    pane = next((line.split("=", 1)[1] for line in report if line.startswith("pane_id=")), "")
    if pane and route.get("status") in ("routed", "bare", "direct"):
        renamed = herdr_host.rename_agent(pane, name, active_env)
        report.append(f"agent_name={herdr_host.agent_name(name) if renamed else 'unset'}")
    if not args.handoff:
        print("\n".join(report))
        return 0

    if route.get("status") != "routed":
        print("\n".join([*report, "handoff=failed"]))
        print(
            "agentihooks init-agent: handoff failed; the new session was not routed to another account",
            file=sys.stderr,
        )
        return 3
    from hooks.context.account_sessions import agent_pid
    from hooks.context.broadcast import mark_handed_off

    marked = mark_handed_off(agent_pid(), route["account"])
    print("\n".join([*report, "handoff=done", f"handed_off_sessions={','.join(marked) or 'none'}"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
