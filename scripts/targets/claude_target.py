"""Claude Code target adapter.

Every method delegates to the existing helpers in ``scripts.install`` — the
bodies are the pre-refactor code paths, moved behind the adapter seam so the
install flow is target-neutral. The regression bar for this file is
byte-identical ``~/.claude`` output versus the pre-seam installer.

Imports of the installer module are lazy (inside methods) because install.py
imports this package at module load. The installer has two live identities —
``install`` (test suite, ``scripts/`` on sys.path) and ``scripts.install``
(console entry point) — and the adapter must bind to whichever object the
process is actually running, or the test suite's home-isolation patches on
``install`` would be bypassed and tests would write to the real home.
"""

from __future__ import annotations

import json
import re
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

_JSONDict = dict[str, Any]


def _install_module() -> Any:
    mod = sys.modules.get("install") or sys.modules.get("scripts.install")
    if mod is None:
        from scripts import install as mod  # production cold path
    return mod


HOOK_RECORD_KEY = "claude_hook_commands"


SETTINGS_RECORD_KEY = "claude_settings_managed"

PLUGIN_RECORD_KEY = "claude_enabled_plugins"


def _hook_commands(hooks: dict) -> set[str]:
    return {
        h.get("command", "")
        for groups in hooks.values()
        if isinstance(groups, list)
        for g in groups
        if isinstance(g, dict)
        for h in g.get("hooks", [])
        if isinstance(h, dict) and h.get("command")
    }


def _keep_foreign_hooks(settings_path: Path, rendered: dict) -> dict:
    """Rendered hooks plus every existing hook group agentihooks did not write."""
    _i = _install_module()
    try:
        existing = json.loads(settings_path.read_text()).get("hooks") or {}
    except (OSError, ValueError, AttributeError):
        existing = {}
    state = _i._load_state()
    owned = _hook_commands(rendered) | set(state.get(HOOK_RECORD_KEY, []))
    roots = [re.compile(re.escape(str(r)) + r"(?=[/\s'\"]|$)") for r in _i._managed_roots()]

    def ours(command: str) -> bool:
        return command in owned or any(r.search(command) for r in roots)

    merged = deepcopy(rendered)
    if not isinstance(existing, dict):
        existing = {}
    for event, groups in existing.items():
        if not isinstance(groups, list):
            continue
        foreign = [
            g
            for g in groups
            if isinstance(g, dict)
            and not any(ours(h.get("command", "")) for h in g.get("hooks", []) if isinstance(h, dict))
        ]
        if foreign:
            merged[event] = merged.get(event, []) + foreign
    state[HOOK_RECORD_KEY] = sorted(_hook_commands(rendered))
    _i._save_state(state)
    return merged


def _parent_dict(doc: _JSONDict, path: list[str]) -> _JSONDict | None:
    current: Any = doc
    for key in path:
        current = current.get(key) if isinstance(current, dict) else None
        if not isinstance(current, dict):
            return None
    return current


def _pop_path(doc: _JSONDict, path: list[str]) -> None:
    parent = _parent_dict(doc, path[:-1])
    if parent is not None:
        parent.pop(path[-1], None)


def _merge_tree(
    existing: _JSONDict,
    rendered: _JSONDict,
    prior_record: _JSONDict,
    path: tuple[str, ...],
    new_leaves: set[str],
    new_lists: dict[str, list[Any]],
) -> _JSONDict:
    merged: _JSONDict = dict(existing) if isinstance(existing, dict) else {}
    for key, value in rendered.items():
        new_path = path + (key,)
        dotted = ".".join(new_path)
        current = merged.get(key)
        if isinstance(value, dict):
            merged[key] = _merge_tree(
                current if isinstance(current, dict) else {}, value, prior_record, new_path, new_leaves, new_lists
            )
            continue
        if isinstance(value, list):
            prior_list: list[Any] = prior_record.get("lists", {}).get(dotted, [])
            stale = [item for item in prior_list if item not in value]
            base: list[Any] = [item for item in current if item not in stale] if isinstance(current, list) else []
            for item in value:
                if item not in base:
                    base.append(item)
            merged[key] = base
            new_lists[dotted] = list(value)
            continue
        merged[key] = value
        new_leaves.add(dotted)
    return merged


def _additive_merge(existing: _JSONDict, rendered: _JSONDict, prior_record: _JSONDict) -> tuple[_JSONDict, _JSONDict]:
    """Merge rendered into existing, keeping every key agentihooks does not render.

    A dict value recurses, a list value unions with existing order first and
    the rendered entries appended, and a scalar leaf is overwritten. A path
    or list entry prior_record names but rendered no longer carries is
    removed; a path never recorded here is never touched.
    """
    new_leaves: set[str] = set()
    new_lists: dict[str, list[Any]] = {}
    merged = _merge_tree(existing, rendered, prior_record, (), new_leaves, new_lists)

    for dotted in set(prior_record.get("leaves", [])) - new_leaves:
        _pop_path(merged, dotted.split("."))
    for dotted in set(prior_record.get("lists", {})) - set(new_lists):
        _pop_path(merged, dotted.split("."))

    return merged, {"leaves": sorted(new_leaves), "lists": new_lists}


def _remove_recorded_settings(doc: _JSONDict, record: _JSONDict) -> None:
    for dotted in record.get("leaves", []):
        _pop_path(doc, dotted.split("."))
    for dotted, written in record.get("lists", {}).items():
        path = dotted.split(".")
        parent = _parent_dict(doc, path[:-1])
        if isinstance(parent, dict) and isinstance(parent.get(path[-1]), list):
            parent[path[-1]] = [item for item in parent[path[-1]] if item not in written]


def _remove_owned_hooks(doc: _JSONDict, owned: set[str]) -> None:
    hooks = doc.get("hooks")
    if not isinstance(hooks, dict):
        return
    for event in list(hooks):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept = [
            g
            for g in groups
            if isinstance(g, dict)
            and not any(isinstance(h, dict) and h.get("command") in owned for h in g.get("hooks", []))
        ]
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        doc.pop("hooks", None)


def _remove_owned_plugins(doc: _JSONDict, owned: set[str]) -> None:
    plugins = doc.get("enabledPlugins")
    if not isinstance(plugins, dict):
        return
    for name in owned:
        plugins.pop(name, None)
    if not plugins:
        doc.pop("enabledPlugins", None)


def remove_managed_keys(settings_path: Path) -> bool:
    """Strip only what agentihooks added to a managed settings.json.

    Removes every leaf and list entry recorded under SETTINGS_RECORD_KEY, the
    hook groups recorded under HOOK_RECORD_KEY, the enabledPlugins entries
    recorded under PLUGIN_RECORD_KEY, and the managed-by marker. Every other
    key and entry the operator holds now is left as it is. Returns True if
    the file still carries content afterwards, False if it was removed.
    """
    _i = _install_module()
    if not settings_path.exists():
        return False
    try:
        doc = json.loads(settings_path.read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(doc, dict) or doc.get(_i.MANAGED_BY_KEY) != _i.MANAGED_BY_VALUE:
        return False

    state = _i._load_state()
    _remove_recorded_settings(doc, state.pop(SETTINGS_RECORD_KEY, {}))
    _remove_owned_hooks(doc, set(state.pop(HOOK_RECORD_KEY, [])))
    _remove_owned_plugins(doc, set(state.pop(PLUGIN_RECORD_KEY, [])))
    doc.pop(_i.MANAGED_BY_KEY, None)
    _i._save_state(state)

    if doc:
        _i.save_json(settings_path, doc)
        return True
    settings_path.unlink()
    return False


def settings_document(rendered: dict) -> dict:
    """
    rendered["env"] can carry connector-injected literal values, and this is the one adapter that copies the whole dict
    into settings verbatim — codex and copilot read only permissions.defaultMode. Scan each env value and drop
    credential-shaped literals before they reach disk; ${VAR}/$VAR references pass through untouched (claude expands
    them).
    """
    _i = _install_module()
    rendered = deepcopy(rendered)
    profile_env = rendered.pop("_agentihooks", {}).get("env", {})
    if profile_env:
        rendered["env"] = {**profile_env, **rendered.get("env", {})}

    env_block = rendered.get("env")
    if isinstance(env_block, dict) and env_block:
        from hooks.secrets import scan as _scan_secrets
        from scripts.targets._common import scannable

        clean_env: dict = {}
        for ek, ev in env_block.items():
            hits = _scan_secrets(scannable(str(ev)), mode="strict")
            if hits:
                _i._cprint(
                    f"  [!!] settings env var '{ek}' looks like a credential "
                    f"({', '.join(hits)}) — dropped from settings.json. Export it in "
                    "the shell environment instead of writing it to disk."
                )
                continue
            clean_env[ek] = ev
        rendered = {**rendered, "env": clean_env} if clean_env else {k: v for k, v in rendered.items() if k != "env"}
    return rendered


def enabled_plugins(
    operator: _JSONDict, layered: _JSONDict, bundle: Path | None, user: Path | None = None, shared: Path | None = None
) -> _JSONDict:
    from scripts.deps_preflight import fleet_plugins

    _i = _install_module()
    owned = dict.fromkeys(fleet_plugins(bundle, user, shared), True)
    state = _i._load_state()
    state[PLUGIN_RECORD_KEY] = sorted(set(owned) | set(layered))
    _i._save_state(state)
    return {**operator, **owned, **layered}


def _install_rule_files(dst: Path, sources: list[Path], filter_fn) -> None:
    from scripts.targets._common import _atomic_write

    _i = _install_module()
    items = {}
    for src in sources:
        if src.is_dir():
            items.update(
                {item.name: item for item in src.iterdir() if filter_fn(item) and not item.name.startswith(".")}
            )
    ledger = _i._state_links()
    kept = frozenset(
        name for name in items if str(dst / name) in ledger and (dst / name).is_file() and not (dst / name).is_symlink()
    )
    _i._remove_agentihooks_symlinks(dst, "rule", keep=kept)
    dst.mkdir(exist_ok=True)
    records = []
    for name, src in sorted(items.items()):
        path = dst / name
        text = src.read_text()
        if name in kept:
            if path.read_text() != text:
                _atomic_write(path, text)
        elif path.exists() or path.is_symlink():
            continue
        else:
            _atomic_write(path, text)
        records.append((path, src, "rules"))
    if not records:
        return
    _i._state_record_links(records)
    state = _i._load_state()
    copied = {str(path) for path, _, _ in records}
    for entry in state["managed_links"]:
        if entry["link"] in copied:
            entry["rule_sources"] = [str(src) for src in sources]
    _i._save_state(state)


def refresh_rules(rules_dir: Path, claude_md: Path, local_md: Path, dry_run: bool) -> str:
    from hooks.context.rules_refresh import collect_profile_rules
    from scripts.profiles.render import owner, render_claude, split_key

    if not dry_run:
        name = owner(rules_dir.parent)
        if name:
            profile, overlays = split_key(name)
            claude_md = render_claude(profile, force=True, overlays=overlays) / "CLAUDE.md"
        else:
            _i = _install_module()
            layers = next(
                (
                    entry["rule_sources"]
                    for link, entry in _i._state_links().items()
                    if Path(link).parent == rules_dir and not Path(link).is_symlink() and "rule_sources" in entry
                ),
                None,
            )
            if layers:
                _install_rule_files(
                    rules_dir,
                    [Path(src) for src in layers],
                    lambda path: path.suffix == ".md" and path.name != "README.md",
                )
    return collect_profile_rules(rules_dir, claude_md, local_md)


def refresh_every_home(caller: str) -> None:
    from scripts.profiles import render
    from scripts.targets import SUPPORTED_TARGETS

    for target in SUPPORTED_TARGETS:
        keys = render.rerender(target, skip=[caller] if target == "claude" else [])
        if target == "claude":
            _mark_homes(render.rendered_root(), keys)


def _mark_homes(root: Path, keys: list[str]) -> None:
    from hooks.context.rules_refresh import home_rules, profile_home, write_refresh_marker
    from scripts.profiles import homes

    _i = _install_module()
    for key in keys:
        home = root / homes.HOMES / key / homes.CURRENT / "claude"
        pending = write_refresh_marker(key, home_rules(home), home=profile_home(home))["pending_count"]
        print(f"{_i._GREEN}[OK]{_i._RESET} Refresh marker written for profile '{key}' ({pending} sessions).")


class ClaudeAdapter:
    name = "claude"

    def home(self) -> Path:
        return _install_module().CLAUDE_HOME

    def summary_counts(self) -> dict[str, int]:
        from scripts.init_reporter import installed_counts

        installer = _install_module()
        settings_path = self.home() / "settings.json"
        settings = installer.load_json(settings_path) if settings_path.exists() else {}
        mcp = installer.load_json(installer._CLAUDE_JSON) if installer._CLAUDE_JSON.exists() else {}
        return installed_counts(mcp.get("mcpServers", {}), self.home() / "skills", settings.get("enabledPlugins", {}))

    def write_settings(self, rendered: _JSONDict) -> Path:
        _i = _install_module()
        rendered = settings_document(rendered)

        existing_settings_path = _i.CLAUDE_HOME / "settings.json"
        existing = _i.load_json(existing_settings_path) if existing_settings_path.exists() else {}
        merge_source = {k: v for k, v in rendered.items() if k not in ("hooks", "enabledPlugins")}

        state = _i._load_state()
        merged, record = _additive_merge(existing, merge_source, state.get(SETTINGS_RECORD_KEY, {}))
        state[SETTINGS_RECORD_KEY] = record
        _i._save_state(state)

        merged["enabledPlugins"] = enabled_plugins(
            existing.get("enabledPlugins") or {},
            rendered.get("enabledPlugins") or {},
            _i._get_bundle_path(),
            _i._get_user_bundle_path(),
            _i._get_shared_bundle_path(),
        )
        merged["hooks"] = _keep_foreign_hooks(existing_settings_path, rendered.get("hooks") or {})
        if not merged["hooks"]:
            del merged["hooks"]
        merged[_i.MANAGED_BY_KEY] = _i.MANAGED_BY_VALUE

        _i._backup_settings(existing_settings_path)
        _i.CLAUDE_HOME.mkdir(parents=True, exist_ok=True)
        _i.save_json(existing_settings_path, merged)
        _i._cprint(f"[OK] Wrote {existing_settings_path}")
        return existing_settings_path

    def install_features(self, subdir: str, layers: list[tuple[str, Path]], filter_fn) -> None:
        _i = _install_module()
        dst = _i.CLAUDE_HOME / subdir
        if subdir == "rules":
            _install_rule_files(dst, [src for _, src in layers], filter_fn)
            return
        for label, src in layers:
            _i._symlink_dir_contents(src, dst, label=label, filter_fn=filter_fn)

    def install_persona(
        self,
        profile_dirs: list[tuple[str, Path]],
        profile_chain: list[str],
        bundle_dir: Path | None,
        user_dir: Path | None = None,
        shared_dir: Path | None = None,
    ) -> None:
        _install_module()._install_claude_persona(profile_dirs, profile_chain, bundle_dir, user_dir, shared_dir)

    def register_hooks_utils(self, profile_name: str) -> None:
        _install_module()._install_user_mcp(profile_name)

    def register_mcp(self, servers: dict) -> None:
        _install_module()._merge_mcp_to_user_scope(servers)

    def teardown(self) -> None:
        """
        Claude's removal path predates the adapter seam and stays in uninstall_global (settings key removal via
        remove_managed_keys, symlink sweep, CLAUDE.md restore, ~/.claude.json MCP removal). Nothing extra to do here.
        """
        return

    def post_install_reconcile(self, profile_chain: list[str], persisted_profile: str) -> None:
        """
        Guard: only prune when the FULL intended profile chain resolved this run — a transiently-missing profile source
        would otherwise shrink the managed set and falsely delete that profile's servers.
        """
        _i = _install_module()

        intended_chain = [p.strip() for p in persisted_profile.split(",") if p.strip()]
        missing = [name for name in intended_chain if name not in profile_chain]
        if not missing:
            current_managed = set(_i._collect_all_managed_mcp_servers().keys())
            removed_mcp = _i._reconcile_managed_mcp_ledger(current_managed)
            if removed_mcp:
                _i._cprint(
                    f"  [OK] Removed {len(removed_mcp)} MCP server(s) no longer in any "
                    f"profile/bundle: {', '.join(removed_mcp)}"
                )
        else:
            _i._cprint(
                f"  [--] Skipping MCP ledger reconcile — profile(s) {', '.join(missing)} did not "
                "resolve this run (transient source loss); ledger left unchanged."
            )

        _i._snapshot_claude_json()
