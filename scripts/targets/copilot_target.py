"""GitHub Copilot CLI target adapter.

Writes the Copilot-shaped install surface (docs/reference/COPILOT-COMPAT.md §5):

- ``~/.copilot/settings.json`` — managed keys only; ``config.json`` is
  machine-managed by the CLI ("User settings belong in settings.json" is its
  own header comment) and is never written here.
- ``~/.copilot/hooks/agentihooks.json`` + wrapper script — lifecycle events
  routed to ``python -m hooks`` with ``AGENTIHOOKS_TARGET=copilot``.
- ``~/.copilot/copilot-instructions.md`` — persona: bundle CLAUDE.md ⊕
  profile-chain CLAUDE.mds ⊕ compiled rules ⊕ CI manifesto.
- ``~/.agents/skills/`` — skills symlinks, plus commands translated to skills
  (Copilot has no prompt-file/slash-command mechanism).
- ``~/.copilot/agents/`` — agents translated to Copilot custom agents.
- ``~/.copilot/mcp-config.json`` — MCP registration (stdio/http/sse).

Copilot facts this file encodes were verified against @github/copilot
1.0.79-6: the ``HookType`` enum in ``schemas/api.schema.json``, the settings
catalogue embedded in ``prebuilds/*/runtime.node``, and the config help topic
in ``app.js`` (docs/reference/COPILOT-COMPAT.md §10 evidence table).
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from scripts.targets._common import (
    TRANSLATED_COMMANDS_MANIFEST,
    _atomic_write,
    _command_is_wrapper,
    _install_module,
    agents_skills_home,
    build_persona,
    clear_managed_mcp,
    drop_if_credentialed,
    has_env_reference,
    load_manifest,
    reap_translated_commands,
    record_managed_mcp,
    resolve_env_references,
    scannable,
    skill_names_in,
    strip_persona,
    write_persona,
)

_MANAGED_HEADER = "<!-- managed-by: agentihooks — regenerate with: agentihooks init --target copilot -->"
_MANAGED_FOOTER = "<!-- agentihooks:managed-end -->"

# The subset of Copilot's 17-event HookType enum that hook_manager dispatches,
# spelled exactly as the enum in the shipped schemas/api.schema.json.
#
# Copilot's loader accepts the Claude-style PascalCase aliases too — verified
# against 1.0.80, where a config carrying all twelve PascalCase names drew no
# complaint while a bogus name in the same file was named in
# "Ignoring unknown hook event(s)". The enum spellings are used anyway: they are
# the ones with direct schema evidence, and acceptance by the loader is not by
# itself proof that an alias reaches the same handler.
#
# Two of these are NOT case variants — `agentStop` and `userPromptSubmitted` are
# different tokens from Claude's `Stop` and `UserPromptSubmit`, so lowercasing a
# Claude event name would silently produce an event that does not exist.
# hooks.targets.normalizer maps both spellings back to the dispatch vocabulary.
COPILOT_HOOK_EVENTS = (
    "sessionStart",
    "sessionEnd",
    "userPromptSubmitted",
    "preToolUse",
    "postToolUse",
    "postToolUseFailure",
    "agentStop",
    "subagentStart",
    "subagentStop",
    "preCompact",
    "permissionRequest",
    "notification",
)

# A hook that outlives this budget is killed. Copilot fails OPEN on timeout —
# on every event, PreToolUse included — so a slow hook silently stops guarding
# rather than blocking the session. Generous enough that only a genuinely hung
# process hits it.
#
# Field name is `timeoutSeconds`: `timeoutSec` (what the public hooks reference
# documents) appears ZERO times in the shipped 1.0.80 package, while
# `timeoutSeconds` appears in both app.js and the native engine. The loader
# tolerates unrecognized keys silently, so a wrong spelling would not error —
# it would just leave the default in force.
_HOOK_TIMEOUT_SECONDS = 30

# Claude tool names → Copilot runtime tool names, for custom-agent frontmatter.
_TOOL_NAMES = {
    "Read": "view",
    "Write": "create",
    "Edit": "edit",
    "Bash": "shell",
    "Grep": "grep",
    "Glob": "glob",
    "WebFetch": "web_fetch",
    "WebSearch": "web_search",
    "Agent": "task",
    "Task": "task",
    "TodoWrite": "update_todo",
    "AskUserQuestion": "ask_user",
}

# Copilot caps a custom agent body at 30,000 characters.
_AGENT_BODY_MAX = 30000

# Copilot refuses a SKILL.md whose description exceeds this and drops the skill
# with a load error; claude and codex have no equivalent limit, so an over-long
# description is invisible until a copilot session reports it.
_SKILL_DESCRIPTION_MAX = 1024


def oversized_skill_descriptions(skills_dir: Path) -> list[tuple[str, int]]:
    """(skill name, description length) for every skill copilot will refuse."""
    import yaml

    out: list[tuple[str, int]] = []
    if not skills_dir.is_dir():
        return out
    for skill in sorted(skills_dir.iterdir()):
        md = skill / "SKILL.md"
        if not md.is_file():
            continue
        try:
            text = md.read_text()
            if not text.startswith("---"):
                continue
            front = yaml.safe_load(text.split("---", 2)[1]) or {}
        except (OSError, ValueError, yaml.YAMLError):
            continue
        description = " ".join(str(front.get("description", "")).split())
        if len(description) > _SKILL_DESCRIPTION_MAX:
            out.append((skill.name, len(description)))
    return out


def copilot_home() -> Path:
    """Resolve COPILOT_HOME (first entry when the env var is a comma list)."""
    raw = os.environ.get("COPILOT_HOME", "").split(",")[0].strip()
    return Path(raw).expanduser() if raw else Path.home() / ".copilot"


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """``(frontmatter, body)`` from a markdown file; ``({}, text)`` when absent."""
    import yaml

    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) != 3:
        return {}, text
    try:
        front = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        front = {}
    return (front if isinstance(front, dict) else {}), parts[2].lstrip("\n")


_MCP_ALWAYS_ENABLED = ("hooks-utils",)

# WSL: reach a Windows browser, which holds the sessions the distro's own browser
# does not. Tried in order; the first that resolves wins.
#
# `explorer.exe` is deliberately absent. It is the obvious choice and it is wrong:
# spawned with a Linux working directory — Copilot's normal condition — it ignores
# the URL and opens a File Explorer window on Documents. Every launcher here takes
# the URL as argv, so a query string full of `&` survives; anything routed through
# `cmd /c start` would not.
_WSL_BROWSER_CANDIDATES = (
    "wslview",
    "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe",
    "/mnt/c/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    "/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "/mnt/c/Program Files/Microsoft/Edge/Application/msedge.exe",
)

_OAUTH_URL_FILE = "$HOME/.copilot/pending-oauth-urls.txt"
_OAUTH_URL_SINK = f'mkdir -p "$HOME/.copilot" && printf "%s\\n" "$1" >> "{_OAUTH_URL_FILE}"'


class CopilotAdapter:
    name = "copilot"

    def __init__(self) -> None:
        # Rules collected by install_features("rules", ...) and compiled into
        # copilot-instructions.md by install_persona — the features loop runs first.
        self._pending_rules: list[tuple[str, str, str]] = []  # (layer_label, name, text)
        # Skill names seen in this install, recorded by the skills step so the
        # later commands step knows which names a real skill already owns. The
        # driver always runs skills before commands.
        self._skill_names: set[str] = set()

    def home(self) -> Path:
        return copilot_home()

    @staticmethod
    def _managed_sidecar(home: Path) -> Path:
        return home / ".agentihooks-managed.json"

    @staticmethod
    def _bypass_env_file() -> Path:
        return _install_module().AGENTIHOOKS_STATE_DIR / "copilot.env"

    @staticmethod
    def _running_under_wsl() -> bool:
        if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
            return True
        try:
            return "microsoft" in Path("/proc/version").read_text().lower()
        except OSError:
            return False

    @staticmethod
    def _resolves(command: str) -> bool:
        return bool(Path(command).exists() if os.sep in command else shutil.which(command))

    def _resolve_browser_command(self, browser) -> list[str] | None:
        """Resolve ``_agentihooks.browserCommand`` against the machine being installed on.

        A bundle profile is installed on more than one machine, so a launcher
        naming a Windows browser is right under WSL and absent on macOS or native
        Linux. Copilot reports only a *spawn* failure to its debug log, so an
        unresolvable launcher would silently open nothing — worse than the
        platform default it replaced. Anything that does not resolve here is
        dropped with a warning, leaving Copilot's own default in place.

        ``"auto"`` asks for the right answer per platform: the first resolvable
        entry of ``_WSL_BROWSER_CANDIDATES`` under WSL, and Copilot's own default
        everywhere else, which is already the operator's chosen browser.
        """
        _i = _install_module()
        if isinstance(browser, str) and browser.strip().lower() == "auto":
            if not self._running_under_wsl():
                return None
            browser = next(
                ([c] for c in _WSL_BROWSER_CANDIDATES if self._resolves(c)),
                None,
            )
            if not browser:
                _i._cprint(
                    "  [!!] browserCommand 'auto': no Windows browser found from WSL — "
                    "copilot keeps xdg-open, which reaches a browser inside the distro "
                    "carrying none of your Windows sessions. Install wslu, or name a "
                    "browser path in browserCommand."
                )
                return None
        if isinstance(browser, str):
            browser = shlex.split(browser)
        if not browser:
            return None
        # A typo'd native file (`"browserCommand": true`) must degrade to
        # Copilot's own browser like every other malformed value, not abort the
        # whole install. A dict is iterable over its keys, which would build a
        # launcher out of nonsense — excluded explicitly rather than by luck.
        if not isinstance(browser, (list, tuple)) or not all(isinstance(p, str) for p in browser):
            _i._cprint(
                f"  [!!] browserCommand must be a string or a list of strings, got {type(browser).__name__} "
                "— dropped. Copilot keeps its own per-platform browser."
            )
            return None

        launcher = [str(part) for part in browser]
        head = launcher[0]
        if not self._resolves(head):
            _i._cprint(
                f"  [!!] browserCommand '{head}' not found on this machine — dropped. "
                'Copilot keeps its own per-platform browser; use "auto" for a portable profile.'
            )
            return None
        return launcher

    def _write_managed_env(self, directives: dict) -> None:
        """Render the ``_agentihooks`` directives Copilot exposes only as env vars.

        ``allowAll`` — Copilot has no settings key for YOLO: ``permissions.allow``
        rules cover tools only (a ``write`` rule still hits path verification)
        and ``trustedFolders`` is not a recognized user setting at all.
        ``COPILOT_ALLOW_ALL`` (the env form of ``--allow-all-tools``) is the only
        global switch, proven to clear a denial that tool rules did not.

        ``browserCommand`` / ``suppressBrowserLaunch`` — Copilot resolves the OAuth
        browser per platform: ``open`` on macOS, ``xdg-open`` on Linux. Under WSL
        ``xdg-open`` reaches whatever Linux browser is installed in the distro,
        which carries none of the operator's Windows sessions, so a Microsoft
        authorization lands in a browser that can never satisfy it.
        ``COPILOT_DEBUG_BROWSER`` is consulted ahead of every launch path — before
        the remote-environment skip, before ``$BROWSER``, before the per-platform
        default — and takes a JSON string array that Copilot spawns with the URL
        appended. ``browserCommand`` names that command (``explorer.exe`` for the
        Windows default browser, or an explicit ``chrome.exe`` path);
        ``suppressBrowserLaunch`` substitutes a sink that parks the URL in a file
        instead, for when no browser should open at all. ``browserCommand`` wins
        if both are set.

        ``channels`` — the broadcast subscription list. Claude carries it in its
        settings ``env`` block; Copilot has no ``env`` settings key, so without
        this a Copilot session reads an unset ``AGENTIHOOKS_BASE_CHANNELS``,
        subscribes to nothing, and every channel-targeted broadcast passes it by.

        All land in a managed env file the installer's ``agentienv`` shell block
        already auto-exports, so they reach interactive sessions.
        """
        _i = _install_module()
        path = self._bypass_env_file()

        lines: list[str] = []
        launcher: list[str] | None = None
        launcher_note = ""
        channels = directives.get("channels")
        if isinstance(channels, (list, tuple)):
            channels = ",".join(str(c) for c in channels)
        if channels:
            lines.append("# native _agentihooks.channels → broadcast subscriptions\n")
            lines.append(f"AGENTIHOOKS_BASE_CHANNELS={channels}\n")
        if directives.get("allowAll"):
            # The literal string "true", not "1". Commander binds the var to
            # --allow-all-tools on presence alone, so "1" does grant the tools
            # axis — but folder trust, workspace MCP sources, repo hooks and
            # plugin loading each test `=== "true"` separately, and silently
            # stay off for any other value. Half the switch is worse than none,
            # because the half that works makes it look set.
            lines.append("# native _agentihooks.allowAll → Copilot allow-all-tools\n")
            lines.append("COPILOT_ALLOW_ALL=true\n")

        browser = self._resolve_browser_command(directives.get("browserCommand"))
        if browser:
            launcher = browser
            launcher_note = f"opens {launcher[0]}"
            lines.append("# native _agentihooks.browserCommand → OAuth browser launcher\n")
        elif directives.get("suppressBrowserLaunch"):
            launcher = ["sh", "-c", _OAUTH_URL_SINK, "agentihooks-oauth"]
            launcher_note = f"parks OAuth URLs in {_OAUTH_URL_FILE}"
            lines.append("# native _agentihooks.suppressBrowserLaunch → park OAuth URLs, never open a browser\n")
        if launcher:
            lines.append(f"COPILOT_DEBUG_BROWSER='{json.dumps(launcher)}'\n")

        if not lines:
            if path.exists():
                path.unlink()
                _i._cprint(f"  [RM] no copilot env directives — removed {path}")
            return

        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(
            path,
            "# managed-by: agentihooks — regenerate with: agentihooks init --target copilot\n" + "".join(lines),
        )
        if channels:
            _i._cprint(f"  [OK] broadcast channels → AGENTIHOOKS_BASE_CHANNELS={channels} in {path}")
        if directives.get("allowAll"):
            _i._cprint(f"  [OK] allowAll → COPILOT_ALLOW_ALL=true in {path} (run: source ~/.bashrc)")
        if launcher:
            _i._cprint(f"  [OK] COPILOT_DEBUG_BROWSER in {path} — {launcher_note} (run: source ~/.bashrc)")

    # ------------------------------------------------------------------
    # settings: settings.json managed keys + hooks/agentihooks.json + wrapper
    # ------------------------------------------------------------------

    def write_settings(self, native: dict) -> Path:
        _i = _install_module()
        home = self.home()
        home.mkdir(parents=True, exist_ok=True)

        settings_path = home / "settings.json"
        doc = self._load_json(settings_path)

        # Settings are authored natively (profiles/_base/settings.base.copilot.json
        # plus each profile's .copilot/settings.overrides.json) and arrive here
        # already merged.
        #
        # `_agentihooks` is a reserved block, not a Copilot setting — Copilot
        # warns about unknown top-level keys, so it is consumed here and never
        # written to disk. It carries the directives Copilot has no settings key
        # for; today that is allow-all, which is an env var only.
        native = dict(native or {})
        directives = native.pop("_agentihooks", None) or {}
        self._mcp_directives = directives
        wanted: dict = {k: v for k, v in native.items() if not k.startswith("_")}

        # Copilot merges an inline `hooks` key with the hooks/ directory, so a
        # native file declaring both would fire every hook twice.
        if wanted.pop("hooks", None) is not None:
            _i._cprint(
                "  [!!] native copilot settings declared a 'hooks' key — dropped. "
                "Hooks are written to hooks/agentihooks.json; declaring both fires each hook twice."
            )

        # Floor: the hook layer is the entire guardrail surface.
        wanted["disableAllHooks"] = False

        # Managed-key discipline: record what we wrote so a later change can
        # update it, while a value the operator hand-edited since our last
        # write is left alone. The record lives in a sidecar file — copilot
        # warns about unknown top-level settings keys on every launch, so an
        # in-file `agentihooks` key is per-launch noise (observed v1.0.80).
        recorded = self._load_json(self._managed_sidecar(home))
        legacy = doc.pop("agentihooks", None)
        if not recorded and isinstance(legacy, dict) and isinstance(legacy.get("managed"), dict):
            recorded = legacy["managed"]
        recorded = dict(recorded) if isinstance(recorded, dict) else {}

        for key, value in wanted.items():
            current = doc.get(key)
            # `current == value`: the on-disk value already equals what we
            # would write, so it is ours (or an operator edit identical to
            # ours — indistinguishable and immaterial). Recording it heals a
            # lost/deleted sidecar; without it, a sidecar-less install with our
            # value intact would misread every key as a hand-edit forever and
            # never auto-apply a future change.
            if current is None or current == recorded.get(key) or current == value:
                doc[key] = value
                recorded[key] = value
            else:
                _i._cprint(
                    f"  [!!] settings.json '{key}' hand-set to {current!r} (managed value would be "
                    f"{value!r}) — leaving operator value in place"
                )
        _atomic_write(self._managed_sidecar(home), json.dumps(recorded, indent=2) + "\n")

        self._write_managed_env(directives)

        _atomic_write(settings_path, json.dumps(doc, indent=2) + "\n")
        _i._cprint(f"[OK] Wrote managed keys into {settings_path}")

        self._write_hooks_json(home)
        return settings_path

    def _write_hooks_json(self, home: Path) -> None:
        _i = _install_module()
        wrapper = home / "agentihooks-hook.sh"
        python_bin = str(_i._detect_venv() or sys.executable)
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        wrapper.write_text(
            "#!/usr/bin/env bash\n"
            "# managed-by: agentihooks — regenerate with: agentihooks init --target copilot\n"
            "set -euo pipefail\n"
            f"cd {shlex.quote(str(_i.AGENTIHOOKS_ROOT))}\n"
            f'AGENTIHOOKS_COPILOT_EVENT="${{1:-}}" AGENTIHOOKS_TARGET=copilot '
            f"exec {shlex.quote(python_bin)} -m hooks\n"
        )
        wrapper.chmod(0o755)

        # The hooks DIRECTORY, not an inline `hooks` key in settings.json.
        # Copilot merges both sources, so writing to both fires every hook twice.
        hooks_dir = home / "hooks"
        hooks_path = hooks_dir / "agentihooks.json"
        # Copilot's hook stdin is the event's input object alone — no
        # hookEventName/hookType field (observed live, v1.0.80: a dispatched
        # sessionEnd carried only reason/sessionId/timestamp/cwd). The
        # registration is the event's identity, so each entry passes its event
        # name as argv[1]; the wrapper exports it as AGENTIHOOKS_COPILOT_EVENT
        # for the normalizer. Commands run via /bin/sh, so the argument
        # survives the spawn.
        desired = {
            e: [
                {
                    "type": "command",
                    "command": f"{wrapper} {e}",
                    "timeoutSeconds": _HOOK_TIMEOUT_SECONDS,
                }
            ]
            for e in COPILOT_HOOK_EVENTS
        }

        existing: dict = {}
        if hooks_path.exists():
            try:
                existing = json.loads(hooks_path.read_text())
            except (json.JSONDecodeError, OSError):
                backup = hooks_path.with_suffix(f".json.bak.{datetime.now(timezone.utc):%Y%m%d%H%M%S}")
                shutil.copy2(hooks_path, backup)
                _i._cprint(f"  [!!] Unparseable agentihooks.json backed up → {backup}")
                existing = {}

        # Preserve foreign entries the operator added to OUR file; other files
        # under hooks/ are never read or rewritten.
        merged = existing.get("hooks", {}) if isinstance(existing.get("hooks"), dict) else {}
        for event, hooks in desired.items():
            prior = merged.get(event, [])
            prior = prior if isinstance(prior, list) else []
            foreign = [h for h in prior if not _command_is_wrapper(h.get("command", ""), wrapper)]
            merged[event] = foreign + hooks
        for event in [e for e in merged if e not in desired]:
            prior = merged[event] if isinstance(merged[event], list) else []
            foreign = [h for h in prior if not _command_is_wrapper(h.get("command", ""), wrapper)]
            if foreign:
                merged[event] = foreign
            else:
                del merged[event]

        _atomic_write(hooks_path, json.dumps({"version": 1, "hooks": merged}, indent=2) + "\n")
        _i._cprint(f"[OK] Wrote {hooks_path} ({len(COPILOT_HOOK_EVENTS)} events)")
        _i._cprint(
            "  [!!] Copilot keys hook trust by content hash (`disabledHooks`): editing the "
            "wrapper invalidates the hash and the hook needs re-approving."
        )

    # ------------------------------------------------------------------
    # features: skills / agents / commands / rules
    # ------------------------------------------------------------------

    def install_features(self, subdir: str, layers: list[tuple[str, Path]], filter_fn) -> None:
        _i = _install_module()
        if subdir == "skills":
            dst = agents_skills_home()
            self._skill_names = skill_names_in(layers, filter_fn)
            # A name that used to be a command and is now a real skill still has
            # our translated directory sitting on it. The symlinker refuses to
            # replace a non-symlink, so without this the real skill would never
            # install — and nothing else would ever reap the directory while the
            # command file still exists. Clear our own artifact first; the
            # manifest is what proves the directory is ours to remove.
            reap_translated_commands(self._skill_names, reason="a real skill now owns the name")
            for label, src in layers:
                _i._symlink_dir_contents(src, dst, label=f"copilot {label}", filter_fn=filter_fn)
        elif subdir == "commands":
            self._translate_commands_to_skills(layers, filter_fn)
        elif subdir == "agents":
            self._translate_agents(layers, filter_fn)
        elif subdir == "rules":
            # Copilot auto-loads instructions files, not a rules dir — compile
            # them into copilot-instructions.md.
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
            _i._cprint(f"  [OK] {len(self._pending_rules)} rule(s) queued for copilot-instructions.md compilation")

    def _translate_commands_to_skills(self, layers: list[tuple[str, Path]], filter_fn) -> None:
        """commands/*.md → ~/.agents/skills/<name>/SKILL.md.

        Copilot has no prompt-file mechanism (github/copilot-cli#1113), so a
        command reaches the model as a discoverable skill instead of a slash
        command. Tracked in a manifest keyed separately from the symlinked
        skills that share this directory, so a codex re-init cannot reap what
        this wrote and vice versa.
        """
        import yaml

        _i = _install_module()
        dst_dir = agents_skills_home()
        dst_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = dst_dir / TRANSLATED_COMMANDS_MANIFEST
        previous = load_manifest(manifest_path)

        sources: dict[str, Path] = {}
        for _label, src in layers:
            if not src.is_dir():
                continue
            for f in sorted(src.iterdir()):
                if filter_fn(f):
                    sources[f.name] = f

        written: list[str] = []
        for name, src in sources.items():
            stem = Path(name).stem
            skill_dir = dst_dir / stem
            # A real skill of the same name outranks a translated command,
            # whether it is already symlinked or was just installed in this
            # run's skills step. Recreating our directory here would shadow it
            # on the next install, when the symlinker refuses to replace a
            # non-symlink.
            if stem in self._skill_names or skill_dir.is_symlink():
                _i._cprint(f"  [!!] skill '{stem}' owns this name — command translation skipped")
                continue
            if skill_dir.exists() and stem not in previous:
                _i._cprint(f"  [!!] {skill_dir} exists and is not agentihooks-managed — skipping (operator file wins)")
                continue
            try:
                front, body = _split_frontmatter(src.read_text())
            except OSError:
                continue
            description = front.get("description") or f"Command '{stem}' from the agentihooks bundle."
            out_front = {"name": stem, "description": description}
            skill_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write(
                skill_dir / "SKILL.md",
                "---\n" + yaml.safe_dump(out_front, sort_keys=False).strip() + "\n---\n\n" + body,
            )
            written.append(stem)

        reap_translated_commands(set(previous) - set(written), reason="no longer in any source layer")
        _atomic_write(manifest_path, json.dumps(sorted(written)))
        _i._cprint(f"  [OK] {len(written)} command(s) translated → skills in {dst_dir}")

    def _translate_agents(self, layers: list[tuple[str, Path]], filter_fn) -> None:
        """agents/*.md → ~/.copilot/agents/*.md with Copilot frontmatter.

        Real files, not symlinks — the frontmatter schema differs (tool names
        are Copilot runtime names, ``description`` is required).
        """
        import yaml

        _i = _install_module()
        dst_dir = self.home() / "agents"
        dst_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = dst_dir / ".agentihooks-manifest.json"
        previous = load_manifest(manifest_path)

        sources: dict[str, Path] = {}
        for _label, src in layers:
            if not src.is_dir():
                continue
            for f in sorted(src.iterdir()):
                if filter_fn(f):
                    sources[f.name] = f

        written: list[str] = []
        for name, src in sources.items():
            dst_file = dst_dir / name
            if dst_file.exists() and name not in previous:
                _i._cprint(f"  [!!] {dst_file} exists and is not agentihooks-managed — skipping (operator file wins)")
                continue
            try:
                front, body = _split_frontmatter(src.read_text())
            except OSError:
                continue

            stem = Path(name).stem
            out_front: dict = {
                "name": front.get("name") or stem,
                # Required by Copilot; a missing one makes the agent unloadable.
                "description": front.get("description") or f"Agent '{stem}' from the agentihooks bundle.",
            }
            tools = front.get("tools")
            if isinstance(tools, str):
                tools = [t.strip() for t in tools.split(",") if t.strip()]
            if isinstance(tools, list):
                # Claude scoped grants ("Bash(git diff*)") reduce to the bare
                # tool — copilot's grammar has no scoping, so the scope is
                # dropped and the agent body's own discipline is what remains
                # of the restriction.
                mapped = [
                    _TOOL_NAMES.get(base, base)
                    for t in tools
                    if isinstance(t, str)
                    for base in (t.split("(", 1)[0].strip(),)
                    if base
                ]
                if mapped:
                    out_front["tools"] = sorted(set(mapped))
            # Claude model aliases ("haiku", "sonnet") are not copilot model
            # ids — copilot warns "model X is not available" on every agent
            # invocation and falls back to auto. Dropping the field IS the
            # auto fallback, without the per-run warning.

            if len(body) > _AGENT_BODY_MAX:
                marker = "\n\n<!-- truncated by agentihooks: exceeds Copilot's 30000-char agent body limit -->\n"
                cut = body.rfind("\n\n", 0, _AGENT_BODY_MAX - len(marker))
                body = body[: cut if cut > 0 else _AGENT_BODY_MAX - len(marker)] + marker
                _i._cprint(f"  [!!] agent '{stem}' body exceeded 30000 chars — truncated at a paragraph boundary")

            _atomic_write(
                dst_file,
                "---\n" + yaml.safe_dump(out_front, sort_keys=False).strip() + "\n---\n\n" + body,
            )
            written.append(name)

        for stale in set(previous) - set(written):
            (dst_dir / stale).unlink(missing_ok=True)
        _atomic_write(manifest_path, json.dumps(sorted(written)))
        _i._cprint(f"  [OK] {len(written)} agent(s) translated → {dst_dir}")

    # ------------------------------------------------------------------
    # persona: copilot-instructions.md
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
        dst = self.home() / "copilot-instructions.md"
        text = write_persona(dst, managed_text, _MANAGED_HEADER, _MANAGED_FOOTER)
        _i._cprint(
            f"[OK] Wrote {dst} ({len(text.encode())} bytes; {len(profile_chain)} profile(s), "
            f"{len(self._pending_rules)} rule(s))"
        )

    # ------------------------------------------------------------------
    # MCP registration
    # ------------------------------------------------------------------

    def register_hooks_utils(self, profile_name: str) -> None:
        _i = _install_module()
        python_bin = str(_i._detect_venv() or sys.executable)
        # Same transport resolver as the claude path — it reads
        # AGENTIHOOKS_MCP_TRANSPORT and ~/.agentihooks/.env, which a bare
        # os.environ read does not, so the targets cannot disagree about
        # whether the daemon is in use.
        transport = _i._resolve_installer_mcp_transport()
        if transport == "stdio":
            entry: dict = {"command": python_bin, "args": ["-m", "hooks.mcp"]}
        else:
            # Reuse the claude-side builder rather than re-deriving the URL: it
            # validates MCP_PORT and honours MCP_SCHEME.
            entry = {
                "type": "http",
                "url": _i._build_mcp_config("")["mcpServers"]["hooks-utils"]["url"],
            }
        self.register_mcp({"hooks-utils": entry})

    def register_mcp(self, servers: dict) -> None:
        """Merge a layer of MCP servers into ~/.copilot/mcp-config.json.

        Claude ``.mcp.json`` entries translate almost 1:1. Unlike codex,
        Copilot has an SSE client, so no transport is dropped. It has no
        ``${VAR}`` header expansion either — a header reference is resolved
        from the install environment and baked to the literal Copilot needs
        (the same value ``~/.claude.json`` holds); an unset variable keeps the
        drop-and-warn.
        """
        _i = _install_module()
        config_path = self.home() / "mcp-config.json"
        doc = self._load_json(config_path)
        table = doc.get("mcpServers")
        table = dict(table) if isinstance(table, dict) else {}

        from hooks.secrets import scan as _scan_secrets

        added: list[str] = []
        for name, spec in servers.items():
            spec = dict(spec)
            if drop_if_credentialed(name, spec, "mcp-config.json"):
                continue
            stype = spec.get("type") or ("local" if spec.get("command") else "http")
            if stype == "stdio":
                stype = "local"

            entry: dict = {"type": stype}
            if spec.get("command"):
                entry["command"] = spec["command"]
                if spec.get("args"):
                    entry["args"] = list(spec["args"])
                if spec.get("env"):
                    clean_env: dict = {}
                    for ek, ev in dict(spec["env"]).items():
                        ev_s = scannable(str(ev))
                        if not ev_s:
                            # Nothing but ${VAR} references — no literal to scan.
                            clean_env[ek] = ev
                            continue
                        hits = _scan_secrets(ev_s, mode="strict")
                        if hits:
                            _i._cprint(
                                f"  [!!] MCP '{name}' env var '{ek}' looks like a credential "
                                f"({', '.join(hits)}) — dropped from mcp-config.json. Export it in "
                                "the shell environment instead of writing it to disk."
                            )
                            continue
                        clean_env[ek] = ev
                    if clean_env:
                        entry["env"] = clean_env
            elif spec.get("url"):
                entry["url"] = spec["url"]
                clean_headers: dict = {}
                for hk, hv in dict(spec.get("headers") or {}).items():
                    hv_s = str(hv)
                    if has_env_reference(hv_s):
                        resolved, ok = resolve_env_references(hv_s)
                        if ok:
                            # Copilot sends headers literally, so a ${VAR} must
                            # be baked to the value from the operator's own env
                            # at install time — the same literal ~/.claude.json
                            # already stores. The scan below is skipped on
                            # purpose: the resolved token IS the intended
                            # credential, not an accidental leak.
                            clean_headers[hk] = resolved
                            continue
                        _i._cprint(
                            f"  [!!] MCP '{name}' header '{hk}' references an unset variable — "
                            "Copilot sends header values literally and does not expand these; "
                            "header dropped. Export the variable before install, or set a literal."
                        )
                        continue
                    hits = _scan_secrets(hv_s, mode="strict")
                    if hits:
                        _i._cprint(
                            f"  [!!] MCP '{name}' header '{hk}' looks like a credential "
                            f"({', '.join(hits)}) — dropped from mcp-config.json."
                        )
                        continue
                    clean_headers[hk] = hv
                if clean_headers:
                    entry["headers"] = clean_headers
            else:
                continue
            if spec.get("tools"):
                clean_tools = []
                for tool in spec["tools"]:
                    hits = _scan_secrets(scannable(str(tool)), mode="strict")
                    if hits:
                        _i._cprint(
                            f"  [!!] MCP '{name}' tools entry looks like a credential "
                            f"({', '.join(hits)}) — dropped from mcp-config.json."
                        )
                        continue
                    clean_tools.append(tool)
                if clean_tools:
                    entry["tools"] = clean_tools
            # Copilot-native fields a Claude .mcp.json cannot express. Passed
            # through when a native mcp-config layer supplies them:
            #   auth/oidc=false  — do NOT attempt OAuth for this server. Without
            #     it a 401 from an http/sse server starts a browser OAuth flow,
            #     which under WSL launches a Windows browser that cannot
            #     authenticate and leaves the session hanging.
            #   tools/excludeTools — trim the tool surface a server contributes,
            #     the only lever against copilot's static-context ceiling.
            #   deferTools — "auto" allows tool-search deferral where enabled.
            for key in ("auth", "oidc", "deferTools", "excludeTools", "timeout", "filterMapping"):
                if key in spec:
                    entry[key] = spec[key]

            # Interactive OAuth is opt-IN on copilot. Left to its default, a 401
            # from any http/sse server starts a browser authorization flow; under
            # WSL that launches a Windows browser with no session and the turn
            # hangs with nothing to click. Our servers authenticate by header or
            # run locally, so a server that genuinely needs OAuth says so with an
            # explicit `auth: true` in its native mcp-config layer.
            entry.setdefault("auth", False)
            entry.setdefault("oidc", False)

            table[name] = entry
            added.append(name)

        doc["mcpServers"] = table
        _atomic_write(config_path, json.dumps(doc, indent=2) + "\n")
        if added:
            record_managed_mcp(self.name, added)
            _i._cprint(f"  [OK] Copilot MCP servers: {', '.join(added)}")

    def teardown(self) -> None:
        _i = _install_module()
        home = self.home()
        wrapper = home / "agentihooks-hook.sh"

        # Hooks: strip our entries from our file; foreign entries the operator
        # added to it survive, and other files under hooks/ are never touched.
        hooks_path = home / "hooks" / "agentihooks.json"
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
            if doc is None:
                merged = None
            if merged is not None:
                foreign_left = {}
                for event, hooks in merged.items():
                    hooks = hooks if isinstance(hooks, list) else []
                    foreign = [h for h in hooks if not _command_is_wrapper(h.get("command", ""), wrapper)]
                    if foreign:
                        foreign_left[event] = foreign
                if foreign_left:
                    _atomic_write(hooks_path, json.dumps({"version": 1, "hooks": foreign_left}, indent=2) + "\n")
                    _i._cprint(f"  [RM] Removed agentihooks entries from {hooks_path} (operator hooks kept)")
                else:
                    hooks_path.unlink()
                    _i._cprint(f"  [RM] Removed {hooks_path}")
        wrapper.unlink(missing_ok=True)

        # settings.json: remove keys still holding our recorded value; a key
        # the operator hand-edited since is theirs and stays. Our seeded
        # trustedFolders entry is withdrawn; the operator's own entries stay.
        settings_path = home / "settings.json"
        if settings_path.exists():
            doc = self._load_json(settings_path)
            recorded = self._load_json(self._managed_sidecar(home))
            if not recorded:
                legacy = doc.get("agentihooks")
                recorded = legacy.get("managed", {}) if isinstance(legacy, dict) else {}
            removed_keys = []
            for key, value in list(recorded.items() if isinstance(recorded, dict) else []):
                if doc.get(key) == value:
                    doc.pop(key, None)
                    removed_keys.append(key)
            if not recorded:
                # No record (legacy install / hand-deleted table): remove only
                # what is content-verifiably ours rather than claiming by name.
                status = doc.get("statusLine")
                if isinstance(status, dict) and "hooks.statusline" in str(status.get("command", "")):
                    doc.pop("statusLine", None)
                    removed_keys.append("statusLine")
                if "disableAllHooks" in doc:
                    _i._cprint("  [!!] no managed-key record found — disableAllHooks left as-is; review it")
            doc.pop("agentihooks", None)
            # trustedFolders was written by older installs and is not a
            # recognized Copilot setting — withdraw the stale key entirely
            # rather than leaving a value the CLI warns about on every launch.
            if isinstance(doc.get("trustedFolders"), list):
                root = str(_i.AGENTIHOOKS_ROOT)
                remaining = [t for t in doc["trustedFolders"] if t != root]
                if remaining:
                    doc["trustedFolders"] = remaining
                else:
                    doc.pop("trustedFolders", None)
            _atomic_write(settings_path, json.dumps(doc, indent=2) + "\n")
            if removed_keys:
                _i._cprint(f"  [RM] Removed managed keys from {settings_path}: {', '.join(removed_keys)}")
        self._managed_sidecar(home).unlink(missing_ok=True)
        self._write_managed_env({})

        strip_persona(home / "copilot-instructions.md", _MANAGED_HEADER, _MANAGED_FOOTER)

        # Translated agents + commands: reap everything the manifests own.
        agents_dir = home / "agents"
        agents_manifest = agents_dir / ".agentihooks-manifest.json"
        for name in load_manifest(agents_manifest):
            (agents_dir / name).unlink(missing_ok=True)
        agents_manifest.unlink(missing_ok=True)

        skills_dir = agents_skills_home()
        cmd_manifest = skills_dir / TRANSLATED_COMMANDS_MANIFEST
        reap_translated_commands(set(load_manifest(cmd_manifest)), reason="copilot target removed")
        cmd_manifest.unlink(missing_ok=True)

        # MCP: remove the names this adapter recorded writing; anything else in
        # the file is the operator's.
        mcp_path = home / "mcp-config.json"
        from scripts.targets._common import managed_mcp_names

        recorded_names = managed_mcp_names(self.name)
        if mcp_path.exists():
            doc = self._load_json(mcp_path)
            table = doc.get("mcpServers")
            if isinstance(table, dict):
                removed = [n for n in recorded_names if table.pop(n, None) is not None]
                if not recorded_names and "hooks-utils" in table:
                    # No record: remove hooks-utils only when its content proves
                    # it is ours — a name collision with the operator's own
                    # server must not delete their entry.
                    entry_text = json.dumps(table["hooks-utils"])
                    if "hooks.mcp" in entry_text:
                        table.pop("hooks-utils")
                        removed.append("hooks-utils")
                    else:
                        _i._cprint(
                            "  [!!] 'hooks-utils' in mcp-config.json has no install record and "
                            "does not look agentihooks-managed — left in place; review it."
                        )
                doc["mcpServers"] = table
                _atomic_write(mcp_path, json.dumps(doc, indent=2) + "\n")
                if removed:
                    _i._cprint(f"  [RM] Removed MCP servers from {mcp_path}: {', '.join(removed)}")
        clear_managed_mcp(self.name)

    def _apply_mcp_default_disabled(self) -> None:
        """Start every configured MCP server disabled, per ``_agentihooks.mcpDefaultDisabled``.

        Copilot connects every configured server when a session opens and opens
        a browser OAuth tab for each one that 401s; there is no lazy-connect key
        (copilot-cli #1938, #2026, #3462). A server named in ``disabledMcpServers``
        stays fully configured but is not connected, so ``/mcp enable <name>``
        becomes the on-demand switch.

        ``enabledMcpServers`` is Copilot's record of what the operator turned on
        by hand; those are never re-disabled here, so an enable survives the next
        install.

        ``mcpAlwaysEnabled`` is a floor rather than a skip-list: a name in it is
        lifted OUT of ``disabledMcpServers`` as well as kept out of it. Adding to
        the set is not enough — a stale settings file, or one `/mcp disable
        hooks-utils` in the past, would otherwise leave the toolbelt off forever
        while the install reported it as enabled. Turning one off for good means
        dropping it from ``mcpAlwaysEnabled``, which is where that decision belongs.
        """
        directives = getattr(self, "_mcp_directives", None) or {}
        if not directives.get("mcpDefaultDisabled"):
            return

        _i = _install_module()
        home = self.home()
        config = self._load_json(home / "mcp-config.json")
        configured = sorted((config.get("mcpServers") or {}).keys())
        if not configured:
            return

        settings_path = home / "settings.json"
        doc = self._load_json(settings_path)
        always_on = set(directives.get("mcpAlwaysEnabled") or _MCP_ALWAYS_ENABLED)
        operator_enabled = set(doc.get("enabledMcpServers") or [])

        disabled = set(doc.get("disabledMcpServers") or [])
        disabled.update(n for n in configured if n not in always_on and n not in operator_enabled)
        disabled -= always_on
        if disabled == set(doc.get("disabledMcpServers") or []) and not disabled:
            return

        doc["disabledMcpServers"] = sorted(disabled)
        _atomic_write(settings_path, json.dumps(doc, indent=2) + "\n")

        held = sorted(n for n in configured if n not in disabled)
        _i._cprint(
            f"  [OK] {len(doc['disabledMcpServers'])} MCP server(s) start disabled — /mcp enable <name> to connect one"
        )
        if held:
            _i._cprint(f"       left enabled: {', '.join(held)}")

    def post_install_reconcile(self, profile_chain: list[str], persisted_profile: str) -> None:
        _i = _install_module()
        self._apply_mcp_default_disabled()
        _i._cprint("  [--] Copilot install complete. Verify with: agentihooks doctor --target copilot")

    # ------------------------------------------------------------------
    # doctor
    # ------------------------------------------------------------------

    def doctor(self) -> int:
        """Print copilot-install health; return count of failed checks."""
        home = self.home()
        checks: list[tuple[bool, str]] = []

        settings_path = home / "settings.json"
        doc = None
        if settings_path.exists():
            try:
                doc = json.loads(settings_path.read_text())
                checks.append((True, f"settings.json parses ({settings_path})"))
            except json.JSONDecodeError as exc:
                checks.append((False, f"settings.json unparseable: {exc}"))
        else:
            checks.append((False, "settings.json missing — run: agentihooks init --target copilot"))

        if isinstance(doc, dict):
            checks.append((doc.get("disableAllHooks") is not True, "disableAllHooks is not true"))
            status = doc.get("statusLine")
            checks.append(
                (isinstance(status, dict) and status.get("type") == "command", "statusLine wired to a command")
            )

        mcp_path = home / "mcp-config.json"
        if mcp_path.exists():
            try:
                servers = list((json.loads(mcp_path.read_text()).get("mcpServers") or {}).keys())
                checks.append((bool(servers), f"mcpServers registered: {', '.join(servers) or 'NONE'}"))
            except json.JSONDecodeError as exc:
                checks.append((False, f"mcp-config.json unparseable: {exc}"))
        else:
            checks.append((False, "mcp-config.json missing"))

        hooks_path = home / "hooks" / "agentihooks.json"
        wrapper = home / "agentihooks-hook.sh"
        if hooks_path.exists():
            try:
                hooks_doc = json.loads(hooks_path.read_text())
                events = hooks_doc.get("hooks", {})
                ours = sum(
                    1
                    for hooks in events.values()
                    for h in (hooks if isinstance(hooks, list) else [])
                    if _command_is_wrapper(h.get("command", ""), wrapper)
                )
                checks.append(
                    (
                        ours >= len(COPILOT_HOOK_EVENTS),
                        f"agentihooks.json wires {ours} entries across {len(events)} events",
                    )
                )
            except json.JSONDecodeError as exc:
                checks.append((False, f"agentihooks.json unparseable: {exc}"))
        else:
            checks.append((False, f"{hooks_path} missing"))
        checks.append((wrapper.exists() and os.access(wrapper, os.X_OK), f"hook wrapper executable ({wrapper})"))

        persona = home / "copilot-instructions.md"
        if persona.exists():
            text = persona.read_text()
            checks.append((_MANAGED_HEADER in text, "copilot-instructions.md is agentihooks-managed"))
            checks.append((_MANAGED_FOOTER in text, "copilot-instructions.md has its managed-end marker"))
        else:
            checks.append((False, "copilot-instructions.md missing"))

        skills = agents_skills_home()
        n_skills = len(list(skills.iterdir())) if skills.is_dir() else 0
        checks.append((n_skills > 0, f"{n_skills} skill(s) in {skills}"))

        oversize = oversized_skill_descriptions(skills)
        checks.append(
            (
                not oversize,
                f"skill descriptions within copilot's {_SKILL_DESCRIPTION_MAX}-char cap"
                if not oversize
                else f"{len(oversize)} skill(s) over the {_SKILL_DESCRIPTION_MAX}-char description cap "
                f"— copilot drops them: {', '.join(f'{n} ({c})' for n, c in oversize)}",
            )
        )

        agents_dir = home / "agents"
        n_agents = len([f for f in agents_dir.glob("*.md")]) if agents_dir.is_dir() else 0
        checks.append((True, f"{n_agents} custom agent(s) in {agents_dir}"))

        checks.append((shutil.which("copilot") is not None, "`copilot` binary on PATH"))

        failed = 0
        for ok, msg in checks:
            print(f"  [{'OK' if ok else '!!'}] {msg}")
            if not ok:
                failed += 1
        return failed

    # ------------------------------------------------------------------
    # JSON round-trip (operator hand-edits outside managed keys survive)
    # ------------------------------------------------------------------

    @staticmethod
    def _load_json(path: Path) -> dict:
        if path.exists():
            try:
                loaded = json.loads(path.read_text())
                if isinstance(loaded, dict):
                    return loaded
            except (json.JSONDecodeError, OSError):
                backup = path.with_suffix(f".json.bak.{datetime.now(timezone.utc):%Y%m%d%H%M%S}")
                shutil.copy2(path, backup)
                print(f"  [!!] Unparseable {path.name} backed up → {backup}")
        return {}
