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


def _install_module():
    mod = sys.modules.get("install") or sys.modules.get("scripts.install")
    if mod is None:
        from scripts import install as mod  # production cold path
    return mod


HOOK_RECORD_KEY = "claude_hook_commands"


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


def settings_document(rendered: dict) -> dict:
    _i = _install_module()
    rendered = deepcopy(rendered)
    profile_env = rendered.pop("_agentihooks", {}).get("env", {})
    if profile_env:
        rendered["env"] = {**profile_env, **rendered.get("env", {})}

    # rendered["env"] can carry connector-injected literal values, and this
    # is the one adapter that copies the whole dict into settings verbatim
    # — codex and copilot read only permissions.defaultMode. Scan each env
    # value and drop credential-shaped literals before they reach disk;
    # ${VAR}/$VAR references pass through untouched (claude expands them).
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


def enabled_plugins(operator: dict, layered: dict, bundle: Path | None) -> dict:
    from scripts.deps_preflight import fleet_plugins

    return {**operator, **dict.fromkeys(fleet_plugins(bundle), True), **layered}


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
    from scripts.profiles.render import owner, render_claude

    if not dry_run:
        name = owner(rules_dir.parent)
        if name:
            claude_md = render_claude(name, force=True) / "CLAUDE.md"
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


class ClaudeAdapter:
    name = "claude"

    def home(self) -> Path:
        return _install_module().CLAUDE_HOME

    def write_settings(self, rendered: dict) -> Path:
        _i = _install_module()
        rendered = settings_document(rendered)

        existing_settings_path = _i.CLAUDE_HOME / "settings.json"
        personal = _i._preserve_personal_keys(existing_settings_path)
        merged: dict = deepcopy(personal)
        merged.update(rendered)
        existing = _i.load_json(existing_settings_path) if existing_settings_path.exists() else {}
        merged["enabledPlugins"] = enabled_plugins(
            existing.get("enabledPlugins") or {}, rendered.get("enabledPlugins") or {}, _i._get_bundle_path()
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
    ) -> None:
        _install_module()._install_claude_persona(profile_dirs, profile_chain, bundle_dir)

    def register_hooks_utils(self, profile_name: str) -> None:
        _install_module()._install_user_mcp(profile_name)

    def register_mcp(self, servers: dict) -> None:
        _install_module()._merge_mcp_to_user_scope(servers)

    def teardown(self) -> None:
        # Claude's removal path predates the adapter seam and stays in
        # uninstall_global (settings restore, symlink sweep, CLAUDE.md restore,
        # ~/.claude.json MCP removal). Nothing extra to do here.
        return

    def post_install_reconcile(self, profile_chain: list[str], persisted_profile: str) -> None:
        _i = _install_module()

        # Guard: only prune when the FULL intended profile chain resolved this
        # run — a transiently-missing profile source would otherwise shrink the
        # managed set and falsely delete that profile's servers.
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
