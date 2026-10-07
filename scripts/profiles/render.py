from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import shutil
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from hooks.context import quarantine
from scripts.claude_config import claude_home, claude_json
from scripts.profiles import binding, browser, connectors, plugins, sources
from scripts.targets._common import _atomic_write, _install_module, agents_skills_home, build_persona
from scripts.targets.claude_target import settings_document
from scripts.targets.codex_target import codex_home

SHARED = ("projects", "sessions", "todos", "plugins", ".credentials.json")
CODEX_STATE = ("auth.json", "sessions", "history.jsonl", "session_index.jsonl", "hooks.json")
CODEX_INHERITED = ("model", "model_reasoning_effort", "service_tier", "notify", "projects")
STAMP = ".agentihooks-render.json"
CHANNELS, BRAIN = "AGENTIHOOKS_BASE_CHANNELS", "brain"
HEADER = "<!-- agentihooks rendered profile -->"
FOOTER = "<!-- end agentihooks rendered profile -->"
SEED_KEYS = ("hasCompletedOnboarding", "lastOnboardingVersion", "hasTrustDialogAccepted", "oauthAccount", "userID")
PROJECT_SEED_KEYS = ("hasTrustDialogAccepted", "hasClaudeMdExternalIncludesApproved")


def _is_doc(path: Path) -> bool:
    return path.suffix == ".md" and path.name != "README.md"


FEATURES = (("skills", Path.is_dir), ("agents", _is_doc), ("commands", _is_doc))


def rendered_root() -> Path:
    return _install_module().AGENTIHOOKS_STATE_DIR / "profiles"


def live_root() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".agentihooks" / "profiles"


def _refuse_live_render_from_another_checkout(name: str) -> None:
    _i = _install_module()
    running, installed = _i.AGENTIHOOKS_ROOT.resolve(), _i.install_root().resolve()
    if running == installed or not rendered_root().resolve().is_relative_to(live_root().resolve()):
        return
    raise ValueError(
        f"this run comes from {running}, not the installed agentihooks at {installed}, so it renders only "
        f"into a scratch home: agentihooks profile render {name} --out <dir>"
    )


def _global_env() -> dict[str, str]:
    # A session running in a rendered home renders too; shared data must still resolve to the operator's home.
    return {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}


def _chain(name: str) -> list[tuple[str, Path]]:
    _i = _install_module()
    if _i._resolve_profile_dir(name) is None:
        raise ValueError(f"Profile '{name}' not found")
    return _i._resolve_profile_chain(name)


def _stamp(bundle: Path | None, dirs: list[tuple[str, Path]]) -> dict:
    commit = ""
    if bundle is not None:
        head = subprocess.run(["git", "-C", str(bundle), "rev-parse", "HEAD"], capture_output=True, text=True)
        commit = head.stdout.strip()
    chain = [n for n, _ in dirs]
    return {
        "bundle_commit": commit,
        "chain": chain,
        "plugins": plugins.role_defaults(chain),
        **({"browser": browser.spec()} if browser.enabled(chain) else {}),
        "corrections": quarantine.digest(),
    }


def stamp(name: str) -> dict:
    return _stamp(_install_module()._get_bundle_path(), _chain(name))


def _roots(bundle: Path | None, dirs: list[tuple[str, Path]]) -> list[Path]:
    return ([bundle] if bundle is not None else []) + [d for _, d in dirs]


def _features(subdir: str, keep, bundle: Path | None, dirs: list[tuple[str, Path]]) -> dict[str, Path]:
    sources = [_install_module().PACKAGE_FEATURES_DIR / subdir] + [r / ".claude" / subdir for r in _roots(bundle, dirs)]
    found: dict[str, Path] = {}
    for src in sources:
        if src.is_dir():
            found.update({item.name: item for item in sorted(src.iterdir()) if keep(item)})
    return found


def _settings(target: str, bundle: Path | None, dirs: list[tuple[str, Path]]) -> dict:
    _i = _install_module()
    base = _i._load_native_layer(_i.PROFILES_DIR / "_base" / _i._NATIVE_BASE_NAME[target])
    doc = _i.substitute_paths(base, dst=str(_i.install_root()))
    doc = _i.substitute_paths(doc, "__PYTHON__", str(_i._detect_venv() or sys.executable))
    for root in _roots(bundle, dirs):
        path = _i._native_layer_path(root, target, _i._NATIVE_SETTINGS_NAME)
        if path:
            layer = _i._load_native_layer(path)
            if target == "claude":
                layer = _i._resolve_profile_hook_paths(layer, root)
            doc = _i._deep_merge(doc, layer)
    return doc


def _claude_settings(bundle: Path | None, dirs: list[tuple[str, Path]]) -> dict:
    from scripts.profile_telemetry import apply_collector_env, apply_langfuse_env

    _i = _install_module()
    doc = _settings("claude", bundle, dirs)
    env = doc["env"]
    env[CHANNELS] = _with_brain(env.get(CHANNELS, ""))
    apply_langfuse_env(doc, dirs, None)
    apply_collector_env(doc)
    default_home = claude_home(_global_env())
    global_settings = default_home / "settings.json"
    personal = _i.load_json(global_settings) if global_settings.exists() else {}
    # Claude reads the default home as an ancestor project folder when the working folder sits under it.
    excludes = [str(default_home / "CLAUDE.md"), str(default_home / "rules" / "**")]
    settings = settings_document(doc)
    chain = [n for n, _ in dirs]
    enabled_plugins = plugins.allowed(chain, settings.get("enabledPlugins") or {})
    if browser.enabled(chain):
        enabled_plugins.pop(plugins.PLAYWRIGHT, None)
    return {
        **{k: personal[k] for k in _i.PERSONAL_KEYS if k in personal},
        **settings,
        "enabledPlugins": enabled_plugins,
        "claudeMdExcludes": excludes,
    }


def _with_brain(channels: str) -> str:
    names = [name.strip() for name in channels.split(",") if name.strip()]
    return ",".join(names if BRAIN in names else [*names, BRAIN])


def channels(name: str) -> str:
    env = _settings("claude", _install_module()._get_bundle_path(), _chain(name))["env"]
    return _with_brain(env.get(CHANNELS, ""))


def _mcp_servers(target: str, bundle: Path | None, dirs: list[tuple[str, Path]]) -> dict:
    _i = _install_module()
    servers = dict(_i._build_mcp_config("all")["mcpServers"])
    for root in _roots(bundle, dirs):
        path = _i._native_layer_path(root, target, _i._NATIVE_MCP_NAME) or _i._native_layer_path(
            root, "claude", _i._NATIVE_MCP_NAME
        )
        if path:
            servers.update(_i._load_native_layer(path).get("mcpServers") or {})
    return browser.configure(servers, [name for name, _ in dirs])


def _seed(src: Path) -> dict:
    operator = _read_json(src) or {}
    doc = {key: operator[key] for key in SEED_KEYS if key in operator}
    doc["hasCompletedOnboarding"] = True
    if isinstance(operator.get("projects"), dict):
        doc["projects"] = {
            path: {key: project[key] for key in PROJECT_SEED_KEYS if key in project}
            for path, project in operator["projects"].items()
        }
    return doc


def _claude_json(out: Path, servers: dict) -> None:
    _i = _install_module()
    dst = out / ".claude.json"
    doc = _i.load_json(dst) if dst.exists() else _seed(claude_json(_global_env()))
    doc.setdefault("hasCompletedOnboarding", True)
    doc["mcpServers"] = servers
    _i.save_json(dst, doc)


def _relink(dst: Path, items: dict[str, Path]) -> None:
    if dst.is_dir():
        for old in dst.iterdir():
            if old.is_symlink():
                old.unlink()
    dst.mkdir(exist_ok=True)
    for name, src in items.items():
        (dst / name).symlink_to(src)


def _link_commands(skills: Path, commands: Path) -> None:
    for old in skills.iterdir():
        if old.is_dir() and not old.is_symlink() and [p.name for p in old.iterdir()] == ["SKILL.md"]:
            (old / "SKILL.md").unlink()
            old.rmdir()
    if not commands.is_dir():
        return
    for command in sorted(commands.iterdir()):
        skill = skills / command.stem
        # Codex skips a symlinked SKILL.md and one without frontmatter; a hardlink keeps it the Claude file.
        if skill.exists() or not command.read_text().startswith("---"):
            continue
        skill.mkdir()
        (skill / "SKILL.md").hardlink_to(command.resolve())


def _persona(name: str, target: str, bundle: Path | None, dirs: list[tuple[str, Path]], chain: list[str]) -> str:
    items = _features("rules", _is_doc, bundle, dirs)
    sources.write(sources.path(name, target, rendered_root()), sources.rows(bundle, dirs, items))
    rules = [("rule", n, quarantine.annotate(p.read_text(), sources.source(p))) for n, p in items.items()]
    text = quarantine.passages(build_persona(dirs, chain, bundle, rules, HEADER, FOOTER))
    ending = f"\n\n{FOOTER}\n"
    return binding.persona(text.removesuffix(ending)) + ending


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def render_claude(name: str, force: bool = False) -> Path | None:
    _refuse_live_render_from_another_checkout(name)
    _i = _install_module()
    bundle, dirs = _i._get_bundle_path(), _chain(name)
    current = _stamp(bundle, dirs)
    out = rendered_root() / name / "claude"
    if (
        not force
        and _read_json(out / STAMP) == current
        and not (out / "rules").exists()
        and sources.path(name, "claude", rendered_root()).is_file()
        and (out / binding.FILE).is_file()
        and connectors.path(name, "claude", rendered_root()).is_file()
        and "hasCompletedOnboarding" in (_read_json(out / ".claude.json") or {})
    ):
        return None
    out.mkdir(parents=True, exist_ok=True)
    servers, deny, mounts = connectors.claude(_mcp_servers("claude", bundle, dirs), str(out / ".claude.json"))
    connectors.write(connectors.path(name, "claude", rendered_root()), mounts, name, "claude")
    settings = _claude_settings(bundle, dirs)
    if deny:
        permissions = settings["permissions"]
        permissions["deny"] = [*permissions.get("deny", []), *deny]
    _i.save_json(out / "settings.json", settings)
    for subdir, keep in FEATURES:
        _relink(out / subdir, _features(subdir, keep, bundle, dirs))
    if (out / "rules").is_dir():
        shutil.rmtree(out / "rules")
    _atomic_write(out / "CLAUDE.md", _persona(name, "claude", bundle, dirs, current["chain"]))
    _claude_json(out, servers)
    shared = claude_home(_global_env())
    for item in SHARED:
        link = out / item
        if link.is_symlink():
            link.unlink()
        if not link.exists():
            link.symlink_to(shared / item)
    # Claude Code refuses plan file writes that resolve through a symlink.
    plans = out / "plans"
    if plans.is_symlink():
        plans.unlink()
    plans.mkdir(exist_ok=True)
    _i.save_json(out / STAMP, current)
    binding.write(out, name, "claude")
    return out


def _codex_stamp(path: Path) -> dict | None:
    try:
        return tomllib.loads(path.read_text()).get("agentihooks", {}).get("render")
    except (OSError, ValueError):
        return None


def _read_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _operator_codex_home() -> Path:
    home = codex_home()
    # A session running in a rendered Codex home renders too; links must still reach the operator's home.
    return Path.home() / ".codex" if home.resolve().is_relative_to(rendered_root().resolve()) else home


def _link(link: Path, target: Path) -> None:
    if link.is_symlink():
        link.unlink()
    elif link.exists():
        shutil.move(link, link.with_name(f"{link.name}.bak.{datetime.now(timezone.utc):%Y%m%d%H%M%S}"))
    link.symlink_to(target)


def _codex_config(installed: dict, operator: Path, out: Path, settings: dict) -> dict:
    doc = {k: v for k, v in settings.items() if k != "mcp_servers"}
    doc |= {key: installed[key] for key in CODEX_INHERITED if key in installed and key not in doc}
    doc["sqlite_home"] = str(operator)
    doc["features"]["hooks"] = True
    source = f"{operator / 'hooks.json'}:"
    state = (installed.get("hooks") or {}).get("state") or {}
    trusted = {f"{out / 'hooks.json'}:{k.removeprefix(source)}": v for k, v in state.items() if k.startswith(source)}
    if trusted:
        doc["hooks"] = {"state": trusted}
    return doc


def render_codex(name: str, force: bool = False) -> Path | None:
    import tomlkit

    _i = _install_module()
    bundle, dirs = _i._get_bundle_path(), _chain(name)
    claude_fresh = render_claude(name, force=force) is None
    operator = _operator_codex_home()
    config = operator / "config.toml"
    text = config.read_text() if config.exists() else ""
    installed = tomllib.loads(text)
    current = {"render": _stamp(bundle, dirs), "operator": hashlib.sha256(text.encode()).hexdigest()}
    out = rendered_root() / name / "codex"
    manifest = sources.path(name, "codex", rendered_root())
    if (
        not force
        and claude_fresh
        and manifest.is_file()
        and (out / binding.FILE).is_file()
        and connectors.path(name, "codex", rendered_root()).is_file()
        and _read_toml(out / "config.toml").get("agentihooks") == current
    ):
        return None
    claude = rendered_root() / name / "claude"
    out.mkdir(exist_ok=True)
    _link(out / "AGENTS.md", claude / "CLAUDE.md")
    _relink(out / "skills", {p.name: p for p in sorted((claude / "skills").iterdir())})
    _link_commands(out / "skills", claude / "commands")
    for item in CODEX_STATE:
        _link(out / item, operator / item)
    _link(manifest, sources.path(name, "claude", rendered_root()))
    doc = _codex_config(installed, operator, out, _settings("codex", bundle, dirs))
    doc["project_doc_max_bytes"] = max(65536, int(len((claude / "CLAUDE.md").read_bytes()) * 1.25))
    servers, mounts = connectors.codex(_mcp_servers("codex", bundle, dirs))
    connectors.write(connectors.path(name, "codex", rendered_root()), mounts, name, "codex")
    if servers:
        doc["mcp_servers"] = servers
    root = agents_skills_home()
    hidden_skills = [p for p in sorted(root.iterdir()) if p.is_dir()] if root.is_dir() else []
    if hidden_skills:
        doc["skills"] = {"config": [{"path": str(p / "SKILL.md"), "enabled": False} for p in hidden_skills]}
    doc["agentihooks"] = current
    _atomic_write(out / "config.toml", tomlkit.dumps(doc))
    binding.write(out, name, "codex")
    legacy = operator / f"{name}.config.toml"
    if _codex_stamp(legacy):
        legacy.unlink()
    return out


def render(target: str, name: str, force: bool = False) -> Path | None:
    renderers = {"claude": render_claude, "codex": render_codex}
    if target not in renderers:
        raise ValueError(f"{target} per-run profiles are not supported")
    return renderers[target](name, force=force)


def rendered_profiles(target: str) -> list[str]:
    if target == "claude":
        return sorted(home.parent.name for home in rendered_root().glob("*/claude"))
    if target == "codex":
        homes = {config.parent.parent.name for config in rendered_root().glob("*/codex/config.toml")}
        legacy = _operator_codex_home().glob("*.config.toml")
        return sorted(homes | {p.name.removesuffix(".config.toml") for p in legacy if _codex_stamp(p)})
    return []


def _render_scratch(args: argparse.Namespace) -> int:
    bundle = args.bundle or _install_module()._get_bundle_path()
    env = {**os.environ, "AGENTIHOOKS_HOME": str(args.out.resolve())}
    if bundle is not None:
        env["AGENTIHOOKS_BUNDLE_PATH"] = str(bundle.resolve())
    argv = [sys.executable, "-m", "scripts.profiles.render", "render", args.name, "--target", args.target]
    # The child resolves the agentihooks home, bundle and corrections store at import, so only a fresh process sees them.
    cwd = Path(__file__).resolve().parents[2]
    return subprocess.run([*argv, *(["--force"] if args.force else [])], env=env, cwd=cwd).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentihooks profile")
    commands = parser.add_subparsers(dest="command", required=True)
    render_cmd = commands.add_parser("render", help="Render a profile into its own home for one harness")
    render_cmd.add_argument("name")
    render_cmd.add_argument("--target", choices=("claude", "codex", "copilot"), default="claude")
    render_cmd.add_argument("--force", action="store_true")
    render_cmd.add_argument("--out", type=Path, help="Render into this scratch agentihooks home, not the live one")
    render_cmd.add_argument("--bundle", type=Path, help="Bundle for --out (default: the linked bundle)")
    from scripts.profiles import measure

    measure.add_arguments(commands.add_parser("measure", help="Print a profile's first turn input tokens"))
    validate = commands.add_parser("validate", help="Validate the mounted profile through its live harness")
    validate.add_argument("--canary", required=True)
    args = parser.parse_args(argv)
    if args.command == "validate":
        return binding.main(args.canary)
    if args.command == "measure":
        return measure.main(args)
    if args.bundle is not None and args.out is None:
        render_cmd.error("--bundle needs --out")
    if args.target == "copilot":
        print("copilot per-run profiles are not supported", file=sys.stderr)
        return 2
    if args.out is not None:
        return _render_scratch(args)
    try:
        out = render(args.target, args.name, force=args.force)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if out is None:
        print(f"{args.name} ({args.target}) is up to date")
    else:
        print(f"Rendered {args.name} ({args.target}) → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
