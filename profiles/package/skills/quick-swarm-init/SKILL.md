---
name: quick-swarm-init
description: >
  Start a quick swarm in the current herdr tab: this session becomes the master
  in pane 1 and up to four engineers open in the panes beside it, with all
  state in one scratchpad folder and no Redis or ledger server. Use when the
  operator says "start a quick swarm", "quick swarm", "open a quick swarm with
  N agents" or "quick swarm with codex engineers".
argument-hint: "[--agents N] [--harness claude|codex|list]"
---

# Quick Swarm Init

A quick swarm is one herdr tab: the master in pane 1, engineers in panes 2 to 5.
It runs only inside herdr; outside it the command refuses and says why.

1. Start it from this session's pane:

   `agentihooks quick up`

   Four agents by default. `--agents 5` for five, `--agents 2` for two. One
   harness for every engineer with `--harness codex`, or one per engineer in
   pane order with `--harness claude,codex,claude`. Done when the command
   prints the quick id, the folder and one line per seat.

2. Read the seats:

   `agentihooks quick status`

   Each line names the seat, its pane, its harness and its session name.

3. You are now the master. Follow the quick swarm block injected into this
   session: tell each engineer to wait for the operator, track each seat
   folder, and reach seats with SendMessage (Claude to Claude) or
   `agentihooks quick send <seat> "<text>"`. Read what seats sent you with
   `agentihooks quick room`.

4. When the operator ends the quick swarm:

   `agentihooks quick stop`

   It removes the room and the registry. Seat folders stay for review.

If `quick up` refuses because herdr is missing, not running or this session is
outside a herdr pane, report the refusal word for word and stop.
