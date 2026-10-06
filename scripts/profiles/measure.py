from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import NamedTuple

from scripts import select_profile

PROMPT = "Reply with the single word OK."
LAYERS = ("plugins", "mcp", "persona", "hooks")
PERSONA = ("CLAUDE.md", "rules")
COPIED = ("settings.json", ".claude.json")
STRIPPED = ("CLAUDE_CONFIG_DIR", "AGENTIHOOKS_AGENT_NAME")
CLAUDE_ARGS = ["-p", PROMPT, "--output-format", "stream-json", "--verbose", "--max-turns", "1"]
CODEX_ARGS = ["exec", "--json", "--skip-git-repo-check", PROMPT]
CLAUDE_USAGE = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
TIMEOUT_S = 600


class MeasureError(RuntimeError):
    pass


class Reading(NamedTuple):
    tokens: int
    pending: tuple[str, ...]


def _environment(environ: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in environ.items() if k not in STRIPPED and not k.startswith("AGENTIHOOKS_SWARM")}


def _claude_home(rendered: Path, dst: Path, off: frozenset[str]) -> Path:
    for item in rendered.iterdir():
        if item.name not in COPIED and not ("persona" in off and item.name in PERSONA):
            (dst / item.name).symlink_to(item)
    settings = json.loads((rendered / "settings.json").read_text())
    if "plugins" in off:
        settings["enabledPlugins"] = dict.fromkeys(settings.get("enabledPlugins") or {}, False)
    if "hooks" in off:
        settings.pop("hooks", None)
    (dst / "settings.json").write_text(json.dumps(settings))
    claude_json = json.loads((rendered / ".claude.json").read_text())
    if "mcp" in off:
        claude_json["mcpServers"] = {}
    (dst / ".claude.json").write_text(json.dumps(claude_json))
    return dst


def _codex_home(rendered: Path, dst: Path, off: frozenset[str]) -> Path:
    import tomlkit

    for item in rendered.iterdir():
        if item.name != "config.toml" and not ("persona" in off and item.name == "AGENTS.md"):
            (dst / item.name).symlink_to(item)
    config = tomllib.loads((rendered / "config.toml").read_text())
    for layer in ("plugins", "hooks"):
        if layer in off:
            config.setdefault("features", {})[layer] = False
    if "mcp" in off:
        config.pop("mcp_servers", None)
    state = config.get("hooks", {}).get("state")
    if state:
        # Codex keys hook trust by the hooks file path, so the copy's own path must carry it.
        config["hooks"]["state"] = {f"{dst}{k.removeprefix(str(rendered))}": v for k, v in state.items()}
    (dst / "config.toml").write_text(tomlkit.dumps(config))
    return dst


def first_turn(agent: str, stdout: str) -> Reading | None:
    pending: tuple[str, ...] = ()
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("subtype") == "init":
            pending = tuple(s["name"] for s in event.get("mcp_servers", []) if s.get("status") != "connected")
        if agent == "claude" and event.get("type") == "assistant":
            usage = event["message"]["usage"]
            return Reading(sum(usage.get(key, 0) for key in CLAUDE_USAGE), pending)
        if agent == "codex" and event.get("type") == "turn.completed":
            return Reading(event["usage"]["input_tokens"], pending)
    return None


def measure(
    name: str,
    agent: str,
    off: frozenset[str],
    environ: Mapping[str, str] | None = None,
    run: Callable = subprocess.run,
) -> Reading:
    base = _environment(os.environ if environ is None else environ)
    native = CLAUDE_ARGS if agent == "claude" else CODEX_ARGS
    env, flags = select_profile.prepare(name, agent, "", "", native, base)
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as work:
        if agent == "claude":
            env["CLAUDE_CONFIG_DIR"] = str(_claude_home(Path(env["CLAUDE_CONFIG_DIR"]), Path(home), off))
        else:
            env["CODEX_HOME"] = str(_codex_home(Path(env["CODEX_HOME"]), Path(home), off))
        result = run(
            ["agentihooks", agent, *flags],
            cwd=work,
            env={**base, **env},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
        )
    reading = first_turn(agent, result.stdout)
    if reading is None:
        raise MeasureError(f"no usage in session output: {result.stderr.strip()[-500:]}")
    return reading


def _row(layer: str, reading: Reading, cost: str) -> str:
    return f"{layer:<8}{reading.tokens:>7}{cost:>6}  {','.join(reading.pending)}".rstrip()


def _breakdown(name: str, agent: str) -> list[str]:
    full = measure(name, agent, frozenset())
    lines = [
        f"{name} ({agent}) first turn input tokens",
        f"{'layer':<8}{'tokens':>7}{'cost':>6}  mcp not connected",
        _row("full", full, ""),
    ]
    for layer in LAYERS:
        reading = measure(name, agent, frozenset({layer}))
        lines.append(_row(layer, reading, str(full.tokens - reading.tokens)))
    return lines


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("name")
    parser.add_argument("--agent", choices=("claude", "codex"), default="claude")
    parser.add_argument("--without", action="append", choices=LAYERS, default=[])
    parser.add_argument("--breakdown", action="store_true")


def main(args: argparse.Namespace) -> int:
    try:
        if args.breakdown:
            lines = _breakdown(args.name, args.agent)
        else:
            reading = measure(args.name, args.agent, frozenset(args.without))
            without = f" without {','.join(args.without)}" if args.without else ""
            pending = f", mcp not connected: {','.join(reading.pending)}" if reading.pending else ""
            lines = [f"{args.name} ({args.agent}){without}: {reading.tokens} first turn input tokens{pending}"]
    except (MeasureError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0
