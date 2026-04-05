---
description: Operator clearance levels for temporarily elevating permissions
alwaysApply: true
---

# Clearance Levels

The operator can temporarily elevate permissions with a verbal command. Clearance applies for the current task only — it resets after the task is complete or the operator says "clearance revoked" / "back to normal".

| Command | Level | What it unlocks |
|---|---|---|
| "clearance level 0" / "full clearance" | 0 (unrestricted) | All git operations including push to main, force push, branch delete. Destructive ops (rm -rf, kubectl delete) proceed without asking. No guardrail blocks except secrets. |
| "clearance level 1" | 1 (elevated) | Git push to main, git merge to main, gh pr merge — allowed. Still asks before destructive non-git ops (rm -rf, kubectl delete, docker rm). |
| "clearance revoked" / "back to normal" | default | Normal delegation rules apply. Main branch is protected. Destructive ops require permission. |

**Rules:**
- Clearance is granted per-task, not per-session. Once the immediate task is done, revert to default.
- Secrets are NEVER bypassed at any clearance level.
- When operating at clearance 0 or 1, prefix your first action with: `[clearance {level}]` so the operator can see it's elevated.
- If the operator hasn't granted clearance, NEVER assume it. The hook system will still block main branch operations regardless — clearance only changes YOUR behavior for operations the hooks don't catch (like asking for confirmation).
