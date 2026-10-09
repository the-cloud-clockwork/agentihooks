---
name: take-master
description: >
  Make this session the master of a swarm ledger, Claude or Codex, opened by
  hand. Use when the operator says "you are the master of ledger SLUG", "you
  are the master now", "take the master seat", "take master of SLUG", or
  "become the master of this ledger".
argument-hint: "<slug> [--replace]"
---

# Take Master

`agentihooks swarm <slug> take-master` seats the session that runs it as the
swarm's master. It names an unnamed session `master@<code>-<n>`, occupies the
master seat, reopens a closed ledger, sets a stopped swarm running and prints
the full master priming.

## Run

Run it inside this session's own shell, never through another agent:

```bash
agentihooks swarm <slug> take-master
```

- Exit 1 with `is the live master` means another master is running. Run again
  with `--replace` only when the operator asked to replace it; otherwise report
  the name and stop.
- Exit 1 with `not registered` means the hooks did not register this session;
  report it and stop.

## Act on the priming

Exit 0 prints the priming a spawned master gets. Read it in full and follow it
as your standing instructions from now on: the seat handoff, the culture, the
recaps, the learned notes, the ledger summary and the standing duties. Pass
`--as <your master name>` on every `agentihooks swarm` and `agentihooks ledger`
command it names.

Every plan you publish puts each phase under a heading with its exact title,
each task under a heading one level deeper, and one unique
`<!-- slice: <id> -->` anchor naming the task immediately before its heading.
Publish it with
`agentihooks ledger --slug <slug> --as <name> publish-plan <plan-file> --phase <phase-ids>`
and add every task built from it with `--plan-slice <id>`, so its agent reads
only its chunk.

Completion criterion: the priming is printed, `agentihooks swarm <slug> status`
lists this session's master name once, and its first ledger chat line follows
the priming.
