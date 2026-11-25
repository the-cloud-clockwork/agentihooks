---
name: quick-swarm-master
description: >
  The quick swarm master's operating manual: how a quick swarm is built in one
  herdr tab, how a seat launches and registers through init-agent, how
  messages travel between seats, how to troubleshoot a seat, and how to force
  a handoff of an engineer seat or the master's own seat. Every quick master
  loads this before any other action, on its first launch and on every
  successor launch. Use when a quick swarm master starts or hands off, or when
  the operator says "how does the quick swarm work", "troubleshoot the quick
  swarm" or "force a quick seat handoff".
---

# Quick Swarm Master Manual

Load this before any other action, on your first launch and on every
successor's.

## How a quick swarm is built

One herdr tab: the master in pane 1, up to four engineers in the panes to its
right (`--agents 5` for five, `--agents 2` for two), each an equal column.
Everything a quick swarm keeps lives in one scratchpad folder, named after the
session that ran `quick up`: `~/scratchpad/<repo>/quick-<session8>/`. Inside
it:

- `seats.json` — the registry: every seat's pane, harness, session name,
  session id and state (`ready`, `handoff` meaning pending, or `launching`),
  written under a file lock.
- `seats/<seat>/` — `plan.md` (checkbox stages), `notes.md`, `learned.md`,
  `handoff.md` and `opening.md` (the prompt its latest launch opened with).
- `room/chat.jsonl` — one JSON line per message between seats that share no
  native channel; each seat keeps its own read cursor.
- `culture.md` — text every seat's injected block carries.

`~/.agentihooks/quick-swarms.json` is the host-wide index, quick id to folder,
so a session can be found by its herdr pane alone when its launch variables
are missing (`scripts/quick/member.py::_by_pane`).

## How a seat launches and registers

`quick up` and `quick relaunch` both run through `agentihooks init-agent`
(`scripts/quick/launch.py::init_agent`), which writes a fresh, self-deleting
launcher script into the runtime directory and runs it in the seat's herdr
pane. The launcher exports the seat's identity, then its quick variables —
`AGENTIHOOKS_QUICK_SWARM`, `AGENTIHOOKS_QUICK_SEAT`, `AGENTIHOOKS_QUICK_DIR`
(`scripts/quick/member.py`'s `QUICK_ENV` / `SEAT_ENV` / `DIR_ENV`) — plus
`AGENTIHOOKS_AGENT_NAME` and `AGENTIHOOKS_TERMINAL_LAUNCH=1`; herdr itself sets
`HERDR_AGENT` for the pane it starts. A seat's agent registers itself into
`seats.json` (`registry.bind`) only when it reads those quick variables and
the seat was `launching`.

A plain `codex` or `claude` typed straight into a pane carries none of that:
it never binds into the registry, so the seat stays recorded exactly as it
was before and nothing new occupies it as far as the swarm is concerned.
`agentihooks quick relaunch <seat> --folder <folder> --adopt`, run from inside
the hand-started session's own pane, binds that live session and pane as the
seat's ready occupant instead; it refuses when the seat is mid-launch, when
another seat already holds that pane, or when the seat's previously recorded
pane still runs a live agent.

## How messages travel

`agentihooks quick send <seat> "<text>"` posts to the room
(`scripts/quick/room.py::post`); `agentihooks quick room` reads and marks read
every line sent to the caller's seat. Two Claude seats other than the master
reach each other with SendMessage instead. The quick tick tells an idle seat
with nothing typed in its pane that lines are waiting for it, but it never
assumes a message landed.

Never trust that a pane message was delivered (operator order, 24 November):
after typing into a seat's pane, read the pane back and confirm the seat
started on the message or queued it. If the text still sits unsent, press
Enter again and recheck. If delivery still cannot be confirmed, report NOT
SENT and send the message through the room instead with
`agentihooks quick send <seat> "<text>"`.

## Troubleshooting

`agentihooks quick status` prints every seat's registry row, one JSON line
per seat: pane, harness, session name, session id and state. A seat is bound
when its session id is non-empty and its state reads `ready`; `handoff` means
pending, a successor not yet started or not yet bound.

An old launcher path copied from shell history or a log almost never still
exists: the launcher script removes itself (`rm -f`) once the agent running
inside it exits, so the path is gone the moment that happens.
`agentihooks gc`, and the swarm's own run folder cleanup, also sweep away any
stale launcher, route, prompt and started-marker file older than 24 hours on
their own, whether or not an agent ever ran in it
(`scripts/herdr_gc.py::clean_run_dir`, `RUN_FILE_AGE_S`).

## Forcing a handoff

Of your own seat: follow the `quick-swarm-handoff` skill — write the Handoff
v2 into the seat's handoff file, record any lesson, tick finished stages, then
run `agentihooks quick handoff`. It ends this session at its next stop with
its pane kept, and the successor opens in the same pane from the handoff.

Of an engineer's seat: `agentihooks quick relaunch <seat> --folder <folder>
--force`. It ends the engineer's live but unusable agent with its pane kept,
then starts its successor the same way `quick up` did.

The master's own seat has no such force today: on current dev,
`agentihooks quick relaunch master --folder <folder> --force` returns
`master` at once and starts nothing (`scripts/quick/launch.py::force`), and
the quick tick never forces the master either. Start a master successor by
hand instead: open a new session in the master's pane with its quick
variables set (`AGENTIHOOKS_QUICK_SWARM`, `AGENTIHOOKS_QUICK_SEAT=master`,
`AGENTIHOOKS_QUICK_DIR`), then from that pane run `agentihooks quick relaunch
master --folder <folder> --adopt`. An open operator order is changing this.

## Enforcing the merge request discipline

Every seat's seed and cadence block already carry the rule: pack each
delivery into as few merge or pull requests as possible, one per repository,
at most 20 changed files each, and split only above 20. A follow-up goes into
the change already open in that repository, never a new one. An engineer
asks the master through `agentihooks quick send master "<text>"` before
opening any new merge or pull request; the master approves a new one only
when no change is open in that repository, or the open one would pass 20
files once the follow-up lands.
