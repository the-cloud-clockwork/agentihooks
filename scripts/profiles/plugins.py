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
