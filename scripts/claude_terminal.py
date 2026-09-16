import argparse
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


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


def _write_launcher(
    directory: Path,
    name: str,
    prompt: str,
    claude_args: list[str],
    environ: dict[str, str],
) -> tuple[Path, Path | None]:
    root = _runtime_dir(environ)
    safe_name = "".join(character if character.isalnum() or character in "._-" else "_" for character in name)
    stamp = f"{time.strftime('%y%m%d-%H%M%S')}-{os.getpid()}"
    launcher = root / f"{safe_name}-{stamp}.sh"
    prompt_file = root / f"{safe_name}-{stamp}.prompt" if prompt else None
    if prompt_file is not None:
        prompt_file.write_text(prompt, encoding="utf-8")
        prompt_file.chmod(0o600)

    agentihooks_bin = shutil.which("agentihooks") or str(Path(sys.argv[0]).resolve())
    command = [
        agentihooks_bin,
        "claude",
        "--agentihooks-fallback-bare",
        "--name",
        name,
        *claude_args,
    ]
    if prompt_file is not None:
        command_text = f'{shlex.join(command)} "$(cat {shlex.quote(str(prompt_file))})"'
    else:
        command_text = shlex.join(command)
    cleanup = [str(launcher), *([str(prompt_file)] if prompt_file is not None else [])]
    shell = environ.get("SHELL") or "/bin/bash"
    launcher.write_text(
        "#!/usr/bin/env bash\n"
        "set -u\n"
        f"cd {shlex.quote(str(directory))} || exit 1\n"
        f"{command_text}\n"
        f"rm -f {shlex.join(cleanup)}\n"
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
        powershell = shutil.which("powershell.exe")
        if not wt or not powershell:
            raise RuntimeError("WSL interop requires wt.exe and powershell.exe")
        distro = environ.get("WSL_DISTRO_NAME", "")
        wsl_call = ["wsl.exe", *(["-d", distro] if distro else []), "--", "bash", "-lc", str(launcher)]
        ps_command = "& { " + " ".join("'" + value.replace("'", "''") + "'" for value in wsl_call) + " }"
        return "wsl", [
            wt,
            "-w",
            "0",
            "new-tab",
            "--title",
            title,
            powershell,
            "-NoExit",
            "-NoProfile",
            "-Command",
            ps_command,
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
    parser.add_argument("claude_args", nargs=argparse.REMAINDER, help="Arguments after -- pass through to Claude")
    return parser


def main(argv: list[str] | None = None, environ: dict[str, str] | None = None) -> int:
    args = _parser().parse_args(argv)
    active_env = dict(os.environ if environ is None else environ)
    try:
        directory = _resolve_directory(args.dir, active_env)
        prompt = Path(args.prompt_file).expanduser().read_text(encoding="utf-8") if args.prompt_file else args.prompt
        name = args.name or f"s-{time.strftime('%y%m%d-%H%M%S')}"
        claude_args = args.claude_args[1:] if args.claude_args[:1] == ["--"] else args.claude_args
        launcher, prompt_file = _write_launcher(directory, name, prompt, claude_args, active_env)
        host, command = _launch_command(launcher, directory, name, active_env)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"agentihooks claude-terminal: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"host={host}")
        print(f"directory={directory}")
        print(f"name={name}")
        print(f"launcher={launcher}")
        print(f"prompt_file={prompt_file or 'none'}")
        print(f"command={shlex.join(command)}")
        return 0

    try:
        subprocess.Popen(command, env=active_env, start_new_session=True)
    except OSError as exc:
        launcher.unlink(missing_ok=True)
        if prompt_file is not None:
            prompt_file.unlink(missing_ok=True)
        print(f"agentihooks claude-terminal: {exc}", file=sys.stderr)
        return 2
    print(f"Opened routed Claude terminal '{name}' at {directory} ({host})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
