---
description: Operator clearance levels — full clearance is the default
alwaysApply: true
---

# Clearance Levels

## Default: Full Clearance

The operator grants **full clearance** by default. This means:

- All git operations: push to any branch (including main), force push, branch delete, merge
- All destructive ops: `rm -rf`, `kubectl delete`, `docker rm`, `DROP TABLE` — proceed without asking
- All deploy ops: production restarts, rollouts, scaling — proceed without asking
- All infrastructure ops: SSH, SCP, service management — proceed without asking

**The only exception at ANY clearance level: secrets.** Never handle credentials, API keys, tokens, or passwords in plaintext. Ever.

## Restricting Clearance

The operator can REDUCE permissions with a verbal command:

| Command | Effect |
|---|---|
| "restrict clearance" / "careful mode" | Main branch protected. Destructive ops require confirmation. Production restarts require confirmation. |
| "full clearance" / "back to normal" | Returns to default full clearance. |

## Rules

- Full clearance is the DEFAULT. You do not need the operator to say "full clearance" to have it.
- Restricted mode is temporary — it applies for the current task only, then reverts to full clearance.
- Secrets are NEVER bypassed at any clearance level. This is the one absolute.
- When operating in restricted mode, prefix actions that would normally be blocked with: `[restricted — asking]`
- The hook system may still enforce its own blocks (e.g., main branch protection hooks). Clearance governs YOUR behavior for decisions hooks don't catch.
