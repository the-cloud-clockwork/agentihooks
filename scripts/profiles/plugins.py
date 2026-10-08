from __future__ import annotations

from collections.abc import Iterable

from hooks.context.profile_chain import PACKAGE_PREFIX, PACKAGE_ROLES

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


def role_home(chain: list[str]) -> bool:
    return any((PACKAGE_ROLES / name.removeprefix(PACKAGE_PREFIX)).is_dir() for name in chain)


def carried(chain: list[str], operator: dict) -> dict[str, bool]:
    """Role defaults, plus the operator's user scope plugins in a home no swarm role wears."""
    own = {} if role_home(chain) else {plugin: True for plugin, on in operator.items() if on}
    return {**own, **role_defaults(chain)}


def allowed(carried: dict, layered: dict, kept: Iterable[str] = ()) -> dict[str, bool]:
    return {plugin: True for plugin, on in {**dict.fromkeys(kept, True), **carried, **layered}.items() if on}


def kept(home: dict, written: dict) -> list[str]:
    """Plugins enabled inside a home that its last render did not write."""
    return [plugin for plugin, on in home.items() if on and plugin not in written]


def claude_only(name: str) -> bool:
    """True when a profile of the chain enables a plugin in its own Claude layer: Codex has no plugins."""
    from scripts.targets._common import _install_module

    _i = _install_module()
    for _, root in _i._resolve_profile_chain(name):
        path = _i._native_layer_path(root, "claude", _i._NATIVE_SETTINGS_NAME)
        if path and any((_i._load_native_layer(path).get("enabledPlugins") or {}).values()):
            return True
    return False
