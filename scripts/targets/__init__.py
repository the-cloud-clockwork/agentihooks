"""Install-target abstraction — which agent CLI agentihooks installs into.

A *target* is the agent CLI whose config surface ``agentihooks init`` writes:
``claude`` (Claude Code, ``~/.claude``), ``codex`` (OpenAI Codex CLI,
``~/.codex``) or ``copilot`` (GitHub Copilot CLI, ``~/.copilot``). Profile
resolution, bundle linking, settings merging and MCP server-dict assembly are
target-agnostic and stay in ``scripts.install``; everything that touches a
target-specific path or schema goes through the adapter returned by
:func:`get_adapter`.

Design docs: ``docs/reference/CODEX-COMPAT.md`` (§4),
``docs/reference/COPILOT-COMPAT.md`` (§4).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Protocol, Sequence

SUPPORTED_TARGETS = ("claude", "codex", "copilot")
DEFAULT_TARGET = "claude"


def resolve_target(
    cli_value: str | None,
    stored: str,
    installed: Sequence[str] = (),
    *,
    interactive_ok: bool = True,
) -> str:
    """Resolve the install target.

    Precedence: CLI flag > ``AGENTIHOOKS_TARGET`` env > exactly-one-installed-
    target recall (a codex-only machine keeps recalling codex with no stored
    hint needed) > interactive TTY prompt (defaulting to *stored* when it
    names a valid target, else :data:`DEFAULT_TARGET`) > non-interactive
    resolution with multiple installed targets (warns to stderr, naming the
    ambiguity and the ``--target`` flag, then falls back to
    :data:`DEFAULT_TARGET`) > :data:`DEFAULT_TARGET`.

    *stored* only ever supplies the TTY prompt's default now — it never wins
    a non-interactive resolution by itself. That is deliberate: a single
    last-writer-wins ``install_target`` key can't be trusted to speak for
    *which* target's record a bare, non-interactive ``init`` should reinstall
    when more than one is on record. *installed* carries that instead.
    Invalid *cli_value*/env values still hard-exit; an invalid *stored* value
    just loses its prompt-default role.
    """
    candidate = (cli_value or "").strip()
    if not candidate:
        candidate = os.environ.get("AGENTIHOOKS_TARGET", "").strip()
    if not candidate:
        installed_targets = tuple(installed)
        if len(installed_targets) == 1:
            candidate = installed_targets[0]
        elif interactive_ok and sys.stdin.isatty():
            stored_norm = (stored or "").strip().lower()
            prompt_default = stored_norm if stored_norm in SUPPORTED_TARGETS else DEFAULT_TARGET
            options = "/".join(SUPPORTED_TARGETS)
            candidate = input(f"Target ({options}) [{prompt_default}]: ").strip() or prompt_default
            if candidate.lower() not in SUPPORTED_TARGETS:
                # Interactive typo: warn and fall back rather than killing the
                # whole init. Flag/env values below still fail hard.
                print(f"  [WARN] Unknown target '{candidate}' — using '{DEFAULT_TARGET}'.")
                candidate = DEFAULT_TARGET
        else:
            if len(installed_targets) > 1:
                print(
                    f"WARNING: multiple install targets found ({', '.join(sorted(installed_targets))}) — "
                    f"ambiguous bare init, defaulting to '{DEFAULT_TARGET}'. Pass --target to disambiguate.",
                    file=sys.stderr,
                )
            candidate = DEFAULT_TARGET
    candidate = candidate.lower()
    if candidate not in SUPPORTED_TARGETS:
        print(
            f"ERROR: Unknown target '{candidate}'. Supported: {', '.join(SUPPORTED_TARGETS)}",
            file=sys.stderr,
        )
        sys.exit(1)
    return candidate


class TargetAdapter(Protocol):
    """Target-specific write surface consumed by ``_install_global_inner``."""

    name: str

    def home(self) -> Path:
        """Config root this target reads (``~/.claude`` / ``~/.codex`` / ``~/.copilot``)."""
        ...

    def write_settings(self, native: dict) -> Path:
        """Persist the merged settings for this target; returns the file written.

        *native* is authored in THIS target's own format — Claude's
        ``settings.json`` shape, codex's ``config.toml`` shape, copilot's
        ``settings.json`` shape — already merged across base → bundle →
        profile chain. It is not a Claude document to be translated.
        """
        ...

    def install_features(self, subdir: str, layers: list[tuple[str, Path]], filter_fn) -> None:
        """Install one feature kind (skills/agents/commands/rules) from its
        resolved source layers. Claude symlinks all four into ``~/.claude``;
        codex symlinks skills to ``~/.agents/skills``, translates commands to
        ``~/.codex/prompts``, compiles rules into AGENTS.md, and skips agents;
        copilot symlinks skills to ``~/.agents/skills``, translates commands
        into skills there, translates agents to ``~/.copilot/agents``, and
        compiles rules into copilot-instructions.md."""
        ...

    def install_persona(
        self, profile_dirs: list[tuple[str, Path]], profile_chain: list[str], bundle_dir: Path | None
    ) -> None:
        """Assemble and write the persona/system-prompt file for this target."""
        ...

    def register_hooks_utils(self, profile_name: str) -> None:
        """Register agentihooks' own MCP server with the target."""
        ...

    def register_mcp(self, servers: dict) -> None:
        """Merge a layer of MCP server definitions into the target's config."""
        ...

    def post_install_reconcile(self, profile_chain: list[str], persisted_profile: str) -> None:
        """Target-specific end-of-install bookkeeping (ledgers, snapshots)."""
        ...

    def teardown(self) -> None:
        """Remove every artifact this adapter wrote, preserving operator content.

        Claude's teardown lives in ``uninstall_global`` (it predates the
        adapter seam); the non-claude adapters implement it here so a full
        uninstall reaches all installed targets. Must be idempotent — running
        against a clean home is a no-op, never an error."""
        ...


def get_adapter(target: str) -> TargetAdapter:
    if target == "claude":
        from scripts.targets.claude_target import ClaudeAdapter

        return ClaudeAdapter()
    if target == "codex":
        from scripts.targets.codex_target import CodexAdapter

        return CodexAdapter()
    if target == "copilot":
        from scripts.targets.copilot_target import CopilotAdapter

        return CopilotAdapter()
    raise ValueError(f"Unknown target: {target}")
