from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

from scripts.claude_config import claude_home, claude_json
from scripts.profiles import sources
from scripts.targets._common import _atomic_write, _install_module, agents_skills_home, build_persona
from scripts.targets.claude_target import enabled_plugins, settings_document
from scripts.targets.codex_target import codex_home

SHARED = ("projects", "sessions", "todos", "plans", "plugins", ".credentials.json")
CODEX_KEYS = ("model", "model_reasoning_effort", "sandbox_mode", "approval_policy")
STAMP = ".agentihooks-render.json"
CHANNELS, BRAIN = "AGENTIHOOKS_BASE_CHANNELS", "brain"
HEADER = "<!-- agentihooks rendered profile -->"
FOOTER = "<!-- end agentihooks rendered profile -->"


def _is_doc(path: Path) -> bool:
    return path.suffix == ".md" and path.name != "README.md"


FEATURES = (("skills", Path.is_dir), ("agents", _is_doc), ("commands", _is_doc), ("rules", _is_doc))


def rendered_root() -> Path:
    return _install_module().AGENTIHOOKS_STATE_DIR / "profiles"


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
    operator = _read_json(claude_home(_global_env()) / "settings.json") or {}
    plugins = dict(sorted((operator.get("enabledPlugins") or {}).items()))
    return {"bundle_commit": commit, "chain": [n for n, _ in dirs], "plugins": plugins}


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
    doc = _i.substitute_paths(_i._load_native_layer(_i.PROFILES_DIR / "_base" / _i._NATIVE_BASE_NAME[target]))
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
    return {
        **{k: personal[k] for k in _i.PERSONAL_KEYS if k in personal},
        **settings,
        "enabledPlugins": enabled_plugins(
            personal.get("enabledPlugins") or {}, settings.get("enabledPlugins") or {}, bundle
        ),
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
    return servers


def _claude_json(out: Path, bundle: Path | None, dirs: list[tuple[str, Path]]) -> None:
    from scripts.targets._common import drop_if_credentialed, sanitize_env_and_headers

    _i = _install_module()
    dst = out / ".claude.json"
    src = dst if dst.exists() else claude_json(_global_env())
    doc = _i.load_json(src) if src.exists() else {}
    servers = {}
    for name, spec in _mcp_servers("claude", bundle, dirs).items():
        if not drop_if_credentialed(name, spec, str(dst)):
            servers[name] = sanitize_env_and_headers(name, spec, str(dst))
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


def _render_rules(dst: Path, items: dict[str, Path]) -> None:
    dst.mkdir(exist_ok=True)
    for old in dst.iterdir():
        if old.is_file() or old.is_symlink():
            old.unlink()
    for name, src in items.items():
        _atomic_write(dst / name, src.read_text())


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def render_claude(name: str, force: bool = False) -> Path | None:
    _i = _install_module()
    bundle, dirs = _i._get_bundle_path(), _chain(name)
    current = _stamp(bundle, dirs)
    out = rendered_root() / name / "claude"
    if (
        not force
        and _read_json(out / STAMP) == current
        and not any(rule.is_symlink() for rule in (out / "rules").iterdir())
        and sources.path(name, "claude", rendered_root()).is_file()
    ):
        return None
    out.mkdir(parents=True, exist_ok=True)
    _i.save_json(out / "settings.json", _claude_settings(bundle, dirs))
    for subdir, keep in FEATURES:
        items = _features(subdir, keep, bundle, dirs)
        if subdir == "rules":
            _render_rules(out / subdir, items)
            sources.write(sources.path(name, "claude", rendered_root()), sources.rows(bundle, dirs, items))
        else:
            _relink(out / subdir, items)
    _atomic_write(out / "CLAUDE.md", build_persona(dirs, current["chain"], bundle, [], HEADER, FOOTER))
    _claude_json(out, bundle, dirs)
    shared = claude_home(_global_env())
    for item in SHARED:
        link = out / item
        if link.is_symlink():
            link.unlink()
        if not link.exists():
            link.symlink_to(shared / item)
    _i.save_json(out / STAMP, current)
    return out


def _codex_stamp(path: Path) -> dict | None:
    try:
        return tomllib.loads(path.read_text()).get("agentihooks", {}).get("render")
    except (OSError, ValueError):
        return None


def render_codex(name: str, force: bool = False) -> Path | None:
    import tomlkit

    _i = _install_module()
    bundle, dirs = _i._get_bundle_path(), _chain(name)
    current = _stamp(bundle, dirs)
    path = codex_home() / f"{name}.config.toml"
    manifest = sources.path(name, "codex", rendered_root())
    if not force and _codex_stamp(path) == current and manifest.is_file():
        return None
    settings = _settings("codex", bundle, dirs)
    doc: dict = {key: settings[key] for key in CODEX_KEYS if key in settings}
    items = _features("rules", _is_doc, bundle, dirs)
    sources.write(manifest, sources.rows(bundle, dirs, items))
    rules = [("rule", n, p.read_text()) for n, p in items.items()]
    doc["developer_instructions"] = build_persona(dirs, current["chain"], bundle, rules, HEADER, FOOTER)
    global_config = codex_home() / "config.toml"
    installed = tomllib.loads(global_config.read_text()).get("mcp_servers", {}) if global_config.exists() else {}
    hidden_servers = sorted(set(installed) - set(_mcp_servers("codex", bundle, dirs)))
    if hidden_servers:
        doc["mcp_servers"] = {server: {"enabled": False} for server in hidden_servers}
    keep = _features("skills", Path.is_dir, bundle, dirs)
    root = agents_skills_home()
    hidden_skills = [p for p in sorted(root.iterdir()) if p.is_dir() and p.name not in keep] if root.is_dir() else []
    if hidden_skills:
        doc["skills"] = {"config": [{"path": str(p / "SKILL.md"), "enabled": False} for p in hidden_skills]}
    doc["agentihooks"] = {"render": current}
    _atomic_write(path, tomlkit.dumps(doc))
    return path


def render(target: str, name: str, force: bool = False) -> Path | None:
    renderers = {"claude": render_claude, "codex": render_codex}
    if target not in renderers:
        raise ValueError(f"{target} per-run profiles are not supported")
    return renderers[target](name, force=force)


def rendered_profiles(target: str) -> list[str]:
    if target == "claude":
        return sorted(home.parent.name for home in rendered_root().glob("*/claude"))
    if target == "codex":
        return sorted(
            p.name.removesuffix(".config.toml") for p in codex_home().glob("*.config.toml") if _codex_stamp(p)
        )
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentihooks profile")
    commands = parser.add_subparsers(dest="command", required=True)
    render_cmd = commands.add_parser("render", help="Render a profile into its own home for one harness")
    render_cmd.add_argument("name")
    render_cmd.add_argument("--target", choices=("claude", "codex", "copilot"), default="claude")
    render_cmd.add_argument("--force", action="store_true")
    from scripts.profiles import measure

    measure.add_arguments(commands.add_parser("measure", help="Print a profile's first turn input tokens"))
    args = parser.parse_args(argv)
    if args.command == "measure":
        return measure.main(args)
    if args.target == "copilot":
        print("copilot per-run profiles are not supported", file=sys.stderr)
        return 2
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
