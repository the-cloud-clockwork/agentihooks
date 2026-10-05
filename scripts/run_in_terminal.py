import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path

from scripts import herdr_host
from scripts.init_agent import _select_host

NATIVE_SCRIPT = (
    Path(__file__).resolve().parents[1] / "profiles/package/skills/run-in-terminal/scripts/02_run_terminal.sh"
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentihooks run-in-terminal", description="Run a command in a new terminal")
    parser.add_argument("--dir", default="", help="Directory to run in (default $HOME)")
    parser.add_argument("--title", default="", help="Tab title (default the directory name)")
    parser.add_argument("--host", choices=("herdr", "native"), default="", help="Terminal host")
    parser.add_argument("--workspace", default="", help="herdr: workspace label for the tab")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Command after --; none opens a shell")
    return parser


def _in_herdr(directory: Path, title: str, command: str, workspace: str, environ: dict[str, str]) -> list[str]:
    herdr_host.ensure_server(environ)
    placed = herdr_host.open_pane(directory, title, {}, "tab", workspace, environ)
    if command:
        herdr_host._cli(["pane", "run", placed.pane_id, command], environ)
    return [f"workspace_id={placed.workspace_id}", f"tab_id={placed.tab_id}", f"pane_id={placed.pane_id}"]


def main(argv: list[str] | None = None, environ: dict[str, str] | None = None) -> int:
    args = _parser().parse_args(argv)
    env = dict(os.environ if environ is None else environ)
    directory = Path(args.dir or env.get("HOME", str(Path.home()))).expanduser().resolve()
    title = args.title or directory.name
    parts = args.command[1:] if args.command[:1] == ["--"] else args.command
    command = parts[0] if len(parts) == 1 else shlex.join(parts)
    host, explicit = _select_host(args.host, env)
    report = [f"directory={directory}", f"title={title}", f"command={command or 'shell'}"]
    if host == "herdr":
        try:
            print("\n".join(["host=herdr", *report, *_in_herdr(directory, title, command, args.workspace, env)]))
            return 0
        except (herdr_host.HerdrError, OSError, subprocess.TimeoutExpired) as exc:
            if explicit:
                print(f"agentihooks run-in-terminal: {exc}", file=sys.stderr)
                return 2
            report.append(f"herdr_error={exc}")
    done = subprocess.run([str(NATIVE_SCRIPT), str(directory), title, *([command] if command else [])])
    print("\n".join(["host=native", *report]))
    return done.returncode


if __name__ == "__main__":
    raise SystemExit(main())
