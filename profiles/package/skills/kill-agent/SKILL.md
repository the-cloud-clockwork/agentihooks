---
name: kill-agent
description: >
  List or terminate a live Claude Code or Codex agent by exact session name,
  UUID, or PID. Use when the operator says "kill agent", "stop agent",
  "kill Claude session", "kill Codex session", or asks which agent sessions
  are running before terminating one.
argument-hint: "[NAME|UUID|PID] [--type claude|codex|any] [--list] [--dry-run]"
---

# Kill Agent

`agentihooks kill-agent` owns process discovery, exact matching, PID-reuse
checks, process-group validation, caller protection, signal escalation, and
exit verification. Do not construct `ps`, `pgrep`, `pkill`, or `kill` commands.

## List

```bash
agentihooks kill-agent --list --type any
```

Use `--type claude` or `--type codex` when requested. Report the table exactly.

## Resolve safely

Run the deterministic dry-run before every termination:

```bash
agentihooks kill-agent "<exact-name-or-uuid>" --type <claude|codex|any> --dry-run
```

Exit zero with `result=validated signal=none` is the completion criterion. A
duplicate name requires the operator to select one UUID. A Codex process shared
by multiple UUIDs requires the operator to choose whether all listed sessions
may be terminated; only then add `--force-shared` to both commands.

If the operator requested only a dry-run, report the resolved PID, PGID, member
PIDs, and `signal=none`, then stop.

## Terminate

After a successful dry-run, reuse the exact selector and options:

```bash
agentihooks kill-agent "<exact-name-or-uuid>" --type <claude|codex|any>
```

The command sends SIGTERM, waits a bounded interval, revalidates identity before
SIGKILL when required, and verifies every captured process exited. Exit zero
with `result=terminated` is the completion criterion. Report whether SIGKILL
was required.

## Extracted Scripts

| Primitive | Purpose |
|---|---|
| `agentihooks kill-agent` | Deterministic list, resolution, validation, dry-run, termination, and verification |
