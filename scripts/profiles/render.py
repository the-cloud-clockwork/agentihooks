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
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from hooks.context import profile_chain, quarantine
from scripts.claude_config import claude_home, claude_json
from scripts.profiles import binding, browser, connectors, homes, plugins, sources
from scripts.targets._common import _atomic_write, _install_module, agents_skills_home, build_persona
from scripts.targets.claude_target import settings_document
from scripts.targets.codex_target import codex_home

SHARED = ("projects", "sessions", "todos", "plugins", ".credentials.json")
CODEX_STATE = ("auth.json", "sessions", "history.jsonl", "session_index.jsonl", "hooks.json")
CODEX_INHERITED = ("model", "model_reasoning_effort", "service_tier", "notify", "projects")
COPILOT_STATE = ("config.json", "settings.json", "agentihooks-hook.sh", "hooks", "session-state", "logs")
STAMP = ".agentihooks-render.json"
PLUGINS = ".agentihooks-plugins.json"
KEY_SEPARATOR = "+"
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


def declared(name: str) -> list[str]:
    _i = _install_module()
    found = [o for _, path in _i._resolve_profile_chain(name) for o in profile_chain.overlays(path)]
    return [o for o in dict.fromkeys(found) if _i._resolve_profile_dir(o) is not None]


def _chain(name: str, overlays: Sequence[str] = ()) -> list[tuple[str, Path]]:
    _i = _install_module()
    if _i._resolve_profile_dir(name) is None:
        raise ValueError(f"Profile '{name}' not found")
    always = [name, *declared(name)]
    worn = profile_chain.worn(_i._resolve_profile_chain(",".join(always)), list(overlays), _i._resolve_profile_dir)
    return _i._resolve_profile_chain(",".join([*always, *worn]))


def _bundle() -> Path | None:
    _i = _install_module()
    bundle = _i._get_bundle_path()
    linked = (_i._load_state().get("bundle") or {}).get("path")
    if bundle is None and linked:
        raise ValueError(
            f"linked bundle {linked} is missing; relink it with agentihooks bundle link <path> before rendering"
        )
    return bundle


def _pin(bundle: Path | None, revision: str) -> None:
    recorded = f"the launch recorded bundle commit {revision}, but"
    if bundle is None:
        raise ValueError(f"{recorded} no bundle is linked")
    git = ["git", "-C", str(bundle)]
    try:
        head = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
        status = subprocess.run([*git, "status", "--porcelain"], capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"{recorded} git did not answer within 10 seconds for the bundle at {bundle}") from exc
    for done in (head, status):
        if done.returncode:
            raise ValueError(f"{recorded} git cannot read the bundle at {bundle}: {done.stderr.strip()}")
    if head.stdout.strip() != revision:
        differs = f"is at commit {head.stdout.strip()}"
    elif status.stdout.strip():
        differs = "has uncommitted changes"
    else:
        return
    raise ValueError(
        f"{recorded} the bundle at {bundle} {differs}; check out {revision} in the bundle before this launch renders"
    )


def _base_digest() -> str:
    _i = _install_module()
    digest = hashlib.sha256()
    for name in sorted(_i._NATIVE_BASE_NAME.values()):
        digest.update((_i.PROFILES_DIR / "_base" / name).read_bytes())
    return digest.hexdigest()


def _overlays(dirs: list[tuple[str, Path]]) -> list[str]:
    declared_names = {o for _, path in dirs for o in profile_chain.overlays(path)}
    return [n for n, path in dirs if n in declared_names or profile_chain.wears(path)]


def _profiles_digest(dirs: list[tuple[str, Path]]) -> str:
    digest = hashlib.sha256()
    for root in [_install_module().PACKAGE_FEATURES_DIR, *(d for _, d in dirs)]:
        for path in sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
            digest.update(f"{path}\0".encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _stamp(bundle: Path | None, dirs: list[tuple[str, Path]]) -> dict:
    commit = ""
    if bundle is not None:
        head = subprocess.run(["git", "-C", str(bundle), "rev-parse", "HEAD"], capture_output=True, text=True)
        commit = head.stdout.strip()
    chain = [n for n, _ in dirs]
    return {
        "bundle_commit": commit,
        "base": _base_digest(),
        "profiles": _profiles_digest(dirs),
        "chain": chain,
        "overlays": _overlays(dirs),
        "enabled_plugins": _plugins(chain, _settings("claude", bundle, dirs).get("enabledPlugins") or {}),
        **({"browser": browser.spec()} if browser.enabled(chain) else {}),
        "corrections": quarantine.digest(),
    }


def stamp(name: str) -> dict:
    return _stamp(_bundle(), _chain(name))


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


def _claude_settings(bundle: Path | None, dirs: list[tuple[str, Path]], kept: Sequence[str] = ()) -> dict:
    from scripts.profile_telemetry import apply_collector_env, apply_langfuse_env

    _i = _install_module()
    doc = _settings("claude", bundle, dirs)
    env = doc["env"]
    subscribed = _channels(env.get(CHANNELS, ""), dirs)
    if subscribed:
        env[CHANNELS] = subscribed
    apply_langfuse_env(doc, dirs, None)
    apply_collector_env(doc)
    default_home = claude_home(_global_env())
    global_settings = default_home / "settings.json"
    personal = _i.load_json(global_settings) if global_settings.exists() else {}
    # Claude reads the default home as an ancestor project folder when the working folder sits under it.
    excludes = [str(default_home / "CLAUDE.md"), str(default_home / "rules" / "**")]
    settings = settings_document(doc)
    chain = [n for n, _ in dirs]
    enabled_plugins = _plugins(chain, settings.get("enabledPlugins") or {}, kept)
    if browser.enabled(chain):
        enabled_plugins.pop(plugins.PLAYWRIGHT, None)
    return {
        **{k: personal[k] for k in _i.PERSONAL_KEYS if k in personal},
        **settings,
        "enabledPlugins": enabled_plugins,
        "claudeMdExcludes": excludes,
    }


def _plugins(chain: list[str], layered: dict, kept: Sequence[str] = ()) -> dict[str, bool]:
    operator = _read_json(claude_home(_global_env()) / "settings.json") or {}
    return plugins.allowed(plugins.carried(chain, operator.get("enabledPlugins") or {}), layered, kept)


def _home_plugins(prior: Path | None, computed: dict) -> list[str]:
    if prior is None:
        return []
    home = (_read_json(prior / "claude" / "settings.json") or {}).get("enabledPlugins") or {}
    written = _read_json(prior / "claude" / PLUGINS)
    return plugins.kept(home, computed if written is None else written)


def _channels(channels: str, dirs: list[tuple[str, Path]]) -> str:
    names = [name.strip() for name in channels.split(",") if name.strip()]
    if BRAIN in _overlays(dirs) and BRAIN not in names:
        names.append(BRAIN)
    return ",".join(names)


def channels(name: str) -> str:
    dirs = _chain(name)
    return _channels(_settings("claude", _bundle(), dirs)["env"].get(CHANNELS, ""), dirs)


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


def _persona(root: Path, target: str, bundle: Path | None, dirs: list[tuple[str, Path]], chain: list[str]) -> str:
    items = _features("rules", _is_doc, bundle, dirs)
    sources.write(sources.path(root.name, target, root.parent), sources.rows(bundle, dirs, items))
    rules = [("rule", n, quarantine.annotate(p.read_text(), sources.source(p))) for n, p in items.items()]
    text = quarantine.passages(build_persona(dirs, chain, bundle, rules, HEADER, FOOTER))
    ending = f"\n\n{FOOTER}\n"
    return binding.persona(text.removesuffix(ending)) + ending


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def home_key(name: str, overlays: Sequence[str] = ()) -> str:
    names = [name, *sorted(set(overlays))]
    if any(KEY_SEPARATOR in part for part in names):
        raise ValueError(f"a profile or overlay name cannot hold {KEY_SEPARATOR}: {', '.join(names)}")
    return KEY_SEPARATOR.join(names)


def split_key(key: str) -> tuple[str, list[str]]:
    name, *overlays = key.split(KEY_SEPARATOR)
    return name, overlays


def profile_dir(name: str, overlays: Sequence[str] = ()) -> Path | None:
    return homes.current(rendered_root(), home_key(name, overlays))


def owner(home: Path) -> str | None:
    return homes.owner(rendered_root(), home)


def _claude_fresh(root: Path, stamp: dict, required: set[str]) -> bool:
    out = root / "claude"
    mounts = connectors.path(root.name, "claude", root.parent)
    claude_json = _read_json(out / ".claude.json") or {}
    return (
        _read_json(out / STAMP) == stamp
        and not (out / "rules").exists()
        and sources.path(root.name, "claude", root.parent).is_file()
        and (out / binding.FILE).is_file()
        and mounts.is_file()
        and all((_read_json(mounts) or {}).get(server, {}).get("mounted") for server in required)
        and required <= claude_json.get("mcpServers", {}).keys()
        and "hasCompletedOnboarding" in claude_json
    )


def render_claude(name: str, force: bool = False, overlays: Sequence[str] = ()) -> Path | None:
    _refuse_live_render_from_another_checkout(name)
    _i = _install_module()
    _i._load_claude_runtime_env()
    bundle, dirs = _bundle(), _chain(name, overlays)
    key = home_key(name, overlays)
    current = _stamp(bundle, dirs)
    declared = _mcp_servers("claude", bundle, dirs)
    connectors.require_environment(declared)
    required = {server for server, spec in declared.items() if spec.get("enabled_tools") is not None}
    prior = profile_dir(name, overlays)
    if not force and prior is not None and _claude_fresh(prior, current, required):
        return None
    kept = _home_plugins(prior, current["enabled_plugins"])
    root = homes.fresh(rendered_root(), key, current)
    out = root / "claude"
    out.mkdir()
    if prior is not None and (prior / "claude" / ".claude.json").is_file():
        shutil.copy2(prior / "claude" / ".claude.json", out / ".claude.json")
    servers, deny, mounts = connectors.claude(declared, str(out / ".claude.json"))
    connectors.write(connectors.path(root.name, "claude", root.parent), mounts, name, "claude")
    settings = _claude_settings(bundle, dirs, kept)
    if deny:
        permissions = settings["permissions"]
        permissions["deny"] = [*permissions.get("deny", []), *deny]
    _i.save_json(out / "settings.json", settings)
    _i.save_json(out / PLUGINS, current["enabled_plugins"])
    for subdir, keep in FEATURES:
        _relink(out / subdir, _features(subdir, keep, bundle, dirs))
    _atomic_write(out / "CLAUDE.md", _persona(root, "claude", bundle, dirs, current["chain"]))
    _claude_json(out, servers)
    shared = claude_home(_global_env())
    for item in SHARED:
        (out / item).symlink_to(shared / item)
    # Claude Code refuses plan file writes that resolve through a symlink.
    (out / "plans").mkdir()
    if all(mounts[server]["mounted"] for server in required):
        _i.save_json(out / STAMP, current)
    binding.write(out, name, "claude")
    homes.promote(rendered_root(), key, root)
    return out


def _codex_stamp(path: Path) -> dict | None:
    try:
        return tomllib.loads(path.read_text()).get("agentihooks", {}).get("render")
    except (OSError, ValueError):
        return None


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


def render_codex(name: str, force: bool = False, overlays: Sequence[str] = ()) -> Path | None:
    import tomlkit

    from scripts.profiles import codex_master

    _i = _install_module()
    bundle, dirs = _bundle(), _chain(name, overlays)
    master = any(n.removeprefix("package:") == "master" for n, _ in dirs)
    claude_fresh = render_claude(name, force=force, overlays=overlays) is None
    operator = _operator_codex_home()
    config = operator / "config.toml"
    text = config.read_text() if config.exists() else ""
    installed = tomllib.loads(text)
    current = {"render": _stamp(bundle, dirs), "operator": hashlib.sha256(text.encode()).hexdigest()}
    root = profile_dir(name, overlays)
    out = root / "codex"
    if (
        not force
        and claude_fresh
        and sources.path(root.name, "codex", root.parent).is_file()
        and (out / binding.FILE).is_file()
        and connectors.path(root.name, "codex", root.parent).is_file()
        and _read_json(out / STAMP) == current
        and (not master or ((out / "AGENTS.md").is_file() and not (out / "AGENTS.md").is_symlink()))
    ):
        return None
    if out.exists():
        root = render_claude(name, force=True, overlays=overlays).parent
        out = root / "codex"
    manifest = sources.path(root.name, "codex", root.parent)
    claude = root / "claude"
    out.mkdir()
    if master:
        agents = out / "AGENTS.md"
        if agents.is_symlink():
            agents.unlink()
        _atomic_write(agents, codex_master.persona((claude / "CLAUDE.md").read_text()))
    else:
        _link(out / "AGENTS.md", claude / "CLAUDE.md")
    _relink(out / "skills", {p.name: p for p in sorted((claude / "skills").iterdir())})
    _link_commands(out / "skills", claude / "commands")
    for item in CODEX_STATE:
        _link(out / item, operator / item)
    _link(manifest, sources.path(root.name, "claude", root.parent))
    doc = _codex_config(installed, operator, out, _settings("codex", bundle, dirs))
    doc["project_doc_max_bytes"] = max(65536, int(len((claude / "CLAUDE.md").read_bytes()) * 1.25))
    servers, mounts = connectors.codex(_mcp_servers("codex", bundle, dirs))
    connectors.write(connectors.path(root.name, "codex", root.parent), mounts, name, "codex")
    if servers:
        doc["mcp_servers"] = servers
    root = agents_skills_home()
    hidden_skills = [p for p in sorted(root.iterdir()) if p.is_dir()] if root.is_dir() else []
    if hidden_skills:
        doc["skills"] = {"config": [{"path": str(p / "SKILL.md"), "enabled": False} for p in hidden_skills]}
    _atomic_write(out / "config.toml", tomlkit.dumps(doc))
    _i.save_json(out / STAMP, current)
    binding.write(out, name, "codex")
    legacy = operator / f"{name}.config.toml"
    if _codex_stamp(legacy):
        legacy.unlink()
    return out


def render_copilot(name: str, force: bool = False, overlays: Sequence[str] = ()) -> Path | None:
    from scripts.targets.copilot_target import CopilotAdapter, copilot_home

    bundle, dirs = _bundle(), _chain(name, overlays)
    claude_fresh = render_claude(name, force=force, overlays=overlays) is None
    current = _stamp(bundle, dirs)
    config = {"mcpServers": CopilotAdapter().mcp_entries(_mcp_servers("copilot", bundle, dirs))}
    root = profile_dir(name, overlays)
    out = root / "copilot"
    if (
        not force
        and claude_fresh
        and _read_json(out / "mcp-config.json") == config
        and _read_json(out / STAMP) == current
    ):
        return None
    if out.exists():
        root = render_claude(name, force=True, overlays=overlays).parent
        out = root / "copilot"
    out.mkdir()
    _link(out / "copilot-instructions.md", root / "claude" / "CLAUDE.md")
    operator = copilot_home()
    for item in COPILOT_STATE:
        _link(out / item, operator / item)
    # Copilot sends header values literally, so this file holds the resolved gateway credential.
    with os.fdopen(os.open(out / "mcp-config.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
        f.write(json.dumps(config, indent=2) + "\n")
    _install_module().save_json(out / STAMP, current)
    return out


def render(
    target: str, name: str, force: bool = False, overlays: Sequence[str] = (), bundle_revision: str = ""
) -> Path | None:
    renderers = {"claude": render_claude, "codex": render_codex, "copilot": render_copilot}
    if target not in renderers:
        raise ValueError(f"{target} per-run profiles are not supported")
    if bundle_revision:
        _pin(_bundle(), bundle_revision)
    return renderers[target](name, force=force, overlays=overlays)


def rendered_profiles(target: str) -> list[str]:
    if target == "claude":
        return sorted(home.parent.name for home in rendered_root().glob("*/claude"))
    if target == "codex":
        homes = {config.parent.parent.name for config in rendered_root().glob("*/codex/config.toml")}
        legacy = _operator_codex_home().glob("*.config.toml")
        return sorted(homes | {p.name.removesuffix(".config.toml") for p in legacy if _codex_stamp(p)})
    return sorted(config.parent.parent.name for config in rendered_root().glob(f"*/{target}/mcp-config.json"))


def _seed_linked_profiles(out: Path) -> None:
    _i = _install_module()
    state_file = out / "state.json"
    state = _i.load_json(state_file) if state_file.exists() else {}
    state["linked_profiles"] = _i._get_linked_profiles()
    _i.save_json(state_file, state)


def _render_scratch(args: argparse.Namespace) -> int:
    bundle = args.bundle or _bundle()
    _seed_linked_profiles(args.out)
    env = {**os.environ, "AGENTIHOOKS_HOME": str(args.out.resolve())}
    if bundle is not None:
        env["AGENTIHOOKS_BUNDLE_PATH"] = str(bundle.resolve())
    argv = [sys.executable, "-m", "scripts.profiles.render", "render", args.name, "--target", args.target]
    # The child resolves the agentihooks home, bundle and corrections store at import, so only a fresh process sees them.
    cwd = Path(__file__).resolve().parents[2]
    argv += [f"--overlay={overlay}" for overlay in args.overlay]
    return subprocess.run([*argv, *(["--force"] if args.force else [])], env=env, cwd=cwd).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentihooks profile")
    commands = parser.add_subparsers(dest="command", required=True)
    render_cmd = commands.add_parser("render", help="Render a profile into its own home for one harness")
    render_cmd.add_argument("name")
    render_cmd.add_argument("--target", choices=("claude", "codex", "copilot"), default="claude")
    render_cmd.add_argument("--force", action="store_true")
    render_cmd.add_argument("--overlay", action="append", default=[], help="Wear this overlay; repeat for up to three")
    render_cmd.add_argument("--out", type=Path, help="Render into this scratch agentihooks home, not the live one")
    render_cmd.add_argument("--bundle", type=Path, help="Bundle for --out (default: the linked bundle)")
    from scripts.profiles import measure

    measure.add_arguments(commands.add_parser("measure", help="Print a profile's first turn input tokens"))
    validate = commands.add_parser("validate", help="Validate the mounted profile through its live harness")
    validate.add_argument("--canary", required=True)
    show = commands.add_parser("binding", help="Print a profile home's binding record, never an environment value")
    show.add_argument("--home", type=Path, help="Profile home to read (default: this session's)")
    args = parser.parse_args(argv)
    if args.command == "validate":
        return binding.main(args.canary)
    if args.command == "binding":
        try:
            print(json.dumps(binding.record(args.home)))
        except (OSError, ValueError, KeyError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        return 0
    if args.command == "measure":
        return measure.main(args)
    if args.bundle is not None and args.out is None:
        render_cmd.error("--bundle needs --out")
    try:
        if args.out is not None:
            return _render_scratch(args)
        out = render(args.target, args.name, force=args.force, overlays=args.overlay)
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
