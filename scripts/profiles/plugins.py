from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path

from hooks.context.profile_chain import PACKAGE_PREFIX, PACKAGE_ROLES

MATTPOCOCK = "mattpocock-skills@claude-plugins-official"
PLAYWRIGHT = "playwright@claude-plugins-official"
FRONTEND_DESIGN = "frontend-design@claude-plugins-official"
IMPECCABLE = "impeccable@impeccable"
ROLE_PLUGINS: dict[str, tuple[str, ...]] = {
    "engineer": (MATTPOCOCK,),
    "cicd": (MATTPOCOCK,),
    "planner": (MATTPOCOCK,),
    "master": (PLAYWRIGHT,),
}
CODEX_SKILLS: dict[str, tuple[str, ...]] = {FRONTEND_DESIGN: ("frontend-design",), IMPECCABLE: ("impeccable",)}
CODEX_PLUGIN_SKILLS = frozenset((MATTPOCOCK,))


def role_defaults(chain: list[str]) -> dict[str, bool]:
    defaults: dict[str, bool] = {}
    for name in chain:
        defaults.update(dict.fromkeys(ROLE_PLUGINS.get(name.removeprefix(PACKAGE_PREFIX), ()), True))
    return defaults


def role_home(chain: list[str]) -> bool:
    return any((PACKAGE_ROLES / name.removeprefix(PACKAGE_PREFIX)).is_dir() for name in chain)


def carried(chain: list[str], operator: dict) -> dict[str, bool]:
    own = {} if role_home(chain) else {plugin: True for plugin, on in operator.items() if on}
    return {**own, **role_defaults(chain)}


def allowed(base: dict, layered: dict, installed: Iterable[str] = ()) -> dict[str, bool]:
    return {plugin: True for plugin, on in {**dict.fromkeys(installed, True), **base, **layered}.items() if on}


def kept(home: dict, written: dict) -> list[str]:
    return [plugin for plugin, on in home.items() if on and plugin not in written]


def fetched() -> Path:
    return Path.home() / ".agentihooks" / "codex-skills"


def _skill_dirs(folder: Path) -> dict[str, Path]:
    return {p.name: p for p in sorted(folder.iterdir()) if (p / "SKILL.md").is_file()} if folder.is_dir() else {}


def layer_skills(roots: Iterable[Path]) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for root in roots:
        found.update(_skill_dirs(root / ".codex" / "skills"))
    return found


def _installed(plugin: str, plugins_dir: Path) -> dict[str, Path]:
    try:
        installs = json.loads((plugins_dir / "installed_plugins.json").read_text())["plugins"][plugin]
    except (OSError, ValueError, KeyError):
        return {}
    for install in sorted(installs, key=lambda entry: entry.get("scope") != "user"):
        if not install.get("installPath"):
            continue
        root = Path(install["installPath"])
        try:
            listed = json.loads((root / ".claude-plugin" / "plugin.json").read_text()).get("skills")
        except (OSError, ValueError):
            continue
        if isinstance(listed, list):
            found = {(root / entry).name: root / entry for entry in listed if (root / entry / "SKILL.md").is_file()}
        else:
            found = _skill_dirs(root / (listed or "skills"))
        if found:
            return found
    return {}


def plugin_skills(enabled: Iterable[str], plugins_dir: Path) -> dict[str, Path]:
    enabled = list(enabled)
    names = {name for plugin in enabled for name in CODEX_SKILLS.get(plugin, ())}
    found = {name: path for name, path in _skill_dirs(fetched()).items() if name in names}
    for plugin in enabled:
        if plugin in CODEX_PLUGIN_SKILLS:
            found.update(_installed(plugin, plugins_dir))
    return found


def replaced(plugin: str, chain: list[str], available: dict[str, Path], plugins_dir: Path) -> bool:
    from scripts.profiles import browser

    if plugin == PLAYWRIGHT:
        return browser.enabled(chain)
    if plugin in CODEX_PLUGIN_SKILLS:
        return bool(_installed(plugin, plugins_dir))
    return plugin in CODEX_SKILLS and all(name in available for name in CODEX_SKILLS[plugin])


def claude_only(name: str) -> bool:
    """True when a plugin a profile of the chain enables in its own Claude layer has no Codex replacement."""
    from scripts.claude_config import claude_home
    from scripts.targets._common import _install_module

    _i = _install_module()
    resolved = _i._resolve_profile_chain(name)
    chain, roots = [n for n, _ in resolved], [root for _, root in resolved]
    plugins_dir = claude_home({k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}) / "plugins"
    bundle = _i._get_bundle_path()
    layer = layer_skills([*([bundle] if bundle else []), *roots])
    for root in roots:
        path = _i._native_layer_path(root, "claude", _i._NATIVE_SETTINGS_NAME)
        layered = (_i._load_native_layer(path).get("enabledPlugins") or {}) if path else {}
        enabled = [plugin for plugin, on in layered.items() if on]
        available = {**plugin_skills(enabled, plugins_dir), **layer}
        if any(not replaced(plugin, chain, available, plugins_dir) for plugin in enabled):
            return True
    return False
