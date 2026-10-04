---
name: terminate-agent
description: >
  List or terminate a live Claude Code or Codex agent by exact session name,
  UUID, or PID. Use when the operator says "terminate agent", "kill agent", "stop agent",
  "kill Claude session", "kill Codex session", or asks which agent sessions
  are running before terminating one.
argument-hint: "[NAME|UUID|PID] [--type claude|codex|any] [--list] [--dry-run]"
---

# Terminate Agent

`agentihooks terminate-agent` owns process discovery, exact matching, PID-reuse
checks, process-group validation, caller protection, signal escalation, and
exit verification. Do not construct `ps`, `pgrep`, `pkill`, or `kill` commands.

## List

```bash
agentihooks terminate-agent --list --type any
```

Use `--type claude` or `--type codex` when requested. Report the table exactly.

## Resolve safely

Run the deterministic dry-run before every termination:

```bash
agentihooks terminate-agent "<exact-name-or-uuid>" --type <claude|codex|any> --dry-run
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
agentihooks terminate-agent "<exact-name-or-uuid>" --type <claude|codex|any>
```

The command sends SIGTERM, waits a bounded interval, revalidates identity before
SIGKILL when required, and verifies every captured process exited. Exit zero
with `result=terminated` is the completion criterion. Report whether SIGKILL
was required.

An agent running in a herdr pane also prints `pane_id=<id> pane=closed`: its
pane (and the tab, when it was the only pane) is closed after the agent exits.
Report the `pane_id`. When the operator wants the pane kept ("stop it but keep
the terminal"), add `--keep-pane`; the line then reads `pane=kept`.
`pane=close-failed (...)` means the agent is gone but the pane is still open.

## Extracted Scripts

| Primitive | Purpose |
|---|---|
| `agentihooks terminate-agent` | Deterministic list, resolution, validation, dry-run, termination, and verification |
