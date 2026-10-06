import argparse
import os
import shlex
import subprocess
import sys

import yaml

from scripts import init_agent
from scripts.profiles import render as profiles


def prepare(
    name: str, agent: str, model: str, effort: str, agent_args: list[str], environ: dict[str, str]
) -> tuple[dict[str, str], list[str]]:
    if agent not in ("claude", "codex"):
        raise ValueError(f"{agent} per-run profiles are not supported")
    defaults = _defaults(name)
    prefix = f"AGENTIHOOKS_{agent.upper()}"
    active = dict(environ)
    for key in ("model", "effort"):
        if defaults.get(key):
            active[f"{prefix}_{key.upper()}"] = defaults[key]
    native_model, native_effort = init_agent.model_effort(agent, agent_args, active)
    flags = init_agent.model_flags(agent, model or native_model, _effort(agent, effort or native_effort))
    profiles.render(agent, name)
    env = {"AGENTIHOOKS_PROFILE": name}
    if agent == "claude":
        env["CLAUDE_CONFIG_DIR"] = str(profiles.rendered_root() / name / "claude")
    else:
        flags = ["-p", name, *flags]
    return env, [*flags, *_without_model_flags(agent_args)]


def _defaults(name: str) -> dict:
    defaults = {}
    for _, directory in profiles._chain(name):
        path = directory / "profile.yml"
        if path.exists():
            defaults.update(yaml.safe_load(path.read_text()) or {})
    return defaults


def _effort(agent: str, effort: str) -> str:
    mapped = {("claude", "minimal"): "low", ("codex", "max"): "xhigh"}.get((agent, effort), effort)
    if mapped != effort:
        print(f"agentihooks select-profile: {agent} effort {effort} maps to {mapped}", file=sys.stderr)
    return mapped


def _without_model_flags(args: list[str]) -> list[str]:
    kept = []
    arguments = iter(args)
    for argument in arguments:
        if argument in ("--model", "-m", "--effort"):
            next(arguments)
        elif argument in ("-c", "--config"):
            value = next(arguments)
            if not value.startswith("model_reasoning_effort="):
                kept.extend((argument, value))
        elif not argument.startswith(
            ("--model=", "--effort=", "-cmodel_reasoning_effort=", "--config=model_reasoning_effort=")
        ):
            kept.append(argument)
    return kept


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    split = arguments.index("--") if "--" in arguments else len(arguments)
    parser = argparse.ArgumentParser(prog="agentihooks select-profile")
    parser.add_argument("name")
    parser.add_argument("--agent", choices=("claude", "codex", "copilot"), default="claude")
    parser.add_argument("--model", default="")
    parser.add_argument("--effort", choices=("minimal", "low", "medium", "high", "xhigh", "max"), default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(arguments[:split])
    try:
        env, command = prepare(args.name, args.agent, args.model, args.effort, arguments[split + 1 :], dict(os.environ))
    except (OSError, ValueError) as exc:
        print(f"agentihooks select-profile: {exc}", file=sys.stderr)
        return 2
    command = ["agentihooks", args.agent, *command]
    if args.dry_run:
        print("\n".join([*(f"{key}={value}" for key, value in env.items()), f"argv={shlex.join(command)}"]))
        return 0
    return subprocess.run(command, env={**os.environ, **env}).returncode
