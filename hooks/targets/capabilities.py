"""Per-(target, event) hook capabilities.

What a hook process is allowed to say back to its host CLI differs by target.
Handlers consult this map instead of assuming Claude Code semantics.

Codex specifics (docs/reference/CODEX-COMPAT.md §2.4, verified against codex-cli 0.147.0):
- ``PreToolUse`` output supports ``permissionDecision: "deny"`` only — no
  allow/ask, and no ``additionalContext`` injection.
- Blocking via exit code 2 + stderr works on every event, same as Claude.

Copilot specifics (docs/reference/COPILOT-COMPAT.md, verified against @github/copilot 1.0.79-6):
- ``PreToolUse`` is a superset of Claude's: allow/deny/ask, plus
  ``additionalContext`` and ``modifiedArgs`` argument mutation.
- Exit code 2 is NOT a universal block: the runtime carries the string
  "Hook command exited with code 2 (warning)" while fail-closed denial is
  documented for ``preToolUse`` only. The JSON decision envelope is therefore
  the primary block channel for copilot; exit 2 is secondary.
"""

from __future__ import annotations

from hooks.targets import current_target

# Events whose stdout JSON may carry hookSpecificOutput.additionalContext.
_CODEX_NO_CONTEXT_EVENTS = frozenset({"PreToolUse"})

# Values legal in hookSpecificOutput.permissionDecision per target.
_PERMISSION_DECISIONS = {
    "claude": frozenset({"allow", "deny", "ask"}),
    "codex": frozenset({"deny"}),
    "copilot": frozenset({"allow", "deny", "ask"}),
}

# Targets whose PreToolUse channel can rewrite the tool's arguments in flight,
# and the envelope field each one reads the replacement from.
_ARG_MUTATION_TARGETS = frozenset({"claude", "copilot"})
_ARG_MUTATION_FIELD = {"claude": "updatedInput", "copilot": "modifiedArgs"}

# Targets that cannot be trusted to treat exit code 2 as a block on every
# event, so a guardrail must also state its denial in the stdout envelope.
_ENVELOPE_BLOCK_TARGETS = frozenset({"copilot"})

# Targets that load a Claude-shaped repo's own .claude tree themselves. Claude
# Code loads every <repo>/.claude/rules body and the project memory at
# SessionStart (measured: 37 files, 252,291 bytes), so the project bridge would
# only duplicate it there.
_CLAUDE_PROJECT_TREE_TARGETS = frozenset({"claude"})

# Targets whose repo-scope skill root is NOT .claude/skills, so the bridge has
# to expose it under the name they do scan. Copilot scans .claude/skills
# directly (`copilot skill --help`, 1.0.83); codex scans <repo>/.agents/skills.
_REPO_SKILLS_LINK_TARGETS = frozenset({"codex"})


def can_inject_context(event: str, target: str | None = None) -> bool:
    target = target or current_target()
    if target == "codex" and event in _CODEX_NO_CONTEXT_EVENTS:
        return False
    return True


def allowed_permission_decisions(target: str | None = None) -> frozenset[str]:
    target = target or current_target()
    return _PERMISSION_DECISIONS.get(target, _PERMISSION_DECISIONS["claude"])


def supports_arg_mutation(target: str | None = None) -> bool:
    return (target or current_target()) in _ARG_MUTATION_TARGETS


def arg_mutation_field(target: str | None = None) -> str | None:
    return _ARG_MUTATION_FIELD.get(target or current_target())


# Events where a stdout decision envelope is meaningful. Emitting one on,
# say, SessionEnd would be noise the host has no field for.
_DECISION_EVENTS = frozenset({"PreToolUse", "PermissionRequest"})

# Copilot v1.0.80, settled live: preToolUse is fail-closed on any non-zero
# exit and IGNORES stdout envelopes; userPromptSubmitted is the inverse —
# exit code advisory, blocked only by a stdout {"decision": "block"} object.
# The envelope is therefore emitted on both kinds of event: belt-and-braces
# where exit 2 already denies, the ONLY block channel on UserPromptSubmit.
_COPILOT_BLOCKABLE_EVENTS = _DECISION_EVENTS | {"UserPromptSubmit"}


# Documented per-(target, event) ceilings on additionalContext, in bytes.
# Copilot joins every postToolUse hook's context with a blank line and caps the
# result at 10 KB, dropping the remainder without a notice — a filtered command
# output past that point would vanish mid-sentence.
_CONTEXT_CAPS = {("copilot", "PostToolUse"): 10 * 1024}


def context_cap_bytes(event: str, target: str | None = None) -> int | None:
    return _CONTEXT_CAPS.get(((target or current_target()), event))


def loads_claude_project_tree(target: str | None = None) -> bool:
    """Whether the host already loads <repo>/.claude itself."""
    return (target or current_target()) in _CLAUDE_PROJECT_TREE_TARGETS


def needs_repo_skills_link(target: str | None = None) -> bool:
    """Whether <repo>/.claude/skills must be exposed under another name."""
    return (target or current_target()) in _REPO_SKILLS_LINK_TARGETS


def requires_envelope_block(event: str, target: str | None = None) -> bool:
    """Whether a denial must ALSO be stated in stdout JSON, not just via exit 2."""
    if (target or current_target()) not in _ENVELOPE_BLOCK_TARGETS:
        return False
    return event in _COPILOT_BLOCKABLE_EVENTS
