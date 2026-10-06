from __future__ import annotations

from hooks.context.profile_chain import PACKAGE_PREFIX

MATTPOCOCK = "mattpocock-skills@claude-plugins-official"
PLAYWRIGHT = "playwright@claude-plugins-official"
ROLE_PLUGINS: dict[str, tuple[str, ...]] = {
    "engineer": (MATTPOCOCK,),
    "cicd": (MATTPOCOCK,),
    "planner": (MATTPOCOCK,),
    "master": (PLAYWRIGHT,),
}


def role_defaults(chain: list[str]) -> dict[str, bool]:
    defaults: dict[str, bool] = {}
    for name in chain:
        defaults.update(dict.fromkeys(ROLE_PLUGINS.get(name.removeprefix(PACKAGE_PREFIX), ()), True))
    return defaults


def allowed(chain: list[str], layered: dict) -> dict[str, bool]:
    return {plugin: True for plugin, on in {**role_defaults(chain), **layered}.items() if on}


def claude_only(name: str) -> bool:
    """True when a profile of the chain enables a plugin in its own Claude layer: Codex has no plugins."""
    from scripts.targets._common import _install_module

    _i = _install_module()
    for _, root in _i._resolve_profile_chain(name):
        path = _i._native_layer_path(root, "claude", _i._NATIVE_SETTINGS_NAME)
        if path and any((_i._load_native_layer(path).get("enabledPlugins") or {}).values()):
            return True
    return False
