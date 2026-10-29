import sys
from collections.abc import Callable
from pathlib import Path

from scripts.targets._common import _install_module


def native_settings(
    install_target: str,
    profile_dirs: list[tuple[str, Path]],
    settings_profile: tuple[str, Path] | None,
    say: Callable[[str], None] | None = None,
) -> dict:
    _i = _install_module()
    say = say or _i._cprint
    raw_settings = _i._load_native_layer(_i.PROFILES_DIR / "_base" / _i._NATIVE_BASE_NAME[install_target])
    rendered: dict = _i.substitute_paths(raw_settings)
    rendered = _i.substitute_paths(rendered, "__PYTHON__", str(_i._detect_venv() or sys.executable))

    # --- 1a. Bundle-global native overrides: root, then user ---
    # Settings gain the bundle rung that MCP already had; without it a bundle
    # can only express fleet-wide settings by repeating them in every profile.
    for kind, bundle_root in _i._bundle_roots():
        label = "Applied bundle overrides: {path}" if kind == "root" else "Applied user bundle overrides: {path}"
        rendered = _apply_layer(rendered, install_target, bundle_root, label, say)

    # --- 1b. Apply per-profile overrides (chained: each profile merges on top) ---
    for _, pdir in profile_dirs:
        rendered = _apply_layer(rendered, install_target, pdir, "Applied profile overrides: {path}", say)

    # --- 1b2. Apply settings-profile overlay (settings.json + MCP only, not rules/CLAUDE.md) ---
    settings_profile_dir = settings_profile[1] if settings_profile else None
    if settings_profile:
        name = settings_profile[0]
        label = f"Applied settings-profile overlay: {name} ({_i._profile_source_label(name)})"
        rendered = _apply_layer(rendered, install_target, settings_profile_dir, label, say)

    # --- 1c. Apply linked connectors (from last profile in chain) ---
    _apply_connectors(rendered, profile_dirs[-1][0], say)

    from scripts.profile_telemetry import apply_collector_env, apply_langfuse_env

    apply_langfuse_env(rendered, profile_dirs, settings_profile_dir)
    apply_collector_env(rendered)
    return rendered


def _apply_layer(rendered: dict, install_target: str, root: Path, label: str, say) -> dict:
    _i = _install_module()
    path = _i._native_layer_path(root, install_target, _i._NATIVE_SETTINGS_NAME)
    if not path:
        return rendered
    layer = _i._load_native_layer(path)
    # Resolve relative paths in hook commands against the layer's root
    # (claude only — codex/copilot hooks are adapter-written, never
    # declared in their native settings files).
    if install_target == "claude":
        layer = _i._resolve_profile_hook_paths(layer, root)
    say(label.replace("{path}", str(path)))
    return _i._deep_merge(rendered, layer)


def _apply_connectors(rendered: dict, profile_name: str, say) -> None:
    conn_env, conn_deny, conn_disabled = _install_module()._load_connectors(profile_name)
    if conn_env:
        rendered.setdefault("env", {}).update(conn_env)
        say(f"  [OK] Connector env: {list(conn_env.keys())}")
    if conn_deny:
        rendered.setdefault("permissions", {}).setdefault("deny", []).extend(conn_deny)
        say(f"  [OK] Connector deny rules: {len(conn_deny)}")
    if conn_disabled:
        existing_disabled = rendered.get("disabledMcpjsonServers", [])
        merged_disabled = list(dict.fromkeys(existing_disabled + conn_disabled))
        rendered["disabledMcpjsonServers"] = merged_disabled
        say(f"  [OK] Connector disabled MCP servers: {merged_disabled}")
