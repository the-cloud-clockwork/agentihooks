---
title: "Runtime Overlays"
parent: The Four Pillars
nav_order: 5
---

# Runtime Overlays
{: .no_toc }

**Change who your agent is — mid-session, zero restart.**

> Profiles are powerful. But until now, switching profiles meant restarting the session. Overlays break that wall. An agent can shift its own personality, rules, and constraints on the fly — then shift back — without losing a single line of context.

## Table of contents
{: .no_toc .text-delta }

1. TOC
{:toc}

---

## The problem overlays solve

Claude Code reads `CLAUDE.md` and `~/.claude/rules/` **once at session start**. That's it. If you run `agentihooks init --profile X` mid-session, the new files are written but Claude never re-reads them. You'd have to restart — losing your entire conversation context, tool memory, and in-flight work.

Overlays work around this limitation entirely. Instead of rewriting static files, they inject profile content **into the conversation stream itself**, on every user turn, using the same hook system that powers context refresh and broadcast delivery.

The result: your agent can adopt an entirely new behavioral profile in under a second, and release it just as fast.

---

## How it works

```
┌─────────────────────────────────────────────────┐
│  Session running on: anton (base profile)       │
│                                                 │
│  Agent calls:  overlay_add("patch-mode")        │
│       ↓                                         │
│  State file written:                            │
│  ~/.agentihooks/active_overlays.json            │
│       ↓                                         │
│  Next user turn:                                │
│  UserPromptSubmit hook reads state file          │
│  → injects patch-mode CLAUDE.md + rules          │
│  → agent now sees BOTH profiles in context       │
│                                                 │
│  Agent calls:  overlay_remove("patch-mode")     │
│       ↓                                         │
│  State file cleared. Next turn: back to anton.  │
└─────────────────────────────────────────────────┘
```

Overlays are **rendered once** when added — the profile's `CLAUDE.md` and all `rules/*.md` files are concatenated and cached in the state file. The per-turn hook just reads and injects. No file walking, no I/O churn, no latency.

---

## The dance: base + overlay

This is where overlays get interesting. Consider two profiles with complementary philosophies:

| Profile | Philosophy | Scope | Autonomy |
|---------|-----------|-------|----------|
| **anton** | "I am the operator's general-purpose identity" | All domains, all tasks | Full — pushes main, deploys, destroys |
| **patch-mode** | "Make it work live, git catches up" | One service, one fix | Throttled — never declares done, operator-gated commits |

These profiles have **tension**. Anton auto-commits and auto-rebuilds images. Patch-mode forbids commits until the operator says so. Anton works across the whole fleet; patch-mode locks onto a single target.

That tension is the point. When you overlay `patch-mode` onto `anton`, the agent gets **both sets of rules**. The more specific, restrictive rules from patch-mode naturally override anton's general autonomy — because Claude prioritizes the most recent context injection. When you remove the overlay, the agent returns to its normal operating posture.

This is **call and response**. Tension and release. The agent shifts between modes without losing any of the session context that brought it there.

```bash
# Operator notices a broken service
# Agent is running on anton (default)

# Enter surgical mode:
overlay_add("patch-mode")
# → Agent now has patch-mode's rules: investigate first, validate deterministically,
#   don't commit until told, circuit breaker on failures

# Fix lands, operator validates
overlay_remove("patch-mode")
# → Agent returns to anton: full autonomy, auto-commit, image rebuild in parallel
```

---

## MCP tools

Agents can manage their own overlays via the `hooks-utils` MCP server. These tools are available inside any Claude Code session:

| Tool | What it does |
|------|-------------|
| `profile_list` | List all profiles and which overlays are allowed by the base profile |
| `profile_current` | Show base profile + active overlays + effective chain |
| `overlay_add(name)` | Activate an overlay. Enforces the whitelist. Takes effect next turn. |
| `overlay_remove(name)` | Deactivate an overlay. Takes effect next turn. |
| `overlay_refresh(name)` | Re-render overlay content from disk (after bundle changes). |

### Example: agent self-manages

```
Agent:  I need to do a live patch on the auth service.
        Let me enter patch-mode for focused troubleshooting.

        → calls overlay_add("patch-mode")

Agent:  [now operating under patch-mode rules]
        Investigating... running diagnostics... applying fix...
        Validation passed.

Operator: good, integrate it

Agent:  → calls overlay_remove("patch-mode")
        → commits to dev, pushes
        → kicks off image rebuild (back to anton's religion)
```

---

## The whitelist

Not every profile should be overlayable. An operator might have admin-class profiles with elevated privileges, or experimental profiles that aren't ready for agent-initiated use.

The **base profile** controls which overlays are allowed via a field in `profile.yml`:

```yaml
# profiles/anton/profile.yml
name: anton
allowedOverlays:
  - patch-mode
  - router
```

Any `overlay_add` call — whether from an MCP tool or the CLI — checks this list first. If the requested overlay isn't in `allowedOverlays`, the call fails with a clear error naming the allowed set.

```
overlay_add("agenticore")
→ {"success": false, "error": "'agenticore' not in allowedOverlays. Allowed: ['patch-mode', 'router']"}
```

This means the operator controls the blast radius. The agent can shift between approved modes but can't escalate itself to profiles it wasn't designed to use.

---

## State file

Active overlays are stored in `~/.agentihooks/active_overlays.json`:

```json
{
  "base_profile": "anton",
  "overlays": [
    {
      "name": "patch-mode",
      "added_at": "2026-04-10T14:30:00+00:00",
      "added_by": "agent",
      "rules_content": "# PATCH-MODE — Make It Work Live\n\n..."
    }
  ]
}
```

The `rules_content` field is the pre-rendered concatenation of the overlay profile's `CLAUDE.md` + all `rules/*.md`. It's cached at add-time so the per-turn hook has zero filesystem overhead.

---

## Injection format

On every `UserPromptSubmit`, the overlay injector outputs:

```
=== OVERLAY ACTIVE: patch-mode ===
# PATCH-MODE — Make It Work Live
...all rules content...
=== END OVERLAY: patch-mode ===
```

This lands in Claude's context as additional context — the same mechanism used by context refresh, broadcast delivery, and guardrail warnings. Multiple overlays stack: each gets its own block.

---

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `OVERLAY_INJECTION_ENABLED` | `true` | Master switch for overlay injection |

Set in `~/.agentihooks/.env`. When disabled, overlays can still be added/removed (state file is updated) but nothing is injected into the session.

---

## Design decisions

**Why not just re-run `agentihooks init`?**

Because `CLAUDE.md` and `rules/` are read once at session start. A mid-session reinstall rewrites the files but Claude never sees the changes. The overlay system bypasses this by injecting directly into the conversation stream.

**Why render at add-time instead of per-turn?**

Performance. Walking the bundle filesystem on every user turn would add I/O latency to the critical path. Rendering once and caching in the state file keeps the per-turn cost to a single JSON read.

**Why a whitelist instead of a blocklist?**

Fail-safe design. New profiles are invisible to agents by default. The operator explicitly opts profiles into the overlay system. This prevents accidental exposure of experimental or privileged profiles.

**Can overlays conflict with the base profile?**

Yes, intentionally. When rules conflict, the overlay wins because it's injected later in the context — Claude prioritizes recent instructions. When the overlay is removed, the base profile's rules are the only ones visible. This is the "tension and release" model: the overlay temporarily overrides, then cleanly releases.

---

*Next: Learn how all four pillars work together in the [Architecture Reference](../reference/).*
