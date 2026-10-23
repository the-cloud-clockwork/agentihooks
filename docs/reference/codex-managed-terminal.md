---
title: Managed Codex terminals
parent: Reference
nav_order: 16
---

# Managed Codex terminals and push masters

A Codex swarm execution using `app_server` runs through a supervised server.
Herdr displays a native Codex terminal attached to that execution's recorded
thread. Closing the terminal closes the viewer; the server and inbox delivery
continue. Reopening the viewer attaches to the same thread.
An unsent draft belongs to the viewer: inbox delivery preserves it while the
viewer stays open, but closing and reopening the viewer does not restore it.

## Runtime dependencies

The installed `agentihooks` tool includes `websockets>=15.0` as a runtime
dependency. Managed app-server launches can therefore connect without installing
development extras. The client uses `websockets.sync.client` when it connects;
an installation missing that package fails at connection time, even if importing
the protocol module alone succeeds.

Use the [installed tool setup and import check](../getting-started/installation.md#installed-tool-and-codex-app-server-dependencies)
to verify the CLI's environment. Installing the package in a separate workspace
venv does not repair the uv tool environment.

Worker images install a hashed runtime lock before installing AgentiHooks with
`--no-deps`. The delivered dependency change pins `websockets==17.2` in that
worker lock and updates its recorded checksum. The runtime dependency coverage
check scans imports across the Codex session package and app-server probe,
maps third party imports to distributions, and requires them in both the
runtime dependency set and the worker lock. This keeps future client imports
from silently depending on development-only packages.

The delivery proof for [the runtime dependency fix](https://github.com/the-cloud-clockwork/agentihooks/pull/3552)
verified a fresh tool install without development extras, imported
`websockets.sync.client`, and launched an admitted Codex worker on an isolated
scratch swarm. Its supervisor reached `state=connected` with a thread recorded
and its mounted profile validated. An import check alone does not establish
that a live execution connected or that inbox delivery works.

## Choose the transport

The swarm setting `codex-inbox-transport` accepts `app_server` (what
`swarm create` stores) or `legacy`. A swarm that stored `legacy` keeps it; set it before launching the
Codex agents that should receive inbox work in their thread:

```bash
agentihooks swarm <id> set codex-inbox-transport=app_server
```

The launch records the chosen transport. Changing the setting affects subsequent
executions; it does not convert a running session. To select legacy for
subsequent launches (the full rollback is in
[Codex inbox transport](codex-inbox-transport.md#roll-back)):

```bash
agentihooks swarm <id> set codex-inbox-transport=legacy
```

The swarm launch path passes `--codex-transport app_server` and the admitted
seat to `init-agent`. This flag is for a managed swarm execution, which needs
its swarm identity and admission; it is not a standalone terminal switch.

After the next admitted launch, inspect its recorded transport and state:

```bash
agentihooks swarm <id> status --json
```

Check the selected transport in status, then confirm the supervisor's launch
log reports `state=connected` with its execution, endpoint and thread.
For an end to end inbox check, follow the
[scratch swarm canary order](codex-inbox-transport.md#canary-order) and confirm
an item is answered from that thread. Keep the proof isolated from live work.

Delivery and attachment depend on the recorded harness and transport:

| Execution | Push inbox | Native remote terminal attachment | Foreground inbox wait |
|---|---|---|---|
| Claude | Yes, through its channel | No | No |
| Codex `app_server` | Yes, through its managed transport | Yes | No |
| Codex `legacy` | No | No | Yes for the master's inbox loop |

The inbox waker skips pane typing when the execution's transport owns push
delivery. Master panes are never typed into by that worker wake path. An unsent
draft in the viewer is separate from incoming inbox work.

## Hook identity after server detachment

Managed local Codex launches detach the supervised server with `setsid` while
the remote terminal viewer stays in the launch pane. The detached server shell
sets `AGENTIHOOKS_SWARM_LAUNCHER` to its own process id before starting
`scripts.codex_session serve`. Hooks run by the app-server can then reach that
launcher through their parent processes and retain the execution's swarm and
agent identity. Activity recording, heartbeats and swarm context injection
depend on that identity.

This assignment is automatic. Select `codex-inbox-transport=app_server` for
subsequent admitted launches using [Choose the transport](#choose-the-transport);
there is no separate hook identity setting. Do not export a guessed launcher
pid or unset `AGENTIHOOKS_SWARM_LAUNCHER` to bypass the check. A separate agent
started inside the managed agent remains nested and must not inherit the outer
agent's task identity. Reopening the viewer does not replace the server or
repair its inherited launch environment.

### Investigate a no hook event finding

A Doctor `no-hook-event` finding can indicate missing attribution even when
tools ran. In the incident documented in
[issue 4720](https://github.com/the-cloud-clockwork/agentihooks/issues/4720),
the reported Codex agent had 126 Bash and about 100 MCP pre-tool hooks, but
no swarm activity record. The detached server's parents never reached the
original launcher. The nested-session check instead found the remote viewer
in the launcher's terminal session and discarded the server's swarm identity
before hook dispatch.

The correction in
[pull request 4722](https://github.com/the-cloud-clockwork/agentihooks/pull/4722)
makes the detached shell the launcher. Its regression test executes the
generated shell command and checks that the inherited launcher id equals the
child process's parent id. The source task records a failing CI run before the
fix and passing checks after it; this evidence does not establish that every
later startup finding has the same cause.

1. Read the Doctor finding and inspect the execution's recorded harness,
   transport and state:

   ```bash
   agentihooks swarm <id> status --json
   ```

2. Compare the agent's transcript and progress with its hook activity for the
   same session and time window. No recorded tools requires startup diagnosis;
   hooked tool calls without swarm activity require attribution diagnosis.
   A connected server alone proves neither tool activity nor correct attribution.
3. For a local managed execution, inspect process ancestry without printing
   command arguments or environment values. Substitute its recorded server pid:

   ```bash
   ps -o pid,ppid,sid,comm -p <server-pid>
   ```

   Repeat for the parent pids to compare the server, supervisor and detached
   shell. The viewer is a separate process; its presence in the launch pane
   does not establish the app-server's hook ancestry.
4. Confirm the installed launcher includes the correction before a subsequent
   admitted launch. An existing server keeps its inherited environment; use the
   normal lifecycle controls rather than changing a running process. Compare
   a subsequent tool call with newly recorded activity and the next Doctor pass.
   Retain the finding, session and process evidence if attribution still fails.

For the related terminal-session fallback used after wrapper loss, see
[Wrapper loss and swarm identity](../pillars/swarm.md#wrapper-loss-and-swarm-identity).
That fallback addresses a different process shape from the detached managed
server corrected here.

## Open and reopen a viewer

The managed launch starts the server separately and runs the attach helper in
the foreground of its herdr pane. To reopen a local managed Codex execution,
use its recorded execution identity:

```bash
agentihooks swarm <id> terminal <execution-id>
```

For a connected local execution this opens a herdr tab in the swarm workspace
and prints JSON containing `agent`, `thread` and `pane`. The viewer runs
`codex resume --remote` with the recorded endpoint and thread, with the Codex
update check turned off so a new Codex release never stops it at the update
prompt. It does not create a new conversation. The same command retains the
existing worker-terminal path for Kubernetes executions; see
[Swarm](../pillars/swarm.md).

The printed `pane` becomes the agent's recorded pane. After the viewer opens,
the reopen writes that pane id to the agent's execution record, so the `Pane`
column of `agentihooks swarm <id> status` and a later retire name the pane
the most recent reopen opened, instead of the launch pane or an earlier
viewer:

```bash
$ agentihooks swarm <id> terminal <execution-id>
{"agent": "<agent-name>", "thread": "<thread-id>", "pane": "<new-pane-id>"}
$ agentihooks swarm <id> status
Agent         ...  Pane           ...
<agent-name>  ...  <new-pane-id>  ...
```

The pane write changes only the pane field. It rereads the stored execution
inside a Redis watch, so a tick that updates the same execution at the same
moment keeps its change. The write makes up to five attempts when a
concurrent update conflicts, then refuses with
`execution history kept changing; pane was not committed`. It leaves the
agent's state-since time untouched, so a reopen never resets how long the agent
has been in its current state. An execution identity with no stored record is
refused with
`unknown execution identity; display labels cannot identify attempts`. The new
viewer is also recorded as a viewer of the execution, as every reopened viewer
is. A viewer that fails to open writes no pane, and the record keeps its
earlier pane.

The delivery evidence is
[the reopen pane write](https://github.com/the-cloud-clockwork/agentihooks/pull/3963),
covered by `test_reopening_a_managed_viewer_records_its_new_pane_on_the_execution`,
`test_reopen_refused_by_the_viewer_records_no_pane`,
`test_a_pane_write_keeps_every_other_field_and_updates_the_projection` and
`test_a_pane_write_for_an_unknown_execution_is_refused`.

For attachment diagnostics, the launcher helper can wait for the recorded thread:

```bash
python -m scripts.codex_session attach \
  --slug <id> \
  --agent <registered-agent-name> \
  --timeout 120
```

It polls every half second for a connected transport with a thread id. The
default timeout is 120 seconds. `--codex-bin` selects the Codex executable.
A viewer that exits nonzero in under five seconds is retried, up to three
attempts, because a newly created thread may not yet have a resumable rollout.
A successful exit or a viewer that stayed open at least five seconds returns
its exit status without another attempt.

## Work as a push master

A push-mode Codex master's opening prompt tells it to read `agentihooks msg
inbox`, handle every open item, then end its turn when no work remains. New
items arrive in that thread after the turn finishes. It must not enter
`agentihooks swarm <id> wait --inbox`, including when older profile text asks
for that loop.

Receipt does not settle an action item. Read and answer it through the inbox:

```bash
agentihooks msg inbox
agentihooks msg read <item-id>
agentihooks msg reply <item-id> "The requested work is complete."
```

An explicit dependency still uses the normal swarm wait, for example:

```bash
agentihooks swarm <id> wait --on checks <pull-request-url>
```

Waits on a reply or another task remain available too. A legacy Codex master
retains its foreground `wait --inbox` loop.

Push-mode handoff rendering replaces a Next instruction to rearm a Monitor
with reading the inbox. The transfer's expected first Next action receives the
same rewrite, so continuity confirmation compares the rendered action exactly.
Legacy rendering retains the foreground inbox wait. New push-master handoffs
should make their first Next action read `agentihooks msg inbox`.

## Profile check in the execution home

Every profiled launch asks the agent to validate its mounted profile with the
binding canary command written into its instructions. For an `app_server`
execution the check runs against the execution home, a private copy of the
shared rendered profile home, not the shared home itself.

1. `init-agent` writes the launch request to a private report file under the
   runtime folder and names it in `AGENTIHOOKS_PROFILE_REPORT`. The request
   records the profile, the harness, `state: pending` and the shared rendered
   home's binding evidence, its canary included.
2. The supervisor copies the shared home into the execution home with mode
   `0700`. The copy's binding evidence names the execution home; the shared
   home's evidence is left unchanged.
3. When the launch environment names a report, the supervisor re-records the
   request against the execution home (`binding.rehome`). It re-inspects the
   execution home's rendered evidence and keeps the request only when its
   canary equals the canary recorded at launch. The report's `rendered` block
   then names the execution home; its canary, profile, harness and pending
   state are unchanged.
4. The server starts with `CODEX_HOME` set to the execution home. When the
   agent runs its canary command, validation compares the live process home
   with the recorded home. Both name the execution home, so the check passes
   and the report moves to `state: validated`.

A launch whose environment names no report (an unprofiled launch) skips the
re-record, and the report file is never touched. Legacy Codex launches never
pass through the supervisor, so their request keeps the rendered home they
started with.

The execution home is `<execution folder>/codex`. The execution folder is the
parent of the server's socket, which the supervisor's launch line prints as
`endpoint=unix://<path>`. Sockets sit under `$HOME/.agentihooks/cx`.

### Refusals

When the execution home's evidence cannot be confirmed, the supervisor marks
the pending request `state: failed` with the reason and the launch fails: the
transport binding is recorded `failed`, the agent is dropped and the execution
folder is removed. The reasons are:

| Reason | Cause |
|---|---|
| `execution home canary differs from the launch request` | The execution home's instructions carry another canary than the shared home did at launch: the profile was rendered again between the request and the execution home. |
| `profile mismatch in rendered binding` | The execution home's evidence names another profile, harness or home than the request: the shared home was rendered for another profile or harness after the request was written. |
| `mounted persona changed since render`, `rendered source manifest changed`, `missing or invalid rendered profile evidence: …` | The execution home's instructions or source manifest no longer match its evidence, or the evidence file is missing or unreadable. |

The reason is written to the report, so `init-agent` prints
`profile_validation=failed` and `agentihooks init-agent: <reason>` and exits 3.
Read the reason there or in the report. Known gap: the supervisor process
does not print the `agentihooks codex-session: launch refused:` line other
launch refusals print; a follow up on the ledger tracks it.

A failure while copying the home (an `OSError` from the copy) refuses no
request: the report stays `pending`, and `init-agent` fails with
`mounted profile canary did not validate before timeout`.

### Read the result

From inside the agent session:

```bash
agentihooks profile binding
```

It prints the session home's profile, harness, persona and source digests,
the routed account and the report state (`pending`, `validated` or
`failed: <reason>`), never an environment value. Point `--home` at an
execution home to check its evidence without a report:

```bash
agentihooks profile binding --home <execution folder>/codex
```

That states `rendered` or `invalid: <reason>`. A home with no evidence file
prints `ERROR:` with the read error and exits 1.

The delivered behaviour is
[pull request 3533](https://github.com/the-cloud-clockwork/agentihooks/pull/3533),
covered by `test_rehomed_request_validates_against_the_execution_copy`,
`test_rehome_refuses_and_marks_an_invalid_execution_copy` and
`test_execution_home_rerecords_the_profile_report_for_the_copy`. Those tests
run against fixture homes; no live `app_server` launch passing its profile
check is cited here.

## Launch waits and the spawn timeout

The swarm runs `init-agent` with a 300 second spawn timeout
(`SPAWN_TIMEOUT_S` in the swarm runtime). A spawn that passes it is killed and
fails with `init-agent timed out for <name>`, which names no wait. To return
inside that timeout, every launch gets one 270 second launch deadline
(`LAUNCH_DEADLINE_S`), counted from the start of `init-agent`. Each of the
waits below is granted its own limit or what remains of the deadline,
whichever is less:

| Wait name | Runs for | Own limit | Result when it runs out |
|---|---|---|---|
| `Codex ready` | A Codex agent in a herdr pane, not `app_server`, whose pane herdr detected or that has an opening prompt | 120 seconds | The session rename is skipped (`session_name_error=not-ready`) and the opening prompt is held (`opening_prompt=held:not-ready`); the launch goes on |
| `Codex ready for the opening prompt` | The same agent, when it has an opening prompt and the first ready wait passed; it follows the session rename when one ran | 120 seconds | `opening_prompt=held:not-ready`; the launch goes on |
| `profile canary` | Every profiled launch, both harnesses and both transports | 90 seconds for Codex, 120 for Claude; `--canary-timeout` overrides it | The launch fails: `profile_validation=failed`, exit 3. The opening prompt was already sent or held before this wait began |

A ready wait passes once the pane reads idle or done on two polls in a row,
1.5 seconds apart. An `app_server` launch has no ready waits, because its
server names its thread before the first turn; only its canary wait takes from
the deadline.

Time spent outside these waits still runs the deadline down: rendering the
profile home, starting the pane and waiting for the route report
(`--route-timeout`). The deadline is fixed when `init-agent` starts, so these
steps and the Codex session rename between the two ready waits shorten what
the later waits are granted.

The deadline bounds only the waits. The herdr commands around them, such as
the pane rename before the first ready wait, the session rename, which
confirms within 15 seconds (`RENAME_CONFIRM_S`), and typing the opening
prompt, still run after the deadline is spent. Each herdr command has its own
30 second subprocess timeout. The 30 seconds between the 270 second deadline
and the 300 second spawn timeout hold the session rename confirm
(`tests/test_launch_deadline.py` checks that 270 plus 15 stays below 300). A
launch whose herdr commands all stall can still reach the spawn timeout.

### Worked budget

A legacy Codex launch whose two ready waits each run their full 120 seconds
has spent 240 seconds of the deadline, plus whatever the route wait and pane
start took. Its canary wait is granted the remaining 30 seconds or less instead
of 90. Before the shared deadline, the same launch could spend 120 + 120 + 90 =
330 seconds in waits alone and outlast the spawn timeout with no named reason.

### When the deadline cuts a wait

A ready wait that is cut short returns not ready; the launch does not fail
there. A canary wait that is cut short and then runs out fails the launch, and
the reason on stderr first names the deadline, the first wait that was granted
less than its own limit and the seconds each wait spent so far. The example
uses whole seconds; a live launch prints the fractional grant, such as
`within 29.8741s`:

```text
agentihooks init-agent: the 270s launch deadline ran out in the profile canary wait after Codex ready 120s, Codex ready for the opening prompt 120s, profile canary 30s: the codex agent reported no canary within 30s
```

A canary wait that runs out inside its own full limit keeps its plain reason,
`mounted profile canary did not validate before timeout: the codex agent
reported no canary within 90s`; see [Profile launch home](profile-launch-home.md).
In a swarm either reason is written to the swarm timer journal as
`spawn failed for <task>: <reason>`. When the task is still active, claimed by
the failed agent and not waiting on its merge, the task is reopened and the
line reads `spawn failed for <task>, task <task> reopened: <reason>`.

### Count launch deadline and spawn timeout failures

Count both failures for one UTC day from the swarm timer journal:

```bash
day=2026-10-23
TZ=UTC journalctl --user -u 'agentihooks-swarm*' \
  --since "$day 00:00" --until "$day 23:59:59" --no-pager -o cat \
  | grep -cE "launch deadline ran out|init-agent timed out for"
```

On this workstation the journal held no line of either kind on any day from
16 to 23 October 2026, the whole span it kept when this section was written.
The journal keeps only a few days; older launches are in the swarm store
history.

### Troubleshooting

| Symptom | Meaning and next check |
|---|---|
| `opening_prompt=held:not-ready` | A ready wait ran out, on its own limit or the deadline. Open the herdr pane: a Codex agent still starting, stuck on a prompt, or busy with a startup turn, so it never read idle twice. |
| `the 270s launch deadline ran out in the <wait> wait after ...` | Earlier waits used most of the deadline. Read the listed seconds; the longest wait is the first to investigate. The named wait is the first one granted less than its own limit, so the steps before it used the deadline: the route wait, the pane start or an earlier wait. |
| `init-agent timed out for <name>` | The spawn passed 300 seconds in steps the deadline does not bound, such as stalled herdr commands around the waits. Read the launch's pane and the swarm journal lines around the spawn. |

### Tests

`tests/test_launch_deadline.py` pins the behaviour: a Codex launch with both
ready waits at 120 seconds grants the canary 30 seconds, ends inside the spawn
timeout and prints the message above; a wait after the deadline is spent gets
no time and the first cut wait stays named; a wait that fails inside its own
limit keeps its own error; a wait run again under the same name adds its time
to that name's total; and `LAUNCH_DEADLINE_S` plus `RENAME_CONFIRM_S` stays
below `SPAWN_TIMEOUT_S`.
Delivered in
[pull request 5288](https://github.com/the-cloud-clockwork/agentihooks/pull/5288):
[red run](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39431053228/job/118655912523),
where the canary got 90 seconds after two 120 second ready waits, 330 seconds
in all, and
[green run](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39431405875/job/118657120243).

## Lifecycle and failures

Reopening a viewer and retiring an execution are separate operations. Managed
execution retirement uses its supervised runtime and closes the execution's
recorded viewers; see [Retire a managed
execution](#retire-a-managed-execution). The ordinary runtime resume path
refuses a managed execution with `unsupported resume: a managed Codex execution
continues through its supervised server`. Use terminal reopening for a live
execution. Renaming a managed agent renames its herdr pane and registry
identity without typing a session-name command into the viewer.

Retiring a managed execution closes the agent's recorded pane, which after a
reopen is the latest reopened viewer, without checking which terminal herdr
now shows in it. Every other recorded viewer pane is closed only while herdr
still lists it on the terminal it was opened with; a viewer recorded without a
terminal id, or whose pane id herdr has since reused for another terminal, is
left open. The launch pane is never a recorded viewer, so once a reopen
replaces it on the record, retirement no longer closes it.
`test_managed_retirement_closes_every_recorded_viewer_and_removes_the_runtime_home`
covers the viewer closing.

The live qualification also recorded these viewer limits:

- Initial launch can leave its `/rename` command in the composer while the
  server starts work; the recorded thread remains the attachment identity.
- Retiring the execution left a reopened viewer showing
  `Reconnecting to server…` during the qualification. A viewer pane that
  retirement leaves open, as described above, can show the same; close it
  separately.
- A Codex update prompt can interrupt attachment. Dismiss or complete it in
  the viewer before checking the conversation history.

| Symptom | Meaning and action |
|---|---|
| Launch fails with `ModuleNotFoundError: No module named 'websockets'` | The launching tool environment lacks its runtime dependency. Update the checkout to a revision containing the dependency fix, reinstall the tool using the installation instructions, and repeat the tool Python import check before retrying an admitted launch. Installing development extras in another venv does not fix this environment. |
| Profile check fails with `live process binding changed since validation` on an `app_server` execution | The report records a home other than the execution home. A revision without the execution home re-record step produces this on every launch; update to one that contains it. See [Profile check in the execution home](#profile-check-in-the-execution-home). |
| Launch fails with `execution home canary differs from the launch request` | The profile was rendered again between the launch request and the execution home. Relaunch; the new request records the current canary. |
| Attach reports `failed` or `exited` | The recorded app-server ended; there is no live thread to attach through that transport. Inspect execution status before attempting recovery. |
| Attach times out without a connected thread | Startup has not produced a connected thread within the timeout. Check the managed server's launch result and serve log; increasing the timeout alone does not repair a failed launch. |
| Reopen reports no connected thread | The immediate reopen command requires an already connected thread. Use the waiting attach helper during startup. |
| Status or retire names a pane that no longer exists after a reopen | A revision before the reopen pane write left the launch pane on the agent record. Update to a revision that contains it; the next reopen records the new pane. |
| Reopen fails with `execution history kept changing; pane was not committed` | The viewer is open and recorded as a viewer; the agent record keeps its earlier pane. Close that viewer, then run the reopen again. See [Open and reopen a viewer](#open-and-reopen-a-viewer). |
| Reopen fails with `unknown execution identity; display labels cannot identify attempts` | The agent is listed, but its execution identity has no stored record. Check `agentihooks swarm <id> status` for the agent's current execution. |
| Agent runs no attachable app-server | The recorded harness or transport lacks native terminal attachment. Use its normal terminal path. |
| Closing a viewer leaves the agent working | Expected: the viewer does not own execution lifetime or inbox delivery. |
| A viewer shows `Reconnecting to server…` after its agent retired | Retirement had no live record of that pane: it was opened some other way than `agentihooks swarm <id> terminal`, it is the launch pane after a reopen replaced it on the record, its pane id was reused for another terminal, or it runs on a revision without viewer cleanup. Close the pane by hand. See [Retire a managed execution](#retire-a-managed-execution). |
| `retire failed` finding with `refusal: viewer pane <pane>: …` or `refusal: runtime home: …` | Retirement ended the server but could not close a pane or remove the execution folder. The binding stays connected and the tick retries; fix the named pane or folder. |

## Retire a managed execution

The tick and the lifecycle controls retire a managed Codex execution through
the same supervised runtime path. Retirement runs these steps in order and
stops at the first one that fails:

1. End the supervised app-server, selected by the server process id and start
   identity recorded on the transport binding. A missing binding, or one with
   no recorded server process or start identity, is refused with
   `managed server identity is unknown`. A server process that is already
   gone, or whose start time no longer matches the recorded identity, counts
   as ended.
2. Close the agent's recorded pane, when it has one, and each viewer pane
   recorded for the execution that herdr still lists on the terminal it was
   recorded with. The agent's recorded pane is the launch pane until a reopen
   replaces it with the newest viewer; it is closed without a terminal check.
   A pane herdr reports as not found counts as closed.
3. Remove the execution folder, the runtime folder holding the server socket
   and the execution home `codex`. Before deleting it, any rollouts under the
   home's `sessions` and `archived_sessions` folders are appended to the
   canonical Codex home (`sqlite_home` from the home's `config.toml`, else
   `~/.codex`; a `sqlite_home` inside the execution home also falls back to
   `~/.codex`), so conversation history survives the removal. A canonical copy
   that already holds the whole rollout is left as it is. A rollout whose
   content diverged from the canonical copy is kept under
   `recovered-<execution folder>`.
4. Mark the binding `exited`, release it and forget the recorded viewers.

### Recorded viewers

`agentihooks swarm <id> terminal <execution-id>` records each viewer pane it
opens, together with the herdr terminal it was opened on, in the swarm's
`codex-viewers` hash keyed by pane. A viewer opened any other way, such as a
hand run `codex resume --remote`, is not recorded and stays open after
retirement.

herdr reuses the ids of closed panes, so retirement closes a recorded viewer
only while `herdr pane list` still shows that pane on the terminal it was
recorded with. A viewer the operator already closed, whose id now belongs to
another pane, is left alone, as is a recorded viewer with no terminal id.
`herdr pane list` runs only when the execution has recorded viewers.

### Removed folder

Only a folder matching the socket layout is removed: its parent is named `cx`
and its own name is the first sixteen characters of the execution id after the
`exe-` prefix, as in `$HOME/.agentihooks/cx/<first 16 characters of the id
after exe->`. The check is on those two names, not on the root. A binding whose
recorded endpoint lies outside that layout keeps its folder; the binding is
still marked exited. A folder already gone is skipped.

### Refusals and retry

A refusal leaves the binding connected and its viewer records in place, so the
next tick retries the whole retirement; ending a server that is already gone
does not refuse the retry. Most tick paths that retire an agent log their own
`could not retire <agent> …, retrying next tick` line; the lost agent path logs
only `lost <agent>`, and the idle stall path logs nothing on a refusal. Only
the path that retires an ended execution counts failures: from its third
failing tick, `agentihooks swarm <id> status` lists a `retire failed` health
finding naming the process and the refusal. The count clears when the agent is
dropped from the swarm.

Known gap: removal catches only `OSError`. A malformed `config.toml` in the
execution home raises out of retirement with no refusal recorded; a follow up
on the ledger tracks it.

Known gap: the lost agent path drops the agent from the swarm even when its
managed retirement is refused, so that server keeps running with its binding
connected, no later tick retries it and no finding is raised; a follow up on
the ledger tracks it.

| Refusal | Cause |
|---|---|
| `managed server identity is unknown` | The binding records no server process or start identity. |
| `survived SIGKILL: <pids>`, `signal to <id> refused: <error>` | The server process or its group did not end. |
| `pane list: <error>` | herdr could not list panes, so recorded viewers could not be checked. |
| `viewer pane <pane>: <error>` | herdr refused to close the agent's recorded pane or a recorded viewer for a reason other than not found. |
| `runtime home: <error>` | Removing the execution folder raised an `OSError`, for example a permission error. |

The delivered behaviour is [pull request
3898](https://github.com/the-cloud-clockwork/agentihooks/pull/3898) ([issue
3896](https://github.com/the-cloud-clockwork/agentihooks/issues/3896)), proved
red on a test-only head in [run
39004056453](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39004056453)
and green in [run
39004150106](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39004150106),
covered by the managed lifecycle tests in
`tests/swarm/test_managed_lifecycle.py` and the attach tests in
`tests/codex_session/test_attach.py`. Rollout preservation before removal
arrived later in [pull request
4339](https://github.com/the-cloud-clockwork/agentihooks/pull/4339). Those
tests use fixture panes and folders; no live retirement of a herdr viewer is
cited here.

## Reclaim orphan execution homes

Retirement and the reclaim a launch runs first both start from a transport
binding: a home that no binding names is never removed by them. Such homes
stay behind when a binding was lost or predates the current layout, for
example homes created in the user runtime folder before the runtime root
moved to `$HOME/.agentihooks/cx`. Each holds a full Codex home with its
plugin cache, so on the runtime folder's tmpfs they fill it. The lifecycle
sweep reclaims every execution home that no live process owns, whether or not
a binding names it.

### Where the sweep looks

The sweep reads two roots, in this order:

| Root | Origin |
|---|---|
| `$XDG_RUNTIME_DIR/cx`, else `/run/user/<uid>/cx` | The runtime folder older launches used, a tmpfs on most hosts. |
| `$HOME/.agentihooks/cx` | The current runtime root (`runtime_root`), where every new execution folder is created. |

Every directory directly inside a root is a candidate, whatever its name. A
root that is missing, is a symbolic link or cannot be listed is skipped and
the other root is still swept. Symbolic links and plain files inside a root
are skipped.

New homes already live under `$HOME/.agentihooks/cx`, so their plugin caches
sit on disk wherever `$HOME` is not itself a tmpfs. Only homes left in the
older runtime root occupy the tmpfs, and the sweep removes them once they are
orphans.

### Owned homes are never touched

A home is owned, and kept, when any of these holds at the sweep's process
snapshot:

- A live process has its working folder inside the home.
- A live process has an argument containing a path inside the home, such as
  a viewer started with `codex resume --remote unix://<home>/s`.
- A live process has an open file inside the home.
- The home's server socket `<home>/s` accepts a connection within one second.
  A socket file left by a server that has exited refuses the connection and
  does not keep the home.

The process checks come first, from the snapshot; a process whose open files
cannot be read is skipped and the others are still checked. A home counts as
"inside" a reference when the reference contains the home's path followed by
`/`.

A home that is not owned is still kept while anything in it changed in the
last ten minutes (`SETTLE_S`, 600 seconds), judged by the newest modification
time anywhere in its tree. A launch copies the home before its server starts,
so a new home has no owner for a moment.

### Removal and transcripts

`agentihooks gc` reports orphan homes as `would remove` and leaves them.
`agentihooks gc --enforce`, which the hourly `agentihooks-gc` cron runs,
removes them on the first sweep that finds them; the two sweep confirmation
that other lifecycle findings wait for does not apply here. Removal uses the
same step as retirement: rollouts under the home's `sessions` and
`archived_sessions` folders are appended to the canonical Codex home first,
under the rules in step 3 of [Retire a managed
execution](#retire-a-managed-execution), then the execution folder is deleted.
The sweep then checks that the folder is gone.

Each removed home is appended as one JSON line to
`~/.agentihooks/gc-codex-homes.jsonl` (the AgentiHooks home), the only lasting
record of what was removed: `gc-last.json` is overwritten by the next sweep.

### Read the report

```bash
agentihooks gc --json | jq '.codex_homes'                      # every orphan home this sweep found
agentihooks gc --json --path ~/.agentihooks/cx | jq '.codex_homes'
jq '.codex_homes' ~/.agentihooks/gc-last.json                  # latest sweep, hourly cron included
jq -s '{homes: length, bytes: (map(.bytes) | add)}' ~/.agentihooks/gc-codex-homes.jsonl
```

The text output of `agentihooks gc` prints no line for Codex homes; read them
from the JSON report. `--path` limits the sweep to homes under that path. Owned
homes and homes inside the ten minute window do not appear. Each entry is one
of:

```json
{"path": "/run/user/1000/cx/69aede3172ac4d86", "bytes": 131362816, "action": "would remove"}
{"path": "/run/user/1000/cx/69aede3172ac4d86", "bytes": 131362816, "action": "removed"}
{"path": "/home/me/.agentihooks/cx/a1b2c3d4e5f60718", "action": "error", "error": "home still present after removal"}
```

`bytes` is the space the home's files occupy on disk. A `removed` entry gains
`log_error` when the line could not be written to `gc-codex-homes.jsonl`; the
home is still removed. An error on one home is reported and the sweep goes on
to the next. While another sweep holds the lifecycle lock, the report is
`{"skipped": "another sweep is running"}` and no home is considered.

### Troubleshooting

| Symptom | Cause and fix |
|---|---|
| An orphan home never appears in `codex_homes` | Something still owns it or wrote to it in the last ten minutes. `pgrep -af <home>` lists processes whose arguments name it; `lsof +D <home>` lists open files and working folders inside it. A shell left in the folder or a hand opened viewer keeps the home until it exits. |
| `action: error` with a TOML parse message | The home's `config.toml` is malformed, so the canonical Codex home for its rollouts cannot be read. The home is kept. Copy any rollouts under its `sessions` folder to `~/.codex/sessions` by hand, then fix or remove the file; the next sweep removes the home. |
| `action: error` with `home still present after removal` | Deletion returned but the folder is still there, for example a process recreated files in it. The next sweep tries again. |
| `action: error` with a permission message | The sweep's user cannot read or delete the folder. Fix the folder's owner or mode. |
| `codex_homes` is missing and the report says `skipped` | Another sweep was running. Run `agentihooks gc --json` again once it ends. |
| The runtime folder tmpfs is still full after an enforcing sweep | Read `codex_homes` for `error` entries and `df -h "$XDG_RUNTIME_DIR"` for other users of the folder. A home that is owned is kept by design. |

### Evidence

Delivered by [pull request
5307](https://github.com/the-cloud-clockwork/agentihooks/pull/5307) ([issue
5305](https://github.com/the-cloud-clockwork/agentihooks/issues/5305)), red on
the test only head in [run
39462556198](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39462556198)
and green on the delivered head in [run
39470242818](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39470242818).
`tests/lifecycle/test_codex_homes.py` covers both roots, each ownership check,
the ten minute window, transcript preservation, the removal log, scope and
every error path. Before the fix, a dry run of the branch against the live
runtime folder found eight orphan homes holding 986300416 bytes. On 23 October
2026, `~/.agentihooks/gc-codex-homes.jsonl` on the workstation listed twelve
removed homes, eight from `/run/user/1000/cx` and four from
`~/.agentihooks/cx`, together 1514475520 bytes; `/run/user/1000/cx` no longer
existed and the latest sweep's `codex_homes` was empty.

See [Swarm inbox delivery](../pillars/swarm.md#delivered-then-confirmed) for
receipt, redelivery and closure rules, and [Codex compatibility](CODEX-COMPAT.md)
for the hook and profile contract.
