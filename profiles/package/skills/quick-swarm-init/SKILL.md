---
name: quick-swarm-init
description: >
  Start a quick swarm in the current herdr tab: this session becomes the master
  in pane 1 and up to four engineers open in the panes beside it, with all
  state in one scratchpad folder and no Redis or ledger server. Use when the
  operator says "start a quick swarm", "quick swarm", "open a quick swarm with
  N agents" or "quick swarm with codex engineers".
argument-hint: "[--agents N] [--harness claude|codex|program|list] [--auth routed|default|named:reference|list] [--command JSON] [--engineer-command N=JSON]"
---

# Quick Swarm Init

A quick swarm is one herdr tab: the master in pane 1, engineers in panes 2 to 5.
It runs only inside herdr; outside it the command refuses and says why.

1. Start it from this session's pane:

   `agentihooks quick up`

   `--agents` counts the master and engineers together: two engineers need
   `--agents 3`, three engineers need `--agents 4`. Four agents by default.
   `--agents 5` for four engineers, `--agents 2` for one. One
   harness for every engineer with `--harness codex`, or one per engineer in
   pane order with `--harness claude,codex,claude`.

   Authentication defaults to `--auth routed`. Use `--auth default` when the
   operator requests each native CLI's local login outside automatic quota
   tiers. Use `--auth named:reference` for an existing account suffix or token
   environment variable name. One choice applies to every engineer; a comma
   separated list gives one choice per engineer in pane order. For mixed
   Claude and Codex engineers, use references belonging to each harness.
   Pass references only. A missing reference is a refusal, with no routed
   fallback. Keep the current master login and global credentials unchanged.

   For saved local commands, pass literal JSON argv with `--command`, or
   repeat `--engineer-command` with `N=JSON` for engineer N, excluding the
   master. Example: `agentihooks quick up --agents 3 --harness claude,program --auth default --command '["/opt/local/claude-wrapper"]' --engineer-command '2=["/usr/bin/tail","-f","/opt/local/events.log"]'`.
   The command validates every executable before creating panes. Shell
   characters remain literal; credentials stay in the program's environment.
   Claude and Codex wrappers receive generated native arguments after their
   saved argv prefix, plus `AGENTIHOOKS_QUICK_PROMPT_FILE`. They must forward
   those arguments and the native profile and authentication environment.
   A `program` seat requires a command, uses process identity, and receives
   no agent prompt typing or automatic conversational handoff. Room messages
   remain stored. Relaunch and retry reuse the saved recipe.

   Done when the command
   prints the quick id, the folder and one line per seat.

2. Read the seats:

   `agentihooks quick status`

   Each line names the seat, its pane, its harness, its session name and its
   saved `auth` and `account`, safe command identity and lifecycle capabilities. Default and named modes stay outside automatic
   tiers through handoff, forced relaunch and retries. Done when every
   engineer's saved selection matches the request.

3. You are now the master. Load the `quick-swarm-master` skill for how the
   swarm is built, how a seat launches and registers, how messages travel and
   how to troubleshoot or force a handoff. Follow the quick swarm block
   injected into this session: tell each engineer to wait for the operator,
   track each seat folder, and reach seats with SendMessage (Claude to
   Claude) or `agentihooks quick send <seat> "<text>"`. Read what seats sent
   you with `agentihooks quick room`.

## Hard rule: never trust a pane message as delivered

The operator, 24 November: "never fucking never trust in a pane message being
delivered you need to double check FUCKING ALWAYS THIS IS A MASTER RULE."

After typing a message into a seat's pane (herdr tab then Enter), read that
pane back and confirm the seat started working on it or queued it. If the text
still sits unsent in the input box, press Enter again and recheck. If it still
cannot be confirmed, report NOT SENT and send the message through the room
instead with `agentihooks quick send <seat> "<text>"`.

4. When the operator ends the quick swarm:

   `agentihooks quick stop`

   It removes the room and the registry. Seat folders stay for review.

If `quick up` refuses because herdr is missing, not running or this session is
outside a herdr pane, report the refusal word for word and stop.
