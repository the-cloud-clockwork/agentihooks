"""OpenAI Codex CLI target adapter.

Writes the Codex-shaped install surface (docs/reference/CODEX-COMPAT.md §5):

- ``~/.codex/config.toml`` — managed keys only, via tomlkit round-trip so
  operator hand-edits outside the managed key set survive every re-init
  (the TOML analogue of ``_preserve_personal_keys``).
- ``~/.codex/hooks.json`` + wrapper script — all supported lifecycle events
  routed to ``python -m hooks`` with ``AGENTIHOOKS_TARGET=codex``.
- ``~/.codex/AGENTS.md`` — persona: bundle CLAUDE.md ⊕ profile-chain
  CLAUDE.mds ⊕ compiled rules (codex has no rules dir) ⊕ CI manifesto.
- ``~/.agents/skills/`` — skills symlinks (open agent-skills standard dir).
- ``~/.codex/prompts/`` — never written; prompts an earlier install translated are removed.
- ``[mcp_servers.*]`` — MCP registration (stdio + http url; SSE entries are
  skipped with a warning until the server side exposes streamable HTTP).

Codex facts this file encodes were verified against codex-cli 0.147.0 on
2026-08-10 (docs/reference/CODEX-COMPAT.md §10 evidence table).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import shutil
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from scripts.targets._common import (
    LEGACY_MCP_SERVER_NAMES,
    MCP_SERVER_NAME,
    _atomic_write,
    _command_is_wrapper,
    _install_module,
    agents_skills_home,
    build_persona,
    clear_managed_mcp,
    drop_if_credentialed,
    has_env_reference,
    migrate_legacy_mcp,
    reap_translated_commands,
    record_managed_mcp,
    scannable,
    skill_names_in,
    strip_persona,
    write_persona,
)

_MANAGED_HEADER = "<!-- managed-by: agentihooks — regenerate with: agentihooks init --target codex -->"
_MANAGED_FOOTER = "<!-- agentihooks:managed-end -->"


# Events supported by both codex hooks.json and our hook_manager dispatch.
CODEX_HOOK_EVENTS = (
    "SessionStart",
    "SessionEnd",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Stop",
    "SubagentStart",
    "SubagentStop",
    "PreCompact",
    "PermissionRequest",
)

# Events codex refuses additionalContext on — it warns per handler when the key
# is set on one of them ("this event cannot emit additionalContext").
_NO_CONTEXT_EVENTS = frozenset({"Stop", "SubagentStop", "SessionEnd", "PreCompact", "PermissionRequest"})


_REFERENCE_NAME = re.compile(r"\$\{?(\w+)")


def _forward_env_references(entry: dict) -> None:
    """Codex passes env values literally: resolve ${VAR} in a bash wrapper fed by env_vars."""
    env = entry.get("env") or {}
    refs = {k: str(v) for k, v in env.items() if has_env_reference(str(v))}
    if not refs:
        return
    assigns = " ".join(f'{k}="{v.replace(chr(34), chr(92) + chr(34))}"' for k, v in refs.items())
    entry["args"] = ["-c", f'{assigns} exec "$0" "$@"', entry["command"], *entry.get("args", [])]
    entry["command"] = "bash"
    entry["env_vars"] = sorted({n for v in refs.values() for n in _REFERENCE_NAME.findall(v)})
    literal = {k: v for k, v in env.items() if k not in refs}
    if literal:
        entry["env"] = literal
    else:
        entry.pop("env", None)


def codex_home(environ: Mapping[str, str] | None = None) -> Path:
    """Resolve CODEX_HOME (first entry when the env var is a comma list)."""
    env = os.environ if environ is None else environ
    raw = env.get("CODEX_HOME", "").split(",")[0].strip()
    return Path(raw).expanduser() if raw else Path(env.get("HOME") or Path.home()) / ".codex"


def _stale_catalog_files() -> tuple[Path, Path]:
    from hooks.config import AGENTIHOOKS_HOME

    return AGENTIHOOKS_HOME / "codex_model_catalog.json", AGENTIHOOKS_HOME / "codex_context_highwater.json"


def _is_ours(group: object, wrapper: Path) -> bool:
    hooks = group.get("hooks") if isinstance(group, dict) else None
    return isinstance(hooks, list) and any(
        isinstance(h, dict) and _command_is_wrapper(str(h.get("command")), wrapper) for h in hooks
    )


def restore_hook_order(home: Path) -> list[str]:
    """Events whose agentihooks group drifted behind a foreign one; the hooks file is rewritten only when one did.

    Codex keys hook trust by group position, so this restores the order `init` writes and never touches trust.
    """
    path = (home / "hooks.json").resolve()
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    hooks = doc.get("hooks") if isinstance(doc, dict) else None
    if not isinstance(hooks, dict):
        return []
    wrapper = path.parent / "agentihooks-hook.sh"
    moved = []
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        ordered = sorted(groups, key=lambda g: not _is_ours(g, wrapper))
        if ordered != groups:
            hooks[event] = ordered
            moved.append(event)
    if moved:
        _atomic_write(path, json.dumps(doc, indent=2))
    return moved


_TOOL_FILTERS = ("enabled_tools", "disabled_tools")
_WHOLE_REFERENCE = re.compile(r"\$\{(\w+)\}|\$(\w+)")


def _codex_stdio(name: str, spec: dict) -> dict:
    from hooks.secrets import scan as _scan_secrets

    _i = _install_module()
    entry: dict = {"command": spec["command"]}
    if spec.get("args"):
        entry["args"] = list(spec["args"])
    clean_env: dict = {}
    for ek, ev in dict(spec.get("env") or {}).items():
        ev_s = scannable(str(ev))
        hits = _scan_secrets(ev_s, mode="strict") if ev_s else []
        if hits:
            _i._cprint(
                f"  [!!] MCP '{name}' env var '{ek}' looks like a credential "
                f"({', '.join(hits)}) — dropped from config.toml. Export it in "
                "the shell environment instead of writing it to disk."
            )
            continue
        clean_env[ek] = ev
    if clean_env:
        entry["env"] = clean_env
        _forward_env_references(entry)
    return entry


def _codex_http(name: str, spec: dict) -> dict:
    # Claude expands ${VAR} in header values; codex sends them literally (gateway 401 on the raw placeholder).
    from hooks.secrets import scan as _scan_secrets

    _i = _install_module()
    entry: dict = {"url": spec["url"]}
    clean_headers: dict = {}
    env_headers: dict = {}
    for hk, hv in dict(spec.get("headers") or {}).items():
        hv_s = str(hv)
        bearer = re.fullmatch(r"Bearer\s+\$\{?(\w+)\}?", hv_s)
        whole = _WHOLE_REFERENCE.fullmatch(hv_s)
        if hk.lower() == "authorization" and bearer:
            entry["bearer_token_env_var"] = bearer.group(1)
        elif whole:
            env_headers[hk] = whole.group(1) or whole.group(2)
        elif has_env_reference(hv_s):
            _i._cprint(
                f"  [!!] MCP '{name}' header '{hk}' uses a ${{VAR}}/$VAR reference inside a longer value — "
                "codex does not expand these; header dropped. Make the whole value one ${VAR} "
                "(mapped to env_http_headers) or an Authorization Bearer ${VAR} (mapped to bearer_token_env_var)."
            )
        elif hits := _scan_secrets(hv_s, mode="strict"):
            _i._cprint(
                f"  [!!] MCP '{name}' header '{hk}' looks like a credential "
                f"({', '.join(hits)}) — dropped from config.toml. Reference it via "
                "Authorization Bearer ${VAR} (mapped to bearer_token_env_var) "
                "instead of a literal value."
            )
        else:
            clean_headers[hk] = hv
    if clean_headers:
        entry["http_headers"] = clean_headers
    if env_headers:
        entry["env_http_headers"] = env_headers
    return entry


def codex_mcp_entry(name: str, spec: dict) -> tuple[dict | None, str]:
    """A declared (Claude-shaped) MCP server as its codex entry, or None and why it cannot mount."""
    if drop_if_credentialed(name, spec, "config.toml"):
        return None, "credential-shaped literal in url, command or args"
    if spec.get("type") == "sse":
        _install_module()._cprint(
            f"  [!!] MCP '{name}' uses SSE — codex has no SSE transport; skipped. "
            "Expose a streamable-HTTP endpoint and re-run init."
        )
        return None, "codex has no SSE transport"
    if spec.get("command"):
        entry = _codex_stdio(name, spec)
    elif spec.get("url"):
        entry = _codex_http(name, spec)
    else:
        return None, "no command or url"
    entry.update({key: list(spec[key]) for key in _TOOL_FILTERS if spec.get(key)})
    if spec.get("default_tools_approval_mode"):
        entry["default_tools_approval_mode"] = spec["default_tools_approval_mode"]
    return entry, ""


class CodexAdapter:
    name = "codex"

    def __init__(self) -> None:
        # Rules collected by install_features("rules", ...) and compiled into
        # AGENTS.md by install_persona — the features loop runs first.
        self._pending_rules: list[tuple[str, str, str]] = []  # (layer_label, name, text)

    def home(self) -> Path:
        return codex_home()

    # ------------------------------------------------------------------
    # settings: config.toml managed keys + hooks.json + wrapper
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_managed(target: dict, recorded: dict, key: str, value, label: str) -> None:
        """Write *value* unless the operator hand-edited that key since our last write."""
        _i = _install_module()
        current = target.get(key)
        if current is None or current == value or current == recorded.get(key):
            target[key] = value
            recorded[key] = list(value) if isinstance(value, list) else value
        else:
            _i._cprint(
                f"  [!!] config.toml '{label}' hand-set to {current!r} (managed value would be "
                f"{value!r}) — leaving operator value in place"
            )

    @staticmethod
    def _managed_sidecar(home: Path) -> Path:
        return home / ".agentihooks-managed.json"

    @classmethod
    def _take_managed(cls, home: Path, doc) -> dict:
        """Pop the in-file record earlier releases kept in *doc*; the sidecar record wins over it."""
        legacy = doc.pop("agentihooks", None)
        sidecar = cls._managed_sidecar(home)
        if sidecar.exists():
            with contextlib.suppress(ValueError):
                recorded = json.loads(sidecar.read_text())
                if isinstance(recorded, dict):
                    return recorded
        managed = legacy.get("managed") if isinstance(legacy, dict) else None
        return managed.unwrap() if isinstance(managed, dict) else {}

    def write_settings(self, native: dict) -> Path:
        _i = self._i = _install_module()
        home = self.home()
        home.mkdir(parents=True, exist_ok=True)

        config_path = home / "config.toml"
        doc = self._load_toml(config_path)
        # A pinned catalog limits codex to the models it lists.
        stale = _stale_catalog_files()
        if doc.get("model_catalog_json") == str(stale[0]):
            del doc["model_catalog_json"]

        # Native settings arrive already merged (base + bundle + profile chain).
        # Everything else is authored natively (profiles/_base/config.base.toml
        # plus each profile's .codex/config.overrides.toml) and arrives here
        # already merged. Each key is applied under the managed-key record so a
        # value the operator hand-edited since our last write is left alone.
        #
        # Nested tables (tui, features, …) merge key-by-key rather than being
        # replaced wholesale: config.toml is a shared operator file, and
        # replacing a table would silently drop settings we never wrote.
        managed = self._take_managed(home, doc)
        for key, value in (native or {}).items():
            if key in ("agentihooks", "mcp_servers") or key.startswith("_"):
                continue
            if isinstance(value, dict):
                table = doc.setdefault(key, {})
                recorded_table = managed.setdefault(key, {})
                for sub, subvalue in value.items():
                    self._apply_managed(table, recorded_table, sub, subvalue, f"{key}.{sub}")
                continue
            self._apply_managed(doc, managed, key, value, key)

        # Floor, applied after the merge: the hook layer is the entire guardrail
        # surface, so it is never left off — not by a native file, not by a
        # hand-edit.
        doc.setdefault("features", {})["hooks"] = True

        # Notification degrade: codex has no Notification hook — the fixed
        # `notify` mechanism (agent-turn-complete, JSON as argv[1]) is bridged
        # into the normal Notification handler by the shim module.
        python_bin = str(_i._detect_venv() or sys.executable)
        doc.setdefault("notify", [python_bin, "-m", "hooks.targets.notify_shim"])

        self._dump_toml(config_path, doc)
        for path in stale:
            path.unlink(missing_ok=True)
        _atomic_write(self._managed_sidecar(home), json.dumps(managed, indent=2) + "\n")
        _i._cprint(f"[OK] Wrote managed keys into {config_path}")

        self._write_hooks_json(home, native.get("_agentihooks", {}).get("env", {}))
        return config_path

    def _write_hooks_json(self, home: Path, profile_env: dict[str, str] | None = None) -> None:
        _i = _install_module()
        wrapper = home / "agentihooks-hook.sh"
        python_bin = str(_i._detect_venv() or sys.executable)
        wrapper.write_text(
            "#!/usr/bin/env bash\n"
            "# managed-by: agentihooks — regenerate with: agentihooks init --target codex\n"
            "set -euo pipefail\n"
            f"cd {shlex.quote(str(_i.AGENTIHOOKS_ROOT))}\n"
            + "".join(
                f'export {key}="${{{key}:={shlex.quote(value)}}}"\n' for key, value in (profile_env or {}).items()
            )
            + f"AGENTIHOOKS_TARGET=codex exec {shlex.quote(python_bin)} -m hooks\n"
        )
        wrapper.chmod(0o755)

        hooks_path = home / "hooks.json"

        # additionalContextLimit unset means codex spills any additionalContext
        # over ~2500 tokens to disk and shows the model a preview; 0 disables
        # spilling, which the project-context injection depends on.
        def _entry(event: str) -> dict:
            hook = {"type": "command", "command": str(wrapper)}
            if event not in _NO_CONTEXT_EVENTS:
                hook["additionalContextLimit"] = 0
            return {"hooks": [hook]}

        desired = {e: [_entry(e)] for e in CODEX_HOOK_EVENTS}

        existing: dict = {}
        if hooks_path.exists():
            try:
                existing = json.loads(hooks_path.read_text())
            except (json.JSONDecodeError, OSError):
                backup = hooks_path.with_suffix(f".json.bak.{datetime.now(timezone.utc):%Y%m%d%H%M%S}")
                shutil.copy2(hooks_path, backup)
                _i._cprint(f"  [!!] Unparseable hooks.json backed up → {backup}")
                existing = {}

        # Preserve foreign matchers/hooks per event; replace only entries that
        # point at our wrapper (identified by path).
        merged = existing.get("hooks", {}) if isinstance(existing.get("hooks"), dict) else {}
        for event, groups in desired.items():
            prior = merged.get(event, [])
            foreign = [
                g
                for g in prior
                if not any(_command_is_wrapper(h.get("command", ""), wrapper) for h in g.get("hooks", []))
            ]
            # Ours first: codex keys hook trust by group position and herdr appends its group.
            merged[event] = groups + foreign
        # Reap our own entries under events we no longer wire (e.g. a stale
        # PostCompact from an earlier install) — foreign groups there survive.
        for event in [e for e in merged if e not in desired]:
            foreign = [
                g
                for g in merged[event]
                if not any(_command_is_wrapper(h.get("command", ""), wrapper) for h in g.get("hooks", []))
            ]
            if foreign:
                merged[event] = foreign
            else:
                del merged[event]
        text = json.dumps({"hooks": merged}, indent=2)
        if existing and hooks_path.read_text() == text:
            _i._cprint(f"[OK] {hooks_path} unchanged; existing Codex hook trust holds")
            return
        _atomic_write(hooks_path, text)
        _i._cprint(f"[OK] Wrote {hooks_path} ({len(CODEX_HOOK_EVENTS)} events)")
        _i._cprint(
            "  [!!] Codex trusts hooks by content hash: run /hooks inside codex once to "
            "trust these (or launch automation with --dangerously-bypass-hook-trust). "
            "Until trusted, codex SILENTLY skips them."
        )

    # ------------------------------------------------------------------
    # features: skills / agents / commands / rules
    # ------------------------------------------------------------------

    def install_features(self, subdir: str, layers: list[tuple[str, Path]], filter_fn) -> None:
        _i = _install_module()
        if subdir == "skills":
            dst = agents_skills_home()
            # ~/.agents/skills is shared with the copilot target, which writes
            # translated commands there as real directories. Clear any whose
            # name a real skill now claims — the symlinker refuses to replace a
            # non-symlink, so otherwise this skill silently never installs.
            reap_translated_commands(skill_names_in(layers, filter_fn), reason="a real skill now owns the name")
            for label, src in layers:
                _i._symlink_dir_contents(src, dst, label=f"codex {label}", filter_fn=filter_fn)
        elif subdir == "commands":
            self._reap_prompts()
        elif subdir == "rules":
            # Codex has no auto-loaded rules dir — compile into AGENTS.md.
            collected: dict[str, tuple[str, str, str]] = {}
            for label, src in layers:
                if not src.is_dir():
                    continue
                for f in sorted(src.iterdir()):
                    if filter_fn(f):
                        try:
                            collected[f.name] = (label, f.name, f.read_text())
                        except OSError:
                            pass
            self._pending_rules = list(collected.values())
            _i._cprint(f"  [OK] {len(self._pending_rules)} rule(s) queued for AGENTS.md compilation")
        elif subdir == "agents":
            _i._cprint(
                "  [--] Codex has no custom-subagent registry — agents skipped (docs/reference/CODEX-COMPAT.md §3 row 16)."
            )

    def _reap_prompts(self) -> None:
        """Remove the prompts an earlier install translated; Codex 0.160 no longer reads ~/.codex/prompts."""
        _i = _install_module()
        prompts_dir = self.home() / "prompts"
        manifest_path = prompts_dir / ".agentihooks-manifest.json"
        if manifest_path.exists():
            try:
                for name in json.loads(manifest_path.read_text()):
                    (prompts_dir / name).unlink(missing_ok=True)
            except (json.JSONDecodeError, OSError):
                pass
            manifest_path.unlink()
            _i._cprint(f"  [RM] Removed translated prompts from {prompts_dir}")

    # ------------------------------------------------------------------
    # persona: AGENTS.md
    # ------------------------------------------------------------------

    def install_persona(
        self,
        profile_dirs: list[tuple[str, Path]],
        profile_chain: list[str],
        bundle_dir: Path | None,
    ) -> None:
        _i = _install_module()
        managed_text = build_persona(
            profile_dirs,
            profile_chain,
            bundle_dir,
            self._pending_rules,
            _MANAGED_HEADER,
            _MANAGED_FOOTER,
        )
        dst = self.home() / "AGENTS.md"
        text = write_persona(dst, managed_text, _MANAGED_HEADER, _MANAGED_FOOTER)

        # Codex caps the combined instruction doc (project_doc_max_bytes,
        # default 32 KiB). v0.147.0 loaded a 415 KB global AGENTS.md in full,
        # but the ceiling is raised defensively anyway — a silently truncated
        # persona is the worst failure mode this file can have.
        size = len(text.encode())
        ceiling = max(65536, int(size * 1.25))
        config_path = self.home() / "config.toml"
        doc = self._load_toml(config_path)
        doc["project_doc_max_bytes"] = ceiling
        self._dump_toml(config_path, doc)
        _i._cprint(
            f"[OK] Wrote {dst} ({size} bytes; {len(profile_chain)} profile(s), "
            f"{len(self._pending_rules)} rule(s); project_doc_max_bytes={ceiling})"
        )

    # ------------------------------------------------------------------
    # MCP registration
    # ------------------------------------------------------------------

    def register_hooks_utils(self, profile_name: str) -> None:
        _i = _install_module()
        python_bin = str(_i._detect_venv() or sys.executable)
        # Same transport resolver as the claude path — it reads
        # AGENTIHOOKS_MCP_TRANSPORT and ~/.agentihooks/.env, which a bare
        # os.environ["MCP_TRANSPORT"] read does not, so the two targets could
        # otherwise disagree about whether the daemon is even in use.
        transport = _i._resolve_installer_mcp_transport()
        if transport == "stdio":
            entry: dict = {"command": python_bin, "args": ["-m", "hooks.mcp"]}
        else:
            # Reuse the claude-side builder rather than re-deriving the URL: it
            # validates MCP_PORT and honours MCP_SCHEME (the daemon serves
            # plaintext on its loopback bind, but an operator fronting it with
            # TLS or moving it off loopback needs https) and picks the path per
            # transport. Re-deriving it here is how this adapter shipped a
            # hardcoded http:// that the claude path had already fixed.
            # Codex takes the url alone — it infers the type itself.
            entry = {"url": _i._build_mcp_config("")["mcpServers"][MCP_SERVER_NAME]["url"]}
        config_path = self.home() / "config.toml"
        doc = self._load_toml(config_path)
        table = doc.get("mcp_servers")
        if table is not None and migrate_legacy_mcp(table, self.name, str(config_path), entry.get("url")):
            self._dump_toml(config_path, doc)
        self.register_mcp({MCP_SERVER_NAME: entry})

    def register_mcp(self, servers: dict) -> None:
        """Merge a layer of MCP servers into [mcp_servers.*] in config.toml.

        Claude ``.mcp.json`` entries translate almost 1:1; SSE transports are
        skipped with a warning — codex has no SSE client (docs/reference/CODEX-COMPAT.md §7).
        """
        _i = _install_module()
        config_path = self.home() / "config.toml"
        doc = self._load_toml(config_path)
        table = doc.setdefault("mcp_servers", {})
        added: list[str] = []
        for name, spec in servers.items():
            entry, _ = codex_mcp_entry(name, dict(spec))
            if entry is None:
                continue
            table[name] = entry
            added.append(name)
        self._dump_toml(config_path, doc)
        if added:
            record_managed_mcp(self.name, added)
            _i._cprint(f"  [OK] Codex MCP servers: {', '.join(added)}")

    def teardown(self) -> None:
        _i = _install_module()
        home = self.home()
        wrapper = home / "agentihooks-hook.sh"

        # hooks.json: strip our groups per event; foreign groups survive.
        hooks_path = home / "hooks.json"
        if hooks_path.exists():
            try:
                doc = json.loads(hooks_path.read_text())
            except (json.JSONDecodeError, OSError):
                # Unparseable file may hold operator hooks mid-edit — preserve
                # it rather than treating it as empty and deleting it.
                backup = hooks_path.with_suffix(f".json.bak.{datetime.now(timezone.utc):%Y%m%d%H%M%S}")
                shutil.move(str(hooks_path), str(backup))
                _i._cprint(f"  [!!] {hooks_path.name} unparseable — preserved at {backup.name}")
                doc = None
            merged = doc.get("hooks", {}) if isinstance(doc, dict) and isinstance(doc.get("hooks"), dict) else {}
            foreign_left = {}
            for event, groups in merged.items():
                groups = groups if isinstance(groups, list) else []
                foreign = [
                    g
                    for g in groups
                    if not any(_command_is_wrapper(h.get("command", ""), wrapper) for h in g.get("hooks", []))
                ]
                if foreign:
                    foreign_left[event] = foreign
            if foreign_left:
                _atomic_write(hooks_path, json.dumps({"hooks": foreign_left}, indent=2))
                _i._cprint(f"  [RM] Removed agentihooks entries from {hooks_path} (operator hooks kept)")
            elif doc is not None:
                hooks_path.unlink()
                _i._cprint(f"  [RM] Removed {hooks_path}")
        wrapper.unlink(missing_ok=True)

        # config.toml: keys still holding our recorded value are withdrawn; a
        # hand-edited key is the operator's and stays. `notify` goes only if it
        # still points at our shim. [features].hooks is left as-is — true with
        # no hooks configured is inert, and the operator may run their own.
        config_path = home / "config.toml"
        if config_path.exists():
            doc = self._load_toml(config_path)
            managed = self._take_managed(home, doc)
            removed_keys = []
            for key in list(managed):
                if doc.get(key) == managed.get(key):
                    doc.pop(key, None)
                    removed_keys.append(key)
            if not managed and any(k in doc for k in ("approval_policy", "sandbox_mode")):
                # No record (legacy install / hand-deleted table). These values
                # carry no agentihooks fingerprint, so removing by name could
                # revert a deliberate operator choice — warn instead, loudly:
                # danger-full-access left behind is worth the operator's look.
                _i._cprint(
                    "  [!!] no managed-key record — approval_policy/sandbox_mode "
                    "left as-is; review them (a torn-down bypass install would have set "
                    '"never"/"danger-full-access").'
                )
            self._managed_sidecar(home).unlink(missing_ok=True)
            notify = doc.get("notify")
            if isinstance(notify, list) and any("notify_shim" in str(part) for part in notify):
                doc.pop("notify", None)
            # Ours by construction (install_persona sets it on every run).
            doc.pop("project_doc_max_bytes", None)
            table = doc.get("mcp_servers")
            from scripts.targets._common import managed_mcp_names

            recorded_names = managed_mcp_names(self.name)
            removed = []
            if table is not None:
                for n in recorded_names:
                    if table.pop(n, None) is not None:
                        removed.append(n)
                for own in (MCP_SERVER_NAME, *LEGACY_MCP_SERVER_NAMES) if not recorded_names else ():
                    if own not in table:
                        continue
                    # No record: remove it only when its content proves it is
                    # ours — a name collision with the operator's own server
                    # must not delete their entry.
                    import tomlkit as _tomlkit

                    entry_text = _tomlkit.dumps({"e": table[own]})
                    if "hooks.mcp" in entry_text:
                        table.pop(own)
                        removed.append(own)
                    else:
                        _i._cprint(
                            f"  [!!] '{own}' in config.toml has no install record and "
                            "does not look agentihooks-managed — left in place; review it."
                        )
                if not len(table):
                    doc.pop("mcp_servers", None)
            self._dump_toml(config_path, doc)
            if removed_keys or removed:
                _i._cprint(
                    f"  [RM] Removed from {config_path}: " + ", ".join(removed_keys + [f"mcp:{n}" for n in removed])
                )
        clear_managed_mcp(self.name)

        strip_persona(home / "AGENTS.md", _MANAGED_HEADER, _MANAGED_FOOTER)

        self._reap_prompts()

    def post_install_reconcile(self, profile_chain: list[str], persisted_profile: str) -> None:
        return None

    # ------------------------------------------------------------------
    # doctor
    # ------------------------------------------------------------------

    def doctor(self) -> int:
        """Print codex-install health; return count of failed checks."""
        home = self.home()
        checks: list[tuple[bool, str]] = []

        config_path = home / "config.toml"
        doc = None
        if config_path.exists():
            try:
                import tomlkit

                doc = tomlkit.parse(config_path.read_text())
                checks.append((True, f"config.toml parses ({config_path})"))
            except Exception as exc:
                checks.append((False, f"config.toml unparseable: {exc}"))
        else:
            checks.append((False, "config.toml missing — run: agentihooks init --target codex"))

        if doc is not None:
            checks.append((bool((doc.get("features") or {}).get("hooks")), "[features] hooks = true"))
            servers = list((doc.get("mcp_servers") or {}).keys())
            checks.append((bool(servers), f"mcp_servers registered: {', '.join(servers) or 'NONE'}"))

        hooks_path = home / "hooks.json"
        wrapper = home / "agentihooks-hook.sh"
        if hooks_path.exists():
            try:
                hooks_doc = json.loads(hooks_path.read_text())
                events = list(hooks_doc.get("hooks", {}).keys())
                ours = sum(
                    1
                    for groups in hooks_doc.get("hooks", {}).values()
                    for g in groups
                    for h in g.get("hooks", [])
                    if _command_is_wrapper(h.get("command", ""), wrapper)
                )
                checks.append(
                    (
                        ours >= len(CODEX_HOOK_EVENTS),
                        f"hooks.json wires {ours} agentihooks entries across {len(events)} events",
                    )
                )
            except json.JSONDecodeError as exc:
                checks.append((False, f"hooks.json unparseable: {exc}"))
        else:
            checks.append((False, "hooks.json missing"))
        checks.append((wrapper.exists() and os.access(wrapper, os.X_OK), f"hook wrapper executable ({wrapper})"))

        agents_md = home / "AGENTS.md"
        if agents_md.exists():
            size = len(agents_md.read_bytes())
            ceiling = int((doc or {}).get("project_doc_max_bytes", 32768) or 32768)
            checks.append((size < ceiling, f"AGENTS.md {size}B < project_doc_max_bytes {ceiling}"))
            checks.append((_MANAGED_HEADER in agents_md.read_text(), "AGENTS.md is agentihooks-managed"))
        else:
            checks.append((False, "AGENTS.md missing"))

        skills = agents_skills_home()
        n_skills = len(list(skills.iterdir())) if skills.is_dir() else 0
        checks.append((n_skills > 0, f"{n_skills} skill(s) in {skills}"))

        failed = 0
        for ok, msg in checks:
            print(f"  [{'OK' if ok else '!!'}] {msg}")
            if not ok:
                failed += 1
        print(
            "  [--] Hook trust cannot be verified from outside codex — run /hooks in a "
            "codex session to confirm; untrusted hooks are SILENTLY skipped."
        )
        return failed

    # ------------------------------------------------------------------
    # TOML round-trip (operator hand-edits outside managed keys survive)
    # ------------------------------------------------------------------

    @staticmethod
    def _load_toml(path: Path):
        import tomlkit

        if path.exists():
            try:
                return tomlkit.parse(path.read_text())
            except Exception:
                backup = path.with_suffix(f".toml.bak.{datetime.now(timezone.utc):%Y%m%d%H%M%S}")
                shutil.copy2(path, backup)
                print(f"  [!!] Unparseable config.toml backed up → {backup}")
        import tomlkit

        return tomlkit.document()

    @staticmethod
    def _dump_toml(path: Path, doc) -> None:
        import tomlkit

        _atomic_write(path, tomlkit.dumps(doc))
