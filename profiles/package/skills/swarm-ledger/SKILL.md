---
name: swarm-ledger
description: >
  Turn a plan file (plus any documents written for the work) into one interactive
  HTML ledger the operator keeps open in a browser: title, overview, sources, phase
  checkboxes, open questions with answer boxes, operator notes, follow-ups, and a
  comments dropdown under every item. Saves to localStorage and
  ~/development-ledger/<slug>.json through a local server. Every agent working that
  plan follows the ledger: operator writes reach it through the inbox in a swarm
  or a ledger watch outside one, it acts on the operator's checks, answers and notes, and records phase states and follow-ups in
  the HTML as work lands. Use when the user says "swarm ledger", "make a ledger for
  this plan", "ledger this plan", or when you work a plan that already has a ledger.
argument-hint: "<plan-file> [other development documents ...]"
---

# Swarm Ledger — The Operator's Live Checklist for a Plan

Every tool runs through one command that agentihooks installs (code in its
`scripts/swarm_ledger/`, standard library only), so every Bash call and every
Monitor resolves it without shell state:

```bash
agentihooks ledger new …                               # build a ledger
agentihooks ledger serve …                             # the local server
agentihooks ledger watch …                             # the Monitor feed
agentihooks ledger --slug <slug> --as <name> <command> # the agent CLI
```

A ledger `<slug>` is a record in the SQLite database `ledgers.sqlite3` in
`~/development-ledger/` (`$LEDGER_DIR`); no per ledger JSON or HTML file is
written. The local server (`$LEDGER_PORT`, default 8765) serves the page shell,
stores every operator and agent edit in that record and streams changes to open
pages. `agentihooks ledger storage export <slug>` writes the whole ledger as JSON
on request, and `storage import <file> --slug <slug>` loads one back losslessly.

Use it for a plan with several phases worked across sessions; a single-step task
needs no ledger.

## Part A — Create a ledger

### A1. Write the content file (AI-JUDGMENT)

Read the plan file and every document named in the arguments. Write
`~/scratchpad/<repo>/<task>/ledger-content.json`:

```json
{
  "title": "<plan name>",
  "overview": "<what the work is for and what done looks like, plain words, ≤ 200 words>",
  "sources": ["<absolute path of the plan file>", "<every other development document>"],
  "phases": [{"title": "<short name>", "description": "<what this phase is, semantically, ≤ 100 words>"}],
  "questions": [{"text": "<a question only the operator can answer>"}],
  "followups": [{"text": "<a known follow-up or blocker>"}]
}
```

Phases follow the plan's own order and granularity. Questions are the plan's
open decisions; an empty list is valid. Write for the operator: plain words, no
ids or hashes in the overview. Done when the file holds every phase the plan
names and every source path exists.

### A2. Build the ledger

<!-- DETERMINISTIC: validate limits, types and sources, store the ledger record -->
```bash
agentihooks ledger new --content <content.json> --plan <plan-file> [--size small|swarm] [--as <name>]
```

A ledger is small (the default) or swarm. A small ledger is one session's work
without a swarm: troubleshooting, an investigation, several steps with no
accepted plan. The session that creates it joins it as its worker through
`--as <name>` (default `$AGENTIHOOKS_AGENT_NAME`; refused without one) and
records its steps and findings there. A plan a swarm works takes `--size swarm`;
`agentihooks swarm <slug> create` marks its ledger swarm. HOME shows each size.
A small ledger moves itself to the bin once every phase, follow-up and task is
done and every question answered, or after 7 days with no change; the bin
deletes it 30 days later. A restored one stays on HOME until it changes again or
sits 7 more days. Swarm ledgers, and ledgers made before sizes, never move on
their own.

The slug is built by code, never typed: `<plan-file-stem>-<YYYY-MM-DD>` with
`--plan`, `small-<session>` for a small ledger with no plan, and
`proof-<swarm code>-<task>-<n>` with `--proof` for a proof swarm made from a swarm
task session; its herdr workspace carries the same name. `--slug` accepts only a
built form. It refuses an overview over 200 words,
a phase description over 100, an empty title, no phases or a missing source.
It leaves an existing ledger untouched (`"created": false`). Done when it
prints `"created": true` or names the existing ledger.

### A3. Start the server and hand the page to the operator

<!-- DETERMINISTIC: start the server detached if it is not answering; prints the base URL -->
```bash
agentihooks ledger serve --ensure
```

Give the operator `<printed base URL>/<slug>`; `/` lists every ledger. The gate hook starts
the server at every session start while any ledger exists, so after a reboot the page is back
as soon as any Claude Code session starts. Done
when `--ensure` printed the base URL.

### A4. Bind the crew and install the gate

A ledger is one task unit: an orchestrator and every agent of that crew share it.
Requirements: Python 3.11+ (the agentihooks floor), stdlib only; the gate needs Claude Code. Codex and
Copilot sessions get the CLI and the watcher, with advisory rules only.

`agentihooks init` installs the gate hooks with the rest of its settings
(SessionStart, UserPromptSubmit, PostToolUse, Stop), so nothing is installed per
ledger and a later `init` keeps them. Hooks load at session start: a session
started before the install needs a restart.

<!-- DETERMINISTIC: the join paragraph for each crew member's launch prompt -->
```bash
agentihooks ledger --slug <slug> --as <member-name> prompt
```

Paste that paragraph into each member's opening prompt. The orchestrator joins
with `--role orchestrator`. Set the ledger's `orchestrator` to its name.
Done when `agentihooks ledger --slug <slug> --as <name> status` lists every member.

## Part B — Work a plan that has a ledger (every crew member)

All commands are `agentihooks ledger --slug <slug> --as <name> <command>`.

### B1. Join, watch

1. `join` (orchestrator: `join --role orchestrator`). A hook binds this session to the ledger.
2. In a swarm, skip the watch: every operator write reaches you as an inbox message at your next
   tool call, and the tick wakes your pane when it sits idle. A ledger without a swarm has no
   inbox, so an idle session hears of the operator only through a `Monitor` (persistent when
   offered) on `agentihooks ledger watch <slug> --as <name> --since-rev <last handled rev, or omit>`.
   Restart with `--since-rev` to replay.

Each line is one operator event:

```
OPERATOR rev=12 comment added on phases/p1 [c-1a2b]: "text"
OPERATOR rev=13 comment edited on phases/p1 [c-1a2b] diff: "- old line\n+ new line"
OPERATOR rev=14 comment deleted on phases/p1 [c-1a2b] was: "text"
OPERATOR rev=15 answer added on questions/q3 [a-9f0e]: "text"
OPERATOR rev=16 note added [n-77aa]: "text"
OPERATOR rev=17 checked phases/p2
OPERATOR rev=18 message added on chat [m-3c4d]: "text" | REPLY RULES: <chat_instructions>
```

plus `SEED_ERROR <message>` and `WARNING <message>`. Done when `join` answered, and with a watch when it printed `WATCHING`.

### B2. Act on operator events, then ack (AI-JUDGMENT)

Who owes what: chat, notes and answers belong to the orchestrator; an event on an item belongs to
the member who `claim`ed it, else the orchestrator; chat starting `@name` belongs to that member.
`events` lists what you owe.

- `answer added|edited on questions/<id>` — apply it to the work; reply with `comment`. An edit
  carries only the diff.
- `checked|unchecked phases/<id>` — his ruling; unchecked means reopen.
- `note …`, `comment …` — read and act; reply with `comment <item> <text>`.
- `checked followups/<id>` — he closed it; stop work on it.
- `… deleted` — he withdrew it; stop acting on it.
- `message added on chat` — the orchestrator answers at once with `say <text>`; the line ends with
  `| REPLY RULES: …`, the ledger's `chat_instructions` (default: under 100 words unless the operator
  asks in a separate message to expand). Follow it exactly.
- `sync requested` — the operator pressed Sync. It is owed by every crew member: re-read the whole
  ledger, act on every operator event you have not handled, update phase and follow-up states, your
  status comments and (orchestrator) the time left, then `ack`.
- `stats sync requested` — the operator pressed the Stats sync. Owed by the orchestrator only: check
  every phase and follow-up state and the time left against the real work, fix what is stale, `ack`.
- `SEED_ERROR` / `WARNING` — fix the seed or the text at once.

Run `ack` after acting. Done when `events` prints nothing.

### B3. Record progress (AI-JUDGMENT)

| Command | Use |
|---|---|
| `phase <id> done --status "<what was done>"` / `phase <id> open` | a phase landed or reopened |
| `followup add "<text>"` / `followup done <id> --status "<what was done>"` / `followup open <id>` | a new blocker or follow-up, its closing, or reopening one checked by mistake |
| `scope <item> out --status "<why it is skipped>"` / `scope <item> in` | an item the operator ruled out of scope, or back in |
| `comment <item> "<text>"` | your status on an item; it amends your previous one |
| `retext <item> "<text>"` | rewrite a follow-up or question in plain words |
| `edit <chat\|item> <entry> "<text>"` / `delete <chat\|item> <entry>...` | fix or remove entries: yours, or (orchestrator) any agent's |
| `audit` | every agent text the filter refuses today: the cleanup worklist |
| `priority add <item> "<text>"` / `priority clear <id>` / `priority clear --all` | ask the operator something only the operator can answer and that blocks the work, one line of at most 20 plain words per item; clear it once answered |
| `relay <item> "<text>" --quote "<operator words>"` | the orchestrator posts a decision the operator gave in its pane: his answer on a question, his comment on any other item, marked relayed from the master pane with the operator's recorded words. Refused for any other agent, and unless the quoted words are in an operator prompt or question answer the session recorded in the last hour; a swarm session's first prompt is its launch prompt and never counts |
| — | notifications: none to send. The page raises one by itself when you add a follow-up or question, or answer an operator comment or chat message; nothing you run creates or clears one |
| `claim <item>` | take an item's operator events |
| `say "<text>"` | chat (orchestrator); `--long` only after the operator asked to expand |
| `time-left "<duration>"` | save remaining time; accepts `3h 20m`, `3h`, `20m`, or integer minutes |

Time Left is one compact duration, for example `3h 20m`. The orchestrator estimates the remaining
work on joining and revises it when progress or blockers change it. Set `0m` after verified
completion. Done when `status` returns the saved `time_left_minutes`.

Writing for the operator (the server enforces it):

- A comment is the item's status in plain words: what was done, or why it was skipped. One per
  agent per item: a second `comment` amends the first. A new entry is made only when the operator
  commented after yours. Evidence (runs, SHAs, files, proofs) stays in PR bodies and notes.
- Refused in comments, chat, follow-ups and questions: clock times, dates, commit hashes, run or job
  ids, file names and paths, code identifiers, labels in capitals, dashes, arrows, AI phrasing, more
  than one parenthesis or semicolon. Comments are at most 50 words, chat 100, item text 40. The
  refusal names every problem; rewrite and send again.
- Checked means completed: the text is dimmed, never struck. An item ruled out is never checked: `scope <item> out`
  shows a yellow "out of scope" dot and disables the item; the operator clicks the dot to bring it
  back, which adds a "Back in scope." comment and an operator event.

Each command is attributed to your name. There is no seed to edit: phase titles and descriptions
change with `phase set <id> title=<text> description=<text>`, follow-up and question text with
`retext`, task fields with `task set`; the ledger title and overview are set at creation and by the
operator. Items are closed by checking them, never deleted. Done when `status` shows your change.

### B4. The gate

For a bound session the hooks do the following; each is tunable in the ledger's `policy`
(`nudge_after_calls` 25, `stop_after_calls` 10, `stop_blocks` 3):

- every tool result and prompt: unhandled operator events you owe are injected, up to 5;
- after `nudge_after_calls` tool calls without a ledger command: a reminder to record progress;
- Stop is blocked while events are unhandled or `stop_after_calls` calls passed without a ledger
  command; after `stop_blocks` blocks it lets the stop through and logs `gate bypassed` in the ledger for the operator;
- any error, an unreadable ledger or a stopped server lets everything through; a ledger with every
  phase and follow-up done is not gated.

### B5. Close

When every phase is done and every follow-up closed, `leave`, then stop any watch you started.
Done when `status` no longer lists you.

## Part C — Run a swarm over a ledger

A swarm is a herdr workspace `<repo>-<code>` of Claude and Codex agents working the ledger's tasks, one task
per agent life. Redis holds claims and the agent registry; a systemd user timer runs `agentihooks swarm tick`
every minute, which retires finished agents, frees the tasks of dead ones and spawns up to the caps. No
process runs between ticks.

1. Tasks: put them in the content's `tasks` list (`title`, `description`, `phase`, `lane` eng or ci), or add
   them later with `agentihooks ledger --slug <slug> --as <name> task add <id> <title> --lane eng --phase p1`.
2. `agentihooks swarm <slug> create --repo <dir> --max-eng-agents 2 --max-ci-agents 1` (created paused).
3. `agentihooks swarm <slug> start`: scales up at once; `pause` stops new spawns, `stop` drains, `stop --now`
   kills every agent and reopens its task. Change caps with `agentihooks swarm <slug> max-eng-agents=3`.
4. `agentihooks swarm <slug> status` and `agentihooks swarm list` show agents, tasks and state.
5. Talk to the swarm from the page chat, or reach every live agent's inbox with `agentihooks swarm <slug> send-message "<text>"`; idle agents
   get it in their pane at once, busy ones when their turn ends.

Agents are told their task in their opening prompt and close it with `agentihooks swarm <slug> issue|pr|done|block|say`.
The session that started the swarm stays out of the work and speaks for the operator.

## Page design (operator rules)

Every change to `template.html` keeps these:

- Minimal. Messages (comments, answers, notes) are plain rows: author and time
  on one line, the text under it, faint Edit and Delete on the right. No cards,
  boxes, bubbles or backgrounds around a message; a thin left line marks the
  operator's own messages.
- "Add comment / answer / note" is a plain text link that opens one underlined
  input line; Enter sends. No free text boxes on the page.
- Comments sit in a dropdown under each item, with a count: open by default when it holds comments,
  closed when empty; the operator's own open or close choice is kept.
  Original sources are collapsed by default, with a count.
- Palette: black background with blue glass sections, red rule lines, white
  text. Body text 14px; title, section heads, item titles and meta text on a
  clear size scale. Centre column 1400px.
- Wide layout: the plan on the left, a sticky column on the right with a
  glass Stats section (started, elapsed, agent-maintained Time Left, completion with a thin
  bar, agents, last activity). The crew list under Stats is collapsed with a count; a red dot
  marks a member who owes the operator a reaction.
- Chat is a round button in the bottom-right corner, same style as Sync, with a red chat icon and
  a red count of agent messages newer than the last time the operator had it open (kept per
  browser). It opens a panel above it that leaves the page usable: plain message rows that scroll,
  one input line at its foot, Expand (wide, full height) or Shrink, Clear (empties it, one logged
  event), and Close or Esc. A notification for a chat reply opens it.
- Screenshots: the chat line and every comment line take a pasted, dropped or picked PNG, JPEG,
  WebP or GIF of at most 8 MB, previewed with Remove before sending. The server keeps one copy per
  image in `<slug>.media` beside the ledger and the entry only its id, type, size and dimensions;
  the line shows a thumbnail that opens full size. The bin keeps the images; the purge deletes them.
- Out of scope: a yellow dot and "out of scope" at the right end of the item row; the item text is
  dimmed, never struck, its checkbox disabled. Clicking the dot brings it back in scope. Every open
  item shows the dot faintly at all times, brighter on hover, to mark it out of scope (with a confirm).
  Completed items are checked with dimmed text; nothing is ever struck through. Stats count
  in-scope items and show how many are out of scope.
- A round Sync button with a red sync icon floats in the bottom-left corner. It sends anything still
  pending, then one sync order with a summary to every crew member; a red badge counts the members
  who have not acked the latest sync. After a sync the button rests for 5 minutes (dimmed, the
  time left in its title); the server refuses a sync inside that window too. A small red sync icon in
  the Stats heading sends a stats-only check to the orchestrator, with its own 5-minute rest. The header carries only the title, no progress line.
- Priorities sit under Original sources, closed by default, with a red count when anything waits on
  the operator. Each row is a red `#id` link that jumps to and highlights its item, the one-line ask,
  and Clear; Clear all sits at the top. Only what blocks on the operator's answer goes there.
- Above Sync sit a back-to-top arrow and a notifications bell, in the same round style. The bell's
  red count is the number of notifications. It opens a panel beside the buttons that leaves the
  page usable: newest first, each row with its time, a red `#id` link that jumps to the item and keeps the panel open (a chat
  reply opens the chat), what happened, the text, and Clear; Clear all at the top;
  the list scrolls; Esc or Close hides it. Hovering a row's text highlights it, and clicking it jumps
  like the `#id` link (selecting text to copy does not). The panel is 600px wide. The server raises a notification for a new follow-up,
  a new open question, or an agent comment or chat message that answers the operator. A row stays
  until the operator clears it; no agent op or seed edit can create or clear one.
- A design change ships in the page shell and static assets; every ledger serves it on the next
  page load.

## How saving works

- Comments, answers and notes are message threads: Add opens a line, Enter
  sends it, Ctrl+Enter adds a new line, Esc cancels; every entry has Edit
  and Delete. Each send, edit or delete is saved at once and logged as one
  event; checkboxes save at once.
- A checkbox someone else changed after the page last saw it keeps their value.
- Time Left displays the saved agent duration in hours/minutes, or "—" until supplied. Time and phase
  completion never recalculate it. Other Stats are computed in the page; started uses
  `_meta.created_at`, the ledger's earliest recorded timestamp.
- Chat is the `chat` thread (last 500 messages); each send is one event, so
  it reaches the orchestrator through the inbox (or a watch, outside a swarm) within seconds.
- The page follows the server's event stream and redraws agent changes, keeping the
  cursor and any message not yet sent. Original sources are collapsed by
  default.
- A save answers with a short acknowledgment (applied and refused op ids, the revision, warnings);
  the page takes the new state from its event stream.
- A `<slug>.json` or `<slug>.html` left in the ledger folder is imported into the record once, after
  a verified copy to `.imported/`, then removed; `agentihooks ledger storage cutover` imports every
  such file at once.
- With the server down the page keeps edits in localStorage, shows "server
  offline", and sends them when the server answers again.
- Every API call carries the page's ledger token and a local Host header;
  anything else gets 403.

## HTTP contract (serving the page from another host)

The page talks to its server only through these calls. Any host that answers them (a web UI, a
hosted service) can serve the same page; the rules stay in `ledger_core.sync` and the op modules.

| Call | Body and reply |
|---|---|
| `GET /<slug>` | the page |
| `GET /api/v1/ledgers/<slug>/events` | the event stream: a snapshot of the ledger and swarm, then patches |
| `PUT /api/<slug>` | `{"changes": [...], "ops": [...]}`: checkbox changes with their base, and ops (`add`, `edit`, `delete`, `clear`, `sync`, `stats_sync`, `priority_clear`, `notification_clear`, agent ops carrying `by`); the reply is `{"applied", "rejected", "_meta": {"rev", "warnings"}}` |
| `GET /api/<slug>` | retired: answers 410 and names the v1 resource |
| `GET /healthz` | `{"dir": "<ledger dir>"}` |

Every `/api/` call carries `X-Ledger-Token` (the page's `ledger-token` meta). A new kind of op is a module
with `OPS`, `check(op)` and `apply(doc, op, ctx)`, registered in `ledger_core.EXTENSION_OPS`
(priorities and notifications are built that way).

## Extracted Scripts

In agentihooks `scripts/swarm_ledger/`; `agentihooks ledger` dispatches to them (`__init__.py`).

| Script | Purpose | Idempotent |
|---|---|---|
| `new_ledger.py` | Validate content and store the new ledger record with a fresh token | Yes (existing ledger untouched) |
| `ledger_server.py` | `--ensure` / `--serve` / `--stop` the local server; applies page saves and streams changes | Yes |
| `storage_migration/` | `agentihooks ledger storage export`, `import` and `cutover`: lossless JSON interchange | Import refuses an existing slug without `--replace` |
| `watch_ledger.py` | Monitor feed: operator changes, seed errors and word-limit warnings, replayable with `--since-rev` | Yes (read-only) |
| `ledger.py` | Agent CLI: join, status, events, ack, say, comment, phase, followup, scope, retext, edit, delete, audit, time-left, claim, prompt | No (writes are attributed ops) |
| `ledger_notifications.py` | Notifications derived from agent events each sync; `notification_clear` for the operator (library) | — |
| `ledger_priorities.py` | Priorities: one-line asks agents add and anyone clears (library) | — |
| `ledger_comments.py` | The agent text filter, one status comment per agent per item, entry permissions, `audit` (library) | — |
| `ledger_hook.py` | Claude Code hook: binds sessions, injects owed events, blocks Stop | Yes |
| `ledger_gate.py` | Routing of operator events to crew members (library) | — |
| `ledger_agent_ops.py` | Server-side agent ops: join, ack, claim, set, add_item (library) | — |
| `chat_ledger.py` | Post a chat message as an agent through the running server | No (each call adds a message) |
| `ledger_core.py` | Validation, flatten, checkbox merge, the `sync` facade over the repository | — (library) |
| `repository/` | `SQLiteLedgerRepository`: row diff writes, events, summaries, token, bin registry, legacy import | — (library) |
| `template.html` | The page: HTML + CSS + vanilla JS, no external requests | — |
