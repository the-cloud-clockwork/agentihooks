import argparse
import os
import shlex
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import yaml

from scripts import init_agent
from scripts.profiles import binding, plugins
from scripts.profiles import render as profiles

OVERLAYS = "AGENTIHOOKS_OVERLAYS"
BUNDLE_REVISION = "AGENTIHOOKS_BUNDLE_REVISION"


def prepare(
    name: str,
    agent: str,
    model: str,
    effort: str,
    agent_args: list[str],
    environ: dict[str, str],
    overlays: Sequence[str] = (),
) -> tuple[dict[str, str], list[str]]:
    if agent not in ("claude", "codex"):
        raise ValueError(f"{agent} per-run profiles are not supported")
    if agent == "codex" and plugins.claude_only(name):
        raise ValueError(f"profile {name} does not support codex; supported harness: claude")
    defaults = _defaults(name)
    prefix = f"AGENTIHOOKS_{agent.upper()}"
    active = dict(environ)
    for key in ("model", "effort"):
        if defaults.get(key):
            active[f"{prefix}_{key.upper()}"] = defaults[key]
    native_model, native_effort, remaining = _native_options(agent, agent_args)
    default_model, default_effort = init_agent.model_effort(agent, [], active)
    flags = init_agent.model_flags(
        agent, model or native_model or default_model, _effort(agent, effort or native_effort or default_effort)
    )
    revision = environ.get(BUNDLE_REVISION, "")
    profiles.render(agent, name, overlays=overlays, bundle_revision=revision)
    env = {"AGENTIHOOKS_PROFILE": name, profiles.CHANNELS: profiles.channels(name), OVERLAYS: ",".join(overlays)}
    env[BUNDLE_REVISION] = revision
    home = "CLAUDE_CONFIG_DIR" if agent == "claude" else "CODEX_HOME"
    env[home] = str(profiles.profile_dir(name, overlays) / agent)
    return env, [*flags, *remaining]


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


def _native_options(agent: str, args: list[str]) -> tuple[str, str, list[str]]:
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--model", "-m", default="")
    parser.add_argument("--effort", default="")
    options, remaining = parser.parse_known_args(args)
    effort = options.effort
    if agent == "codex":
        effort, remaining = _config_effort(remaining)
    return options.model, effort, remaining


def _config_effort(args: list[str]) -> tuple[str, list[str]]:
    effort, kept = "", []
    arguments = iter(args)
    for argument in arguments:
        if argument in ("-c", "--config"):
            value = next(arguments, None)
            if value is None:
                raise ValueError(f"{argument} requires a value")
            pair = [argument, value]
        elif argument.startswith(("-c", "--config=")):
            value = argument[2:] if argument.startswith("-c") else argument.split("=", 1)[1]
            pair = [argument]
        else:
            kept.append(argument)
            continue
        if value.startswith("model_reasoning_effort="):
            effort = value.split("=", 1)[1].strip('"')
        else:
            kept.extend(pair)
    return effort, kept


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    split = arguments.index("--") if "--" in arguments else len(arguments)
    parser = argparse.ArgumentParser(prog="agentihooks select-profile")
    parser.add_argument("name")
    parser.add_argument("--agent", choices=("claude", "codex", "copilot"), default="claude")
    parser.add_argument("--model", default="")
    parser.add_argument("--effort", choices=("minimal", "low", "medium", "high", "xhigh", "max"), default="")
    parser.add_argument("--overlay", action="append", default=[], help="Wear this overlay; repeat for up to three")
    parser.add_argument("--bundle-revision", default="", help="Render only from this bundle commit")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(arguments[:split])
    try:
        environ = {**os.environ, BUNDLE_REVISION: args.bundle_revision}
        env, command = prepare(
            args.name, args.agent, args.model, args.effort, arguments[split + 1 :], environ, args.overlay
        )
    except (OSError, ValueError) as exc:
        print(f"agentihooks select-profile: {exc}", file=sys.stderr)
        if os.environ.get(binding.REPORT):
            binding.refuse(Path(os.environ[binding.REPORT]), f"profile selection failed: {exc}")
        return 2
    command = ["agentihooks", args.agent, *command]
    if args.dry_run:
        print("\n".join([*(f"{key}={value}" for key, value in env.items()), f"argv={shlex.join(command)}"]))
        return 0
    return subprocess.run(command, env={**os.environ, **env}).returncode


def dispatch(argv: list[str]) -> int:
    if argv[0] in ("profile", "profiles"):
        return profiles.main(argv[1:])
    return main(argv[1:])
