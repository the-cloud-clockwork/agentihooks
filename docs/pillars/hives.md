---
title: "Hives"
parent: The Four Pillars
nav_order: 9
permalink: /docs/pillars/hives/
---

# Hives
{: .no_toc }

A hive is one machine that runs swarm agents: a workstation, a VM or a Kubernetes pod. Each hive joins the swarm
once, beats every fifteen seconds, and takes every agent type its `excluded` setting allows.

1. TOC
{:toc}

---

## Adding a machine to a hive

A machine joins a hive only through the UI hive: a UI hive adds the machine and delivers its credentials to it
over SSH. A headless hive cannot add one.

```bash
# On the UI hive
agentihooks hive add ssh://worker@lab-vm:22 \
  --url https://swarm.example.internal:8770/hive/refresh

# On the new machine, once hive add has delivered its hive.env
agentihooks hive install          # user service that runs agentihooks hive run
```

- `hive add <ssh target> --url <url> [--redis-url URL]` names the machine `swarm-hive-<epoch seconds>` from the
  clock; a typed label is refused. It refuses unless the local hive record has
  `ui=yes`, with `hive add refused: hive <id> is headless; only a UI hive adds machines`. On a UI hive it runs
  `herdr machine add --label swarm-hive-<epoch seconds> --remote-session default <target>` (300 second timeout) unless
  `herdr machine list --json` already holds that target, so a rerun only redelivers the credential; it mints a hive member
  named after the label, and delivers it: `ssh -o BatchMode=yes <target>` (60 second timeout) writes the member's
  `hive.env` (mode 0600) into `${AGENTIHOOKS_HOME:-$HOME/.agentihooks}/hive.env` on the remote machine. If delivery
  fails, the minted member is revoked and the command exits 1. On success it prints
  `added <target> to herdr as <label>; its hive member is <id>`.
- `--url` is this hive's refresh endpoint as the new machine reaches it, saved into the delivered `hive.env`.
  A refusal prints `hive add refused: <reason>`, such as `herdr exited 1: <stderr>`, `ssh exited 255: <stderr>`
  or `herdr failed (<error>)`.
- The delivered `hive.env` carries the hive id, the hive URL, a ledger credential and a Redis URL for the hive's
  own Redis user: `AGENTIHOOKS_HIVE_ID`, `AGENTIHOOKS_HIVE_URL`, `AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL` and
  `AGENTIHOOKS_HIVE_REDIS_URL`. `agentihooks hive refresh` posts to the URL saved there. The hive URL must be https
  unless it is loopback.
- When the hive endpoint trusts a ledger CA (`LEDGER_CA_PEM`, else `ledger-ca.pem` in its home), `hive refresh`
  writes it to the file ledger clients trust: `LEDGER_CA_FILE` when set, else `ledger-ca.pem` in the hive home.
  A grant without one leaves that file as it is. The Helm hive endpoint reads the CA from the ledger TLS secret.
- The hive endpoint is `agentihooks hive serve`, or the Compose `hive` service started with `--profile join`.
  The Compose service always serves TLS from `SWARM_TLS_DIR`; bare `hive serve` serves TLS only with `--tls-cert`
  and `--tls-key`, and refuses to serve on a non-loopback address without them. It serves only
  `POST /hive/refresh`; the endpoint's request, handler and Redis time limits, and the 503 it answers when Redis
  does not reply, are under [Hive endpoint limits](#hive-endpoint-limits).
- Each member gets the Redis user `hive-<id>`: it reads the swarm keyspace and writes only its own hive keys;
  [Member keys](#member-keys) lists what lives there.
  A launch order's receipt and acknowledgement live under those keys. The controller reads the acknowledgement and
  records the execution as working, with the hive's process target, pane, model and effort.
  Credentials sit outside what a member can read.
- `hive install` writes and enables the `agentihooks-hive` user service, which runs `hive run` and restarts five
  seconds after any exit. `hive run` publishes sessions, quota, slots and repos every fifteen seconds.
- `agentihooks hive refresh` mints a new Redis credential; `agentihooks hive revoke <id>` deletes a member's
  ledger credential and Redis user.
- `agentihooks hive remove <id>` takes a hive out of the fleet in one run. It sets `draining=yes`, so placement
  puts no new agent or master there, and sends every live agent on the hive one inbox order to hand off. Once none
  is left it revokes the member and its Redis user, removes the herdr machine saved under the hive's label and that
  machine's host key from `~/.ssh/known_hosts`, deletes the hive record and every key below it, and deletes the
  OpenBao record (`k8s/agentihooks-hive-<label>`, or `--path`). With `--antoncore <checkout>` it also stages the
  removal of the hive's Argo application, manifests folder and values overlay there, for a pull request. The label
  defaults to the member's name (`--label` overrides; a rerun after the member is revoked needs it). After `--wait` seconds (default 3600) with agents still on
  the hive it stops with their names and leaves the hive draining; run it again to keep waiting. Every step skips a
  piece that is already gone, so a rerun finishes an interrupted remove. `OPENBAO_URL` and `OPENBAO_TOKEN` must
  be set.
- `agentihooks hive list` prints one line per hive: id, `live` or `stale`, ui, max agents, excluded (`none` when
  empty) and its [headroom](#headroom) reading.
  `agentihooks hive show <id>` prints the full record as JSON.

A Kubernetes worker pod joins the same way, waiting for `hive add` to deliver its credential; see
[Worker hives in Kubernetes](#worker-hives-in-kubernetes). A pod can instead mount a credential that
`agentihooks hive invite <label>` writes into OpenBao, which brings a new hive up from an empty namespace; see
[Reproducible hive](../reference/reproducible-hive.md).

## Repos and checkouts

Each heartbeat reports the repos the hive can work in. `hive run` scans one folder, `HIVE_REPO_ROOT`, default
`~/dev`: every direct child that is a git checkout, and every checkout one level below a child that is not one.
Worktrees count; their origin is read from the shared git directory.

For every checkout the hive reports two names, so a task matches whichever form its swarm uses:

- the absolute path, such as `/home/me/dev/agentihooks`;
- the origin identity, the `origin` remote as lowercase `host/owner/name`, such as
  `github.com/the-cloud-clockwork/agentihooks`. A checkout with no `origin` remote reports only its path.

```bash
# Workers keep their clones under /srv/repos instead of ~/dev
HIVE_REPO_ROOT=/srv/repos agentihooks hive run

# The repos the swarm sees for one hive
agentihooks hive show lab-vm        # the "repos" list
```

Placement drops a hive whose list lacks the task's repo, with "no hive for `<role>` lists the repo `<repo>`"
("no headless hive for ..." when only headless hives were tried).
When a launch names the repo by origin identity, the hive starts the agent in its own checkout with that origin,
the first by path when it holds several. A repo given as a path launches in that path.

`swarm prepare` and `swarm publish` search the same `HIVE_REPO_ROOT` checkouts by repository name when a task
repository entry is not a directory on this host, such as a path recorded on another host; see
[Repositories recorded on another host](swarm.md#repositories-recorded-on-another-host).

## Hive endpoint limits

`agentihooks hive serve` answers `POST /hive/refresh` (a ledger credential for a new Redis password) on one thread
per request; any other path answers 404, like any unknown path. Every limit in this table is a constant in the
service; no flag or environment variable changes one.

| Limit | Value | Effect |
|---|---|---|
| Request body | 4096 bytes | A missing, oversize or non JSON body answers 400. |
| Request socket timeout | 10 seconds | Bounds the TLS handshake and each read of the request. |
| Concurrent handlers | 32 | A further connection waits until a handler gives its slot back. |
| Request deadline | 30 seconds | The service shuts down the client socket of a request older than this. |
| Redis socket timeout | 5 seconds | Bounds each socket operation of every Redis call the service makes, not a whole request: a request that makes several calls can hold its slot longer, and a Redis that keeps sending within 5 seconds is not cut off. A failed call is never retried. |

The request deadline does not interrupt a handler waiting on Redis, which holds a separate connection, so the
Redis client's own timeout is what frees a handler while Redis stalls. The swarm Redis client reads with a
ten-second timeout; the hive endpoint lowers it to five seconds. A stalled call then raises after five seconds, the
handler answers 503 and its slot is free for the next request.

The service applies the bound when `agentihooks hive serve` builds the server. It sets the socket timeout on the
Redis client it was given, sets the retry count to zero and drops every connection opened before, so no pooled
connection keeps the earlier timeout. The swarm Redis client's three-second connect timeout is unchanged. It applies
to each address tried, not to name lookup, and a `socket_connect_timeout` query argument in the Redis URL overrides
it. A Redis that refuses the connection answers 503 at once; one that accepts it and stays silent answers after five
seconds.

### Answers

| Status | Body `error` | Cause |
|---|---|---|
| 200 | none | The credential was accepted; the body carries the grant. |
| 400 | `the body must be a JSON object with a string ledger_credential` | The body is missing, over 4096 bytes, not JSON, or lacks that string field. |
| 403 | the refusal reason, such as `the ledger credential belongs to no hive member` | The credential is not valid, or Redis returned an error other than a timeout, a lost connection or a refused login while the service refreshed the member. |
| 404 | `not found` | The path is not `/hive/refresh`. |
| 503 | `the hive Redis did not answer; try again` | A Redis call timed out, the connection to Redis was refused or dropped, or Redis refused the service's own login (`WRONGPASS`, `NOAUTH`). |

The 503 text is fixed and carries no Redis detail. The service writes no request log, so for an answered request the
client's reply is the only record.

### What a client sees

```text
$ agentihooks hive refresh
hive refresh refused: the hive Redis did not answer; try again
```

The command exits 1. The bound is per Redis call. The client may report the timeout instead of the 503:

```text
hive refresh refused: the hive at https://swarm.example.internal:8770 is unreachable (timed out)
```

A hive whose own Redis credential was refused refreshes it through this service. When the service answers 503, the
swarm Redis client raises `AuthenticationError` with `the hive Redis credential was refused and the hive refused a
refresh (the hive Redis did not answer; try again)`. The `hive` commands print a `refused` line only for their own
refusals. This error comes from the swarm's `redis_client()` helper, so commands that open the swarm Redis, such as
`hive list`, `hive set`, `hive show` and `hive run`, end with a traceback whose last line carries this text.

### Check the bound against a live service

A made-up credential reaches Redis through the service without touching a real member. Add `--cacert` for a
private certificate authority.

```bash
curl -sS -m 15 -w '\nstatus %{http_code} in %{time_total}s\n' \
  -d '{"ledger_credential": "not-a-real-credential"}' https://swarm.example.internal:8770/hive/refresh
```

| Redis state | Output |
|---|---|
| Answering | `{"error": "the ledger credential belongs to no hive member"}` and `status 403` at once |
| Accepting the connection, never replying | `{"error": "the hive Redis did not answer; try again"}` and `status 503` after about 5 seconds |

The CI test `test_a_request_stalled_inside_redis_answers_and_frees_its_slot_within_the_bound` runs this route
against a listener that accepts connections and never replies, with one handler slot and a Redis timeout of one
second: it answers 503 and the slot is free within twice that timeout.

### Hive endpoint troubleshooting

| Symptom | Reading | Action |
|---|---|---|
| `hive refresh` answers 503 | The refresh did not complete, or completed without the reply reaching the client. | Run `agentihooks hive refresh` again. Each success replaces the previous Redis password, and the command takes a per-host lock so two callers do not retire each other's password. |
| Every refresh answers 503 | Redis is stalled, unreachable or refusing the service's connection. | From the swarm host, run `agentihooks hive list`. It opens its own Redis connection with a 3-second connect and 10-second read timeout, so an error or a pause there points at Redis and not at the hive endpoint. Fix Redis, then repeat the request. |
| 403 text `Redis refused the refreshed member (...)` | Redis returned an error other than a timeout, a lost connection or a refused login; see Answers. | Read the detail in the message. |

## Member keys

A member's Redis user `hive-<id>` reads every key under the swarm root and writes only the member's own hive key and
the keys below it. With the default root `agentihooks` (set by `AGENTIHOOKS_SWARM_KEY_PREFIX`), the member
`lab-vm` gets these key patterns:

```text
%R~agentihooks:*            read every swarm key
~agentihooks:hive:lab-vm    write the hive record
~agentihooks:hive:lab-vm:*  write everything below it
```

Every other write, including another member's keys, the controller's inbox and the launch slot rows, is refused
by Redis with `NOPERM`. So every piece of state a member process writes lives below its own hive key:

| State | Where a member keeps it | Who reads it there |
|---|---|---|
| Launch order receipt and acknowledgement | `<root>:hive:<id>:...` | The controller, which records the execution as working. |
| Inbox items, sequence counters, delivery owners, seen marks and the master pool | `<root>:hive:<id>:inbox:...` | The member's own agents; the controller routes sends to the member's agents there and relays mail out of it. |
| Session readings: heartbeat, prompt, session model, progress, wait, waited, woke, wait history and stop blocks | `<root>:hive:<id>:swarm:...` | The controller's idle and wait checks, and the progress and stop gates, for agents placed on that member. |
| Sessions report, including the executions it runs | `<root>:hive:<id>:sessions` | The controller's launch slot refresh. |

### Which keys a process uses

A process counts as the member only while it talks Redis as that member: `AGENTIHOOKS_HIVE_ID` is set and the
Redis URL it resolves is `AGENTIHOOKS_HIVE_REDIS_URL`. `hive add` delivers both in `hive.env`; a process resolves the
hive Redis URL only when `AGENTIHOOKS_DEPLOYMENT` is `compose` or `distributed`. Any other process, the
controller included, uses the controller keys under the plain root.

The controller finds an agent's member from the agent's execution record: the hive it was placed on, when that hive
is in the hive index and holds a member record. An agent on any other host, and every agent in local mode, keeps
its inbox and session readings in the controller keys under the plain root. Those keys are the same as before
member scoping, so a local install and a single host install see no change.

### Mail across members

- **Into a member.** A controller send to an address that a member runs, by name or by the seat it occupies, is
  written into that member's inbox keys. The member's agents read and answer it there.
- **Out of a member.** A member writes mail to any other address, such as `master@<slug>` on the swarm host, in its
  own inbox keys, where it waits. Every swarm tick relays each waiting item to its address's inbox with the same item
  id and withdraws the member's copy with the reason `relayed to its address's inbox`. A relay retry after a lost
  response finds the id already sent and only withdraws the copy.
- **Refused sender.** The relay withdraws an item whose sender the member does not run, with the reason
  `refused: the hive member that sent it does not run its sender`. A member cannot send in another agent's name.
- **Master pool.** A member pools mail across live masters only while every live master's inbox sits in the same
  keys as the sender's. When their inboxes span the controller and a member, or two members, the item goes to its
  address unpooled.

The flow for an engineer that a member runs and the master on the swarm host:

```bash
# On the member: the engineer reports to the master; the item waits in the member's inbox keys
agentihooks msg send master@rig "the pull request is green and queued"

# On the swarm host, after the next tick relays it
agentihooks msg inbox              # run as the master: the item shows with the same id
agentihooks msg reply <id> "thanks, take the next task"
# The reply is written into the member's inbox keys, where the engineer reads it
```

### Launch slot rows

Launch slot rows count how many executions hold each account slot. Only the controller writes them; a member never
touches them. Reserve and acknowledge run in the controller, and every tick the controller refreshes the rows of
member executions from what each member reports:

1. A launch reserves a row for at most 300 seconds while the launch order is unacknowledged.
2. The controller reads the member's acknowledgement and marks the row live, kept for 45 seconds.
3. Every swarm tick reads the sessions report of each live hive, a heartbeat under ninety seconds old, and keeps the
   live rows of the executions it reports alive for two ticks, 120 seconds at the default tick. Only executions
   whose agent record places them on that same hive count, so a hive cannot hold a slot for an execution it does not
   run.
4. A row whose hive stops reporting the execution, or stops beating, is no longer refreshed. It lapses within two
   ticks and the slot is free for the next launch.

`AGENTIHOOKS_CONTROLLER_TICK_SECONDS` sets the tick, and with it the refresh lifetime.

### Checking a member's scope

Read the Redis user's key patterns on the swarm Redis, as an admin user:

```bash
redis-cli -u "$AGENTIHOOKS_SWARM_REDIS_URL" ACL GETUSER hive-lab-vm
```

The `keys` field lists the three patterns above. On the member, a write outside them fails with `NOPERM`; this
probe tries to write a key in the controller's inbox, which the member must not reach:

```bash
redis-cli -u "$AGENTIHOOKS_HIVE_REDIS_URL" SET agentihooks:inbox:probe 1
# (error) NOPERM ...
```

The packaged smoke tests check the same scope end to end: `scripts/packaging/hive-join-smoke.sh` adds a member on
Redis 7 and asserts that the shared inbox, shared session readings and another member's keys are refused, that the
member's own inbox and readings are allowed, and that the relay refuses a sender the member does not run.
`scripts/packaging/compose-hive-smoke.sh` relays a member agent's mail to the master and reads the host's reply from
the member's own keys.

### Limitations

- The controller's wake and escalation pass, exit sweep, swarm status, handoff mailbox and swarm removal read only
  the controller inbox. Mail held for an agent a member runs gets no wake, escalation or exit settlement, and the
  controller's seen marks for such agents stay in the controller keys.
- A member-hosted agent cannot yet complete a life: its agent record writes, such as order consumer binding, take
  master, done, handoff and seat exits, sit outside its keys and are refused.
- A member's master pool, filled while every live master sat on that member, drains only on the next send there.
- The launch slot lifetime of two ticks assumes a tick finishes within one tick interval.
- The pane cleanup's alias prompt scan reads only the controller's prompt keys.

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Mail a member agent sent never reaches the master | No swarm tick has run since the send; the relay runs once per tick. | Check the swarm is started and its controller lease is held: `agentihooks swarm <slug> status`. |
| A member item closes as `refused: the hive member that sent it does not run its sender` | The sender named on the item is not an agent placed on that member. | Send from the agent's own session on the member; the sender is the session, never an argument. |
| A member process fails a write with `NOPERM` | The process writes outside its hive key, such as an agent record or a launch slot row. | Expected for agent record writes, see Limitations; anything else is a bug to file with the key named. |
| The controller shows a member agent idle though it works | The agent's execution record names a hive with no member record, so the controller reads its readings from the controller keys. | Check `agentihooks hive show <id>` lists the hive and that it was added with `hive add`. |
| A slot stays taken after a member lost an agent | The member still reports the execution. | The row lapses within two ticks once the execution leaves the member's sessions report. |

## Settings

Set them with `agentihooks hive set <id> key=value ...`, or on the ledger page's
[Hives panel](#ledger-hives-panel), which writes through `PATCH /api/v1/hives/<id>` and needs the operator.

| Setting | Values | Default | Meaning |
|---|---|---|---|
| `name` | text | the hive id | Display name. |
| `ui` | `yes`, `no` | `no` | The machine has a screen and a person at it. |
| `ephemeral` | `yes`, `no` | `no` | The machine can disappear, such as a pod. Placement prefers lasting hives on a tie. |
| `excluded` | comma list of lane roles or profiles, such as `master,frontend,qa` | `master` on `ui=no`, none on `ui=yes` | Work the hive never takes. An empty value excludes nothing. |
| `prefer` | comma list of `role:rank` | none | Rank 1 is chosen first for that role; unranked hives come after ranked ones. |
| `max-agents` | whole number, 1 or more | `20` on `ui=no`, `10` on `ui=yes` | Working agents the hive runs at once. Masters do not count against it. |

Every hive takes every agent type (master, eng, ci, plan, dispatch and any profile) unless the agent's lane role,
role or profile is in its `excluded` list. `prefer` refuses a role the hive excludes. An unset `excluded` or
`max-agents` follows the hive's current `ui`. A hive registered with the old `roles` setting drops it, with its
stored `max-agents`, on its next `hive set` or heartbeat, and reads under the defaults until then.

### Excluded and prefer: the operator's layout

The operator runs a PC, a VM and a set of pods:

```bash
agentihooks hive set pc  ui=yes excluded=qa,frontend,eng,ci,plan prefer=master:1
agentihooks hive set vm  ui=yes excluded=eng,ci,plan prefer=master:2,qa:1,frontend:1
agentihooks hive set pod-1 ui=no ephemeral=yes max-agents=20
```

- The master runs on the PC while it is live, and on the VM when the PC is gone; the pods exclude it by default.
- QA and frontend work runs on the VM, then on a pod when the VM is gone.
- Engineer, CI, planner and dispatcher work runs on the pods, and on the PC when every pod is full once the PC no
  longer excludes that role.

## Comparing hive configuration

Every hive runs agents with the workstation's configuration. Each beat keeps the hive's configuration published,
named without values or host paths: account token names, agentihooks env setting names, the linked bundle and
profiles, installed Claude rules and skills, managed MCP servers, satisfied bundle dependencies and repo identities.
The snapshot lives fifteen minutes and is rebuilt once under a minute is left, so dependency checks run rarely.

```bash
agentihooks hive compare               # this machine's hive is the base
agentihooks hive compare --base root
```

The first line is the base with a count per section; every other live hive follows as `equal`, `differs` with one
`<section> missing|extra <name>` line per difference, or `no configuration published`. The command exits 1 when
any hive differs or published nothing.

## Placement

Every launch places its agent on one hive. For a worker role, placement keeps the live hives that do not exclude
its lane, role or profile,
then drops, in order, those with no room under `max-agents`, those that do not advertise the account slot the
router picked, those that do not list the task's repo, and, for an interactive login, those that do not hold that
login. Each step that empties the list names its reason, and the launch waits with it.

Among the hives left, placement picks the lowest `prefer` rank for the role, then the most room, then a lasting
hive over an ephemeral one, then the hive id.

`eng`, `ci`, `plan` and `dispatch` prefer headless hives, and `master`, `qa` and `frontend` prefer UI hives.
Placement tries the preferred kind first and takes the other kind only when no preferred hive taking the role has
room or none is live.
A headless hive with room that lacks the account slot, the repo or the login makes the launch wait instead. The
agent record carries the fallback reason as `placement_fallback` in its profile decision; a task pinned to a UI
hive records the pin as its reason. The engineer, cicd, planner
and dispatcher profiles place as `eng`, `ci`, `plan` and `dispatch`; a frontend or qa profile on any lane places as
its UI role.

When no live hive takes the role:

- Every role waits with "no live hive takes" the role.
- The master has its own chain, below.

A hive counts as live while its last heartbeat is under ninety seconds old. Codex launches are not placed on hives.

### Headroom

A hive's room is the smaller of its `max-agents` left and what its measured headroom fits. `hive run` publishes the
headroom reading to `<root>:hive:<id>:headroom`, and placement, the launch reservation and `hive list` read it from
there.

A reading holds:

| Field | Source |
|---|---|
| `cpus`, `load5` | CPU count and the five minute load average from `/proc/loadavg`. |
| `available_mb`, `total_mb` | Available and total memory from `/proc/meminfo`. |
| `pod_total_mb`, `pod_available_mb` | The cgroup v2 `memory.max` limit and that limit minus current use less inactive file pages, only when the limit is set. |
| `pod_cpus`, `pod_cpu_usec`, `pod_load` | The cgroup v2 `cpu.max` quota and usage; `pod_load` needs a previous reading under 30 seconds old, so the pod CPU limit does not apply to the first reading after a start or a gap. |
| `pending` | Launch orders waiting in the hive's order stream, shown by `hive list`. Room counts the hive's pending launch reservations instead. |
| `measured_at` | Milliseconds when the reading was taken. |

The reading expires after 30 seconds, and a reader treats one older than that as absent. With no fresh reading the
hive's room is 0, the refusal reads `missing telemetry: no fresh headroom reading`, and `hive list` shows
`headroom=missing`. Otherwise each limit fits whole agents at 0.5 CPU and 700 MB each under 80 percent of the host,
and of the pod when the cgroup sets a limit; launches reserved on the hive in the last fifteen minutes count against
each limit, and the smallest limit wins.

`hive run` publishes headroom on two paths:

- **Every beat,** right after the heartbeat is written and before launch orders, herdr reports and execution reports,
  stamped with the heartbeat time. A beat that fails after the heartbeat still leaves a fresh reading.
- **Every 10 seconds on its own thread,** started for each Redis connection. A launch inside a beat can run
  `init-agent` for minutes; the thread keeps the reading fresh while it does, and also renews the daemon's order
  consumer marker (45 seconds), so a launch it still holds is not taken for an orphan. The thread paces with the
  scheduler process timer, like the beat. A Redis error, or a reading that cannot be parsed, prints
  `hive headroom publish failed (<error>: <detail>)` to stderr and the thread keeps going. When its timer connection
  is lost the thread ends, and the reconnect starts a new one. Any other error ends the thread with a traceback until
  the next reconnect; the beat still publishes on its own path, but a launch longer than 30 seconds then lapses the
  reading.

`hive list` appends the reading to each hive's line, its fields separated by tabs (shown aligned here):

```bash
agentihooks hive list
# vm  live  ui=no  max-agents=20  excluded=none  cpus=8  load5=1.42  available-mb=21034  pending=0
```

Hive workers load code at start. A hive still on code from before the 10 second publisher writes headroom only
after the beat's launch poll, so its reading lapses during a long launch; restart its `agentihooks-hive` service,
or roll its pod, to pick up the publisher.

| Symptom | Check | Fix |
|---|---|---|
| `hive list` shows `headroom=missing` on a `live` hive | `journalctl --user -u agentihooks-hive`, or the pod log, for `hive headroom publish failed` | Fix the error it names; a Redis error clears when the connection returns. |
| A launch waits with `missing telemetry` while another launch runs on that hive | The hive's agentihooks version | Upgrade and restart the hive service so the 10 second publisher runs. |
| Room reads 0 with a fresh reading | The refusal detail: `cpu`, `ram` or `max agents` | The named limit is spent; wait for agents to finish or raise `max-agents` when the host has room. |

### Account caps on every hive

Quota belongs to the account, so every hive reports the same cap for it. A subscription slot whose own reading is
missing or older takes the cap and reading of the newest one another live hive published for that account. A hive
that holds a token but never probes it, such as a worker pod, still shows the account's real cap; interactive and
api slots keep their own.

A hive's subscription cap scales with quota left: the ceiling `AGENTIHOOKS_HIVE_SESSION_CEILING` (default 12) times
the smaller of the five hour and week percentage left, at least the band in
[Claude account load balancing](load-balancing.md) and at most the ceiling, which wins when set below the band. An
account at 96% of both windows left reports 11; one with under 10% of its five hour window left keeps the band's 2;
under 5% of either window it reports 0. Worker admission under a hive or cluster spawn backend reads every Claude
row this way, interactive slots included. Launches routed by one machine's own processes keep the band.

### Master placement

Every master seat, the lead and seats two and up, follows the same host rule as role placement: it runs only on a
live hive that does not exclude `master` and lists the repo. UI hives come before headless ones, so a worker pod,
which excludes `master` by default, runs a master only when its `excluded` list drops it. Within each kind it is
placed in two tiers, each tried in `prefer` order:

1. A hive that holds the native interactive login for the master's harness.
2. A hive with a subscription or api slot for that harness.

A hive names a subscription slot by its `AH_CC_TOKEN_<slug>` slug; a hosted controller names the same account by
its Secret key. The two may differ only in `_` against `-`, and master placement treats them as one account. A
master on a hive is held to the session cap that hive reports for the account, not the hosted controller's cloud
worker cap. The hive computes that cap from its own quota reading of the account, with the bands in
[Claude account load balancing](load-balancing.md), and reports 0 while it has no fresh reading. When the
reservations on the account already reach the cap, the launch is not started and reports "account reservation is
full".

### Who writes placement

A member's Redis user reads agent records and placement but never writes them. Only the controller and the ledger
server write placement; add, refresh and revoke keep the hive index.

| Keys under the swarm root | Member Redis user | Written by |
|---|---|---|
| Agent records: `swarm:<slug>:agents`, `swarm:<slug>:executions`, `swarm:<slug>:state-since` | read only | The controller and the ledger server. |
| Claims, seats, names and placement waits | read only | The controller and the ledger server. |
| Launch slot rows `launch-slot:*` | read only | The controller, which reserves, acknowledges and refreshes them. |
| Hive index `hives` | read only | `hive add` and `hive refresh` add the member; `hive revoke` and a failed add remove it. |
| The member's own hive record and every key below it | read and write | The member's `hive run` beat and the agents it runs. |

The ledger issues an agent token to a member only for an agent whose record names that member as its hive. A member
that could write agent records could set that field on another hive's seat and take tokens for it; with agent
records read only, a member holds tokens only for the agents the controller placed on it.

What a member process does differently:

- **Beat.** `hive run` writes the hive record, sessions, quota and execution report under its own hive key. It adds
  itself to the hive index and refreshes launch slots only when it is not a member. A non-member host, such as the
  swarm host itself, still does both.
- **Hive id.** A process uses `AGENTIHOOKS_HIVE_ID` as its hive id while its Redis URL is
  `AGENTIHOOKS_HIVE_REDIS_URL`, so its heartbeat lands under the member's own keys and matches the id the ledger
  checks. Otherwise the hive id is `SWARM_HIVE_ID`, then the seeded hive id, then the host name.
- **Launch orders.** When Redis refuses a write while the order consumer applies a launch order, the consumer writes
  `failed` with the reason `refused` to the member's own acknowledgement hash and drops the order. The `hive run`
  service keeps running and takes the next order.

Check a member's scope from the member, with the credentials `hive add` delivered:

```bash
. ~/.agentihooks/hive.env
redis-cli -u "$AGENTIHOOKS_HIVE_REDIS_URL" HGETALL agentihooks:swarm:<slug>:agents          # allowed
redis-cli -u "$AGENTIHOOKS_HIVE_REDIS_URL" HSET agentihooks:swarm:<slug>:agents forged '{}'  # (error) NOPERM ...
redis-cli -u "$AGENTIHOOKS_HIVE_REDIS_URL" SADD agentihooks:hives forged                      # (error) NOPERM ...
```

`scripts/packaging/hive-join-smoke.sh` runs the same check on Redis 7 after an add. The member's writes to agent
records, executions, another member's keys, the hive index and a launch slot row are refused. Its beat, an agent
heartbeat and an inbox send succeed, and the member stays in the hive index. `scripts/packaging/compose-hive-smoke.sh`
also checks that a member stays indexed after a Redis restart and a refresh.

Limitations:

- Code on a member host that writes agent records is refused until it routes through the controller or the ledger:
  the order consumer's runtime binding, remote take master, done, handoff, claim release and seat exits. A
  distributed launch onto a member fails as `refused` until then.
- Redis ACLs scope by key pattern, never by seat or agent name. A member that can write its own agents' keys can
  also write keys it names for another agent below its own hive key. Token issuance reads only the agent record's
  hive, which no member can write.

| Symptom | Cause | Fix |
|---|---|---|
| A launch order on a member acknowledges `failed` with `refused` | The launch wrote a key outside the member's keys, such as an agent record. | Expected for member launches that bind the agent record on the member; see the limitations above. |
| A member is missing from `agentihooks hive list` | The hive index no longer holds it, and a member's beat never adds itself back. | Run `agentihooks hive refresh` on the member; refresh adds it to the index again. |
| A member is refused an agent token | The agent's record names another hive, or no hive. | Read the agent's `hive` field with `HGET agentihooks:swarm:<slug>:agents <name>`; only the controller places an agent. |

## Workstation fallback

When no UI hive fits, because none is live, or the live ones lack the repo, or lack both a slot and the interactive
login for the master's harness, the hive controller leaves the master to the workstation herdr server. It does the
same when the hive runtime is disabled on the controller. When the controller's own herdr server answers, the
controller spawns the master there. Otherwise it refuses the launch with "the workstation hive spawns master work"
followed by the placement reason, and the tick reports that line in its actions.

The `master-headless` health finding marks a master record placed on a headless hive before this rule. The
`master-fallback-auth` finding marks a master that wanted its interactive login and runs on another route, so phone
access through Remote Control is lost.

Placement runs at every master launch, so the successor after the next handoff goes back to the best UI hive that
is live then. `agentihooks swarm <id> master move --to HIVE` forces that move without waiting: the master hands
off and its successor starts on the named hive.

## Prompt and workspace transport

A hive launch runs the agent on another machine, which has no copy of the ledger and no task work folder. The
controller therefore builds everything the agent reads at start and sends it in the launch payload; the hive sends
the agent's work folder lines back every heartbeat.

### What the launch payload carries

When placement picks a hive, the controller writes the payload to
`<root>:swarm:<slug>:launch-prompt:<execution id>` (`<root>` is `AGENTIHOOKS_SWARM_KEY_PREFIX`, default
`agentihooks`). The key expires with the 300 second launch slot reservation. Next to the task fields and the
profile decision it holds:

| Field | Content |
|---|---|
| `hive_opening` | The full opening prompt, built on the controller where the ledger is, for the Claude harness. |
| `hive_opening_digest` | The sha256 hex digest of `hive_opening`. |
| `hive_seeds` | The task's work folder files: `steering.md` whole, and the last 65,536 characters of `progress.md` and `proof.md`. Empty when the task has no work folder. |

The opening names the work folder as `~/.agentihooks/swarm/<slug>/tasks/<task id>`, so the same text is correct
below whichever home the hive runs the agent in.

### What the hive does with it

An order the hive picks up after its 300 seconds is acknowledged as failed with the reason `expired` before the
payload is read. Otherwise the hive checks the payload and acknowledges the order as failed with the reason `prompt`
when:

- the payload key is missing;
- the payload is not JSON, or its task id differs from the order's;
- `hive_opening` is missing or not text, or `hive_opening_digest` does not match it.

The controller ends the execution, drops the launch order and reports the spawn refused with
`hive launch failed: <reason>`.

A payload that passes is written below the hive's own home, in `~/.agentihooks/swarm/<slug>/tasks/<task id>`:

- `steering.md` is replaced with the controller's copy on every launch.
- `progress.md` and `proof.md` are written only when the hive does not hold them yet. On a relaunch of the same task
  on the same hive, its own lines stay and the controller's copy is not merged in.

The agent then starts with `hive_opening` exactly as carried. The hive does not rebuild the prompt from the task.

### Work folder lines on the ledger page

On every fifteen second heartbeat, each hive writes the work folder lines of the executions it hosts to
`<root>:hive:<hive id>:workspaces`, expiring after 45 seconds. Each entry holds `latest_progress` and
`latest_proof`: the last three non-blank lines of `progress.md` and `proof.md`, read from the last 4,096 bytes of
each. A key is left out when its file has no such line. The copy covers every swarm task whose latest execution,
the live one or else the one started last, is a worker on that hive. Masters are left out.

When the controller publishes swarm status, each task with a work folder takes its lines from the hive that runs,
or last ran, its latest execution. A task whose latest execution ran on the controller's own machine is read from
the local folder. These lines are what the ledger page shows in the task's Contract and proof fold.

### Resume and recovery prompts

The restore prompt after a reboot and the resume prompt after an outage tell the agent to re-read its task folder.
For an agent on a remote hive both prompts name `~/.agentihooks/swarm/<slug>/tasks/<task id>`, the folder below the
hive's home, instead of the controller's absolute path.

### Upgrading

A hive refuses a launch payload without `hive_opening` and its digest, so an older controller cannot launch on a
newer hive. Deploy the controller first, then the hives.

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Launches on one hive end with `hive launch failed: prompt` | The controller predates prompt transport and sends no opening and digest. | Upgrade the controller, then the hive. |
| Launches on one hive end with `hive launch failed: expired` | The hive picked up the launch order more than 300 seconds after the controller sent it. | Check that the hive beats (`agentihooks hive list` shows `live`) and its `agentihooks-hive` service runs. |
| A remote task's Contract and proof fold is empty | The hive stopped beating for more than 45 seconds, so its synced copy expired, or the agent has not written a line yet. | Check `agentihooks hive list`. The lines come back on the hive's next heartbeat. |
| A relaunched agent on the same hive does not see proof lines written on another machine | `progress.md` and `proof.md` already on a hive are kept, never merged. | Copy the missing lines into the hive's work folder, or append them from the agent. |

## Failover timing

| Clock | Value | Effect |
|---|---|---|
| Hive heartbeat | every 15 seconds | Sessions, quota and reports expire after 45 seconds without a beat. |
| Headroom | every 10 seconds and every beat | A reading older than 30 seconds counts as missing and the hive's room reads 0; see [Headroom](#headroom). |
| Live window | 90 seconds | A hive past it is stale and gets no new launches. |
| Minute tick | 60 seconds (`AGENTIHOOKS_CONTROLLER_TICK_SECONDS`) | Each tick checks the master's hive. |
| `master-move-grace` | 300 seconds | How long the master's hive may go without a heartbeat before the tick moves the master. |
| Controller lease | three ticks, 180 seconds by default | Another controller takes over at a higher epoch once it lapses. |
| Worker execution lease | 240 seconds | A Swarm v2 Pod worker keeps its task through a controller handover; see [worker execution lease through a controller rollout](swarm.md#worker-execution-lease-through-a-controller-rollout). |
| Launch slot reservation | 300 seconds | A started launch order not acknowledged by then is recorded as ambiguous. |
| Launch slot refresh | two ticks, 120 seconds by default | Each tick keeps the live slot rows a live hive reports; see [Launch slot rows](#launch-slot-rows). |

The master moves when its hive's heartbeat is older than the grace and the runtime does not see the master live,
so a lost master hive is replaced between five and six minutes after its last beat at the defaults. A hive that
never beat, or whose record is gone, never moves its master. Set the grace with
`agentihooks swarm <id> set master-move-grace=SECONDS`.

The steps of the move are in [Automatic failover](#automatic-failover).

## Master failover and master move

The master seat changes hive in two ways: the minute tick moves it off a hive that went quiet, and the operator
moves it to a hive he names. In both, the successor starts through hive launch in the same master seat. The
operator move pins it to the named hive; the automatic move pins it when a live hive qualifies.

### Automatic failover

Each tick checks the live master record. It moves the master only when all of these hold:

- The master's hive has beaten at least once and its record still exists.
- Its last heartbeat is older than `master-move-grace` seconds.
- The runtime does not see the master's session live.

A stopped swarm is checked only when an operator message is waiting for its master.

Then, in one tick:

1. It stores a recovery handoff for the master seat. The handoff says the master should continue on the swarm,
   names the hive and how many seconds ago it stopped beating, and marks Done and Stopped at as `unknown`, because
   the old master left no document. Its Read first list names the master inbox.
2. It records the transfer with the reason `hive-lost`.
3. It bumps the master seat generation and fences the old execution on the quiet hive.
4. It asks master placement for a target among the live hives and records the pending move with that hive.
5. It marks the old master record finished and logs `moved master <name> off lost hive <hive>` in the tick output.

The next master start reads the recovery handoff, pins the successor to the recorded hive and clears the pending
move once the successor is placed. When no live hive qualifies, the move records no target and the successor
starts unpinned on the normal master path.

Tune the grace per swarm:

```bash
agentihooks swarm rig-grade-swarm set master-move-grace=600
```

A hive that pauses longer than the grace, such as a laptop asleep, loses its master; a shorter grace replaces a
lost master sooner. The default is 300 seconds.

### Operator master move

```bash
agentihooks swarm rig-grade-swarm master move --to vm
```

The command checks the move before it writes anything, and refuses with these errors:

| Error | Cause |
|---|---|
| `no live master to move` | The swarm has no live master record. |
| `the master already runs on hive vm` | The target is the master's current hive. |
| `hive vm is not live` | The hive has no record, or its last heartbeat is over ninety seconds old. |
| `the master cannot move to hive vm: <reason>` | [Master placement](#master-placement) would not give the master that hive; the reason is the placement wait reason. |

On success it sends one durable inbox order from `operator` to the master seat and prints the pending move as
JSON, with `from`, `to`, `master`, `item` (the order's inbox id) and `state` set to `ordered`. The order tells the
master to finish its current step, write a Handoff v2 carrying its pending decisions, held messages, claimed task
needs, audit plan and operator overrides, run `agentihooks swarm <id> handoff <doc> --reason operator` and stop.
After that handoff, the tick starts the successor on the named hive and closes the order as done.

A second `master move` replaces the first: it cancels the earlier order in the inbox with "the master move was
replaced" and records the new target.

The successor is pinned only when its transfer follows the master the move named. A master that leaves without a
handoff, or a different master that takes the seat first, starts on normal placement and the pin is not applied.

The ledger page offers the same order: each live hive row in the Swarm tab's Hives panel carries a **move master**
button. It is disabled while another swarm control from the page is running, on a hive that is not live and on the
hive the master already runs on.

### Troubleshooting

- **The master's hive is down and the master did not move.** Check the hive's heartbeat with
  `agentihooks hive list`. A hive that never beat, or whose record was removed, never moves its master, and the move
  waits the full grace plus up to one tick. While the runtime still reports the master's session live, the tick
  leaves it alone.
- **The successor started on another hive than the one ordered.** The pin applies only to the successor of the
  master the order named. If that master left without the handoff the order asks for, its successor uses normal
  placement. Run `master move` again for the new master.
- **The old hive came back and its master is still running.** A returning hive terminates each fenced execution
  before its first heartbeat and takes no launch orders while one still runs; see [Fencing](#fencing).

## Fencing

Fencing stops a superseded agent or controller from acting after its replacement starts.

- **Seat generation.** A failover bumps the master seat's generation and records the old execution as fenced on
  its hive.
- **Returning hive.** Before it writes its first heartbeat, a hive that comes back terminates every fenced
  execution by its recorded process group. While one still runs it takes no launch orders.
- **Controller epoch.** Each controller holds a lease with an epoch. Launch orders and ledger writes carry it, and a
  write from an older epoch is refused with "the controller lease is stale". Release a lease by hand with
  `agentihooks swarm <id> controller release`.
- **Ledger source.** Moving a workstation ledger into the hosted service with `--freeze` fences the source: its
  SQLite database refuses writes with "ledger moved to hosted service" and media writes answer 409.

## Launches on a hive

The controller hands a hive a launch order; the hive's own Redis user receives it, launches the agent and writes
the acknowledgement the controller reads. What the order's payload carries and what the hive writes from it are in
[Prompt and workspace transport](#prompt-and-workspace-transport).

### Refusal causes

A launch the hive cannot complete is acknowledged `failed` with a cause, and the controller records the launch as
failed with "hive launch failed: `<cause>`".

| Cause | Meaning |
|---|---|
| `identity` | The order does not match the admitted execution: generation, lane, task, harness, profile, account slot, route, target hive or state differ, the seat holds a newer execution, or the started agent reported a different harness, profile or account. |
| `prompt` | The payload is missing, is not the order's task, or carries no opening prompt or a digest that does not match it. |
| `stale_epoch` | The order's controller epoch is older than the current controller lease when the hive takes it. |
| `refused` | The controller epoch moved on between the order check and the launch or right after it, or the launch raised a swarm error. |
| `expired` | The order expired before the hive took it, or the agent started after the expiry; the hive stops such an agent. |
| `ambiguous` | The agent may have started but its process could not be identified, or the order was taken 300 seconds ago with no result. |
| `<status>: <detail>` | The local runtime refused the spawn, such as `refused: <why>` or `unavailable: <why>`, with the runtime's own reason. |

## Hive placement for one swarm on a local machine

A machine in `local` mode spawns every lane in its own herdr panes. `agentihooks swarm <id> set hive-placement=on`
makes that one swarm's tick place like a `hybrid` controller: eng, ci, plan and dispatch work goes to joined hives
with the role and room, and the master goes to a UI hive. Every other swarm on the machine, and every ledger client,
stays local, because `AGENTIHOOKS_DEPLOYMENT` is unchanged. Before turning it on, run the machine itself as a hive
(`agentihooks hive install`, then `agentihooks hive set <id> ui=yes max-agents=N`) so local work and the master keep a hive to land on. `hive-placement=off` rolls back on the next
tick. On a machine in any other mode the setting changes nothing.

Hives fill first. When no hive takes an eng, ci, plan or dispatch spawn, the tick runs it in the machine's own herdr
panes while the machine runs fewer worker agents than `agentihooks swarm <id> set local-max-agents=N` (default 8),
counted across every swarm on the machine. At the cap the spawn waits with the hive's reason and the local count.
Masters, qa and frontend keep the UI host rule, and a task pinned to a hive never runs locally.

## Worker hives in Kubernetes

The `agentihooks-swarm` chart runs worker pods from the hive worker image. Each pod:

1. Points git at `GITHUB_PERSONAL_ACCESS_TOKEN` for github.com through a credential helper that reads the variable
   when git asks, and exports it as `GH_TOKEN` so gh is logged in; the token is never written or printed.
2. Clones the repos in `workers.repositories` under its repo root, and the bundle at `HIVE_BUNDLE_URL` and
   `HIVE_BUNDLE_REF` beside them as `agentihooks-bundle`.
3. Installs a mounted credential as `hive.env`. With no mounted credential and no `hive.env` yet, it starts sshd
   and waits for one, up to `HIVE_DELIVERY_WAIT_S` (default 900) seconds, then exits 1 with
   `worker-hive: no member credential was delivered within <n>s; run agentihooks hive add from the UI hive`.
4. Runs `agentihooks init` with the bundle linked, then `agentihooks deps ensure`. The role is rendered at spawn.
5. Sets itself `ui=no ephemeral=yes draining=no excluded=<workers.excluded> max-agents=<workers.maxAgents>`, by
   default `excluded=master max-agents=20`.
6. Runs `agentihooks hive run`.

When the pod stops, for an image update, a rollout or a node drain, its preStop runs `agentihooks hive drain --wait
<workers.drainSeconds>` (default 3600): the hive sets `draining=yes`, so placement sends it no new agent, and waits
until no agent it hosts is running. The grace period is `workers.drainSeconds` plus 30 seconds, so a Recreate rollout
starts the new pod only after the old hive's agents finish or the wait runs out.

The bundle origin and the token come from the hive settings record through `workers.envFromSecrets`. Write the
whole record from the workstation with one command:

```bash
agentihooks hive setup --dry-run   # key names it would write and leave out; writes nothing
agentihooks hive setup             # into secret/k8s/agentihooks-swarm-workstation-hive-settings
agentihooks hive setup --path <record>
```

It takes every key the agentihooks env files define (`.env` and each `*.env` in the agentihooks home, except
`hive.env` and `hive-container.env`), with its value from the agent environment, and prints key names only; a key
the environment does not hold is skipped. It leaves out account tokens, which the accounts records carry,
`SWARM_SECRET` and `AGENTIHOOKS_LEDGER_AGENT_TOKEN`, values that are host paths and values that name localhost, a
loopback, unspecified or link local address, naming each with its reason. It adds the bundle origin as `hive bundle-origin` does and keeps the record's other keys.

To write only the origin, read from the `origin` remote and current branch of the linked bundle, else of
`AGENTIHOOKS_BUNDLE_PATH`:

```bash
agentihooks hive bundle-origin                 # into secret/k8s/agentihooks-swarm-workstation-hive-settings
agentihooks hive bundle-origin --path <record> # another record under the secret mount
```

It needs `OPENBAO_URL` and `OPENBAO_TOKEN`, keeps the record's other keys, and writes an empty origin when no bundle
is linked. A linked bundle with no `origin` remote naming a host and repository is refused. An empty `HIVE_BUNDLE_URL`
clones nothing, unlinks any bundle a persistent home kept, and init runs on the package roles alone. A failed clone
logs `worker-hive: bundle clone failed` and the pod still serves without the bundle, which `agentihooks hive compare`
shows as a difference. A failed init with the bundle logs `worker-hive: agentihooks init with the bundle failed` and
init runs again on the package roles alone, with the bundle unlinked; a failed init on the package roles stops the
pod. A failed `deps ensure` logs its reason and each launch retries it. The image carries node, npm, uv and gh; npm
global installs and uv tools land in `~/.local` under the worker home.

`agentihooks hive join /path/to/invite` enrolls a machine from one private invite
file. It reads the file without executing it, refreshes the member through the
hive endpoint and saves its Redis and NATS credentials privately. The hub operator
issues that file with `agentihooks hive invite`, using `--bus-url` for the NATS
address reachable by the member. A `--like` invite copies that address. Bus and
ledger addresses survive later credential refreshes. A private hub CA must be
trusted before joining, for example through `SSL_CERT_FILE`.

The hive only Helm example uses the same invite: create a Secret with
`kubectl create secret generic hive-invite --from-file=invite=/path/to/invite`,
then install the chart with `-f deploy/helm/agentihooks-swarm/examples/hive-only.yaml`.
It deploys one worker and no hub or NATS server. Worker startup runs `hive join`
before configuring the hive or accepting work, and refuses startup if enrollment
fails. GitHub and provider account credentials remain separate Secret references.

`workers.credentialSecret` is required; it carries the member invite and, with `workers.ssh.enabled`, also the
sshd `authorized_keys` and host key the pod installs for `agentihooks hive add` to reach it. Credentials come from
ExternalSecrets by name. Replicas that mount the same credential would report the same hive id and agent limit, so
the chart refuses `workers.replicas` above 1. Scale with `workers.hives`: each entry, `name` plus its own
`credentialSecret`, renders one more hive pod, `<release>-worker-hive-<name>` with component `worker-hive-<name>` and
its own worktrees ConfigMap. Every other key the entry sets replaces that `workers` key whole, so an entry that sets
`homeVolume` gives the complete source. The chart refuses an entry without a credential of its own, an entry
that sets `replicas`, an entry whose hostPath or claim home is the same path or claim as another hive's, a
repeated name, or a name that is not a lowercase DNS label of at most 20 characters. Add a pod with no installed
credential with `agentihooks hive add` from the UI hive; the pod name is used only then.

The pod's memory follows its agent count: `workers.memoryPerAgentMi.request` (default 700) and `.limit` (default
2048), in MiB, are multiplied by `workers.maxAgents` into the container's memory request and limit, so the scheduler
places the pod only on a node with room for every agent it may run. The chart refuses a request below one or a limit
below the request, and memory set under `workers.resources`. `workers.nodeSelector`, `workers.affinity` and
`workers.tolerations` place the pod; by default it schedules on any node and never uses the host network.

`workers.homeVolume` is the volume source of `/home/worker`, which holds the repo clones, worktrees and scratchpad. It
defaults to an emptyDir on the node root disk; a `hostPath` of type `Directory` on a data disk, or a
`persistentVolumeClaim` (set `emptyDir: null` beside it; the chart refuses more or fewer than one source) keeps them on
a larger disk. A hostPath of type `Directory` starts the pod only where the path exists, so create it only on the
mounted data disk. Any source other than an emptyDir rolls the pod out by
Recreate so two hives never share it. `workers.worktreeMinFreeGb` (default 50), `workers.worktreeMaxPerRepo` (default 60)
and `workers.worktreeStaleDays` (default 7) reach agents as `WT_MIN_FREE_GB`, `WT_MAX_PER_REPO` and `WT_STALE_DAYS`
through the release's worktrees ConfigMap: `wt.sh new` evicts the oldest safe worktrees to keep that floor and cap, and
`agentihooks gc` removes worktrees untouched that many days.

A distributed controller can also serve a workstation swarm without worker hives, launching that swarm's engineer and
CI work as Pods while its master, planners and dispatcher stay on the workstation; see
[Serving a live swarm from a distributed controller](../reference/deployment-modes.md#serving-a-live-swarm-from-a-distributed-controller).

## Ledger Hives panel

The Swarm tab of the ledger page shows a Hives panel after Quota. It reads the hive settings API, edits hive settings
in place and moves the master to another hive.

### Panel

| Column | Shows |
|---|---|
| hive | The hive's name; hovering shows its id. |
| state | `live` while the last heartbeat is under ninety seconds old, else `quiet`. |
| ui, ephemeral | `yes` or `no`, editable. |
| excluded | Comma list, editable. |
| prefer | Comma list of `role:rank`, editable. |
| max agents | Whole number, editable. |
| agents | Working agents against max agents, such as `1/3`. |
| slots | One line per slot: harness, account and the sessions the hive reports on it. |
| master | The live master running on the hive, from any swarm, or `—`. |
| (no header) | The move master button, which moves this ledger's master to the hive. |

The agents figure is the placement load: worker sessions the hive reports, without its interactive master
sessions, plus this runtime's launching, working or suspect non-master agents on the hive that the hive has not
reported yet. Placement compares the same figure with max agents.

- The header count reads `<live> live · <n> hives`, or `—` when the read failed.
- The Quota header count ends with each hive's `<name> <agents>/<max agents>`, such as
  `2 accounts · probed 40s ago · laptop 1/3 · pod-a 0/4`.
- The panel reads `GET /api/v1/hives` at most every fifteen seconds while the Swarm tab is open. An empty table reads
  `Reading hives…` before the first answer, `No hives registered.` when the registry is empty, and
  `Could not read hives.` when the read failed.
- Swarm tab redraws skip the table while one of its fields holds focus; each fresh read still redraws it.
- The panel folds and keeps its folded state, its height and its column widths across reloads, like the other Swarm
  tab panels.

### Editing a hive

Change a field and leave it, or pick a new `yes` or `no`: the page sends that one field as
`PATCH /api/v1/hives/<id>` with the ledger's credential and its slug. The note line shows the write pending, then
done, or the server's error message, such as `role qa needs a UI, and this hive has ui=no`. The row then shows the
hive as the server stored it.

The page converts what was typed before it sends it:

| Field | Typed | Sent |
|---|---|---|
| excluded | `master, qa` | `["master", "qa"]` |
| prefer | `master:1,qa:2` | `{"master": 1, "qa": 2}` |
| max agents | `4` | `4`; anything other than digits is sent as text and refused |

Only the operator can edit. An agent's credential gets
`403 Hive settings need the operator`.

### Move master

The button sends the swarm control `master_move` with the hive id. The server runs
`agentihooks swarm <slug> master move --to <hive>`, so the move behaves as described in
[Headless fallback](#headless-fallback) and in the [swarm commands](swarm.md): the live master hands off and its
successor starts on that hive.

The button is off when:

- the Swarm tab has no swarm status yet, or another swarm control is still pending;
- the hive is quiet;
- this ledger's master already runs on that hive.

The command can still refuse a hive the button allows, such as one placement would not give the master; the
control then answers `502 request_refused` and the note line shows the server's answer.

```bash
curl -X PUT "http://127.0.0.1:8765/api/v1/ledgers/$SLUG/swarm/controls" \
  -H "Content-Type: application/json" -H "X-Ledger-Token: $LEDGER_TOKEN" \
  -d '{"action": "master_move", "hive": "workstation"}'
```

A hive id must start with a letter or digit and hold only letters, digits, dots, dashes or underscores, at most 63
characters; any other answers `400 request_refused` with `A master move needs a hive id`. A control from an agent's
credential answers `403 forbidden` with `swarm controls need the operator`.

### Hive settings API

| Method and path | Who | Answer |
|---|---|---|
| `GET /api/v1/hives` | no ledger credential needed | Every registered hive. |
| `GET /api/v1/hives/<id>` | no ledger credential needed | One hive. |
| `PATCH /api/v1/hives/<id>` | the operator | The hive after the change. |

Every answer is `{"data": ..., "revision": "<sha256 of data>"}`. A hive carries its stored record (`id`, `name`,
`ui`, `ephemeral`, `excluded`, `prefer`, `max_agents`, `harnesses`, `slots`, `interactive`, `repos`,
`heartbeat_at`, `version`) and three computed fields:

- `live`: `true` while the heartbeat is under ninety seconds old.
- `agents`: the placement load described under [Panel](#panel).
- `master`: `{"slug", "name"}` of the live master on the hive, or `null`.

Each slot also gains `agents`, the sessions the hive reports for that harness and account.

```bash
curl -s http://127.0.0.1:8765/api/v1/hives/pod-a
```

```json
{
  "data": {
    "id": "pod-a", "name": "pod-a", "ui": "no", "ephemeral": "yes",
    "roles": ["eng", "ci"], "prefer": {}, "max_agents": 4, "agents": 1,
    "live": true, "master": null,
    "slots": [{"harness": "claude", "account": "work", "kind": "subscription", "cap": 2, "provider": "", "agents": 1}],
    "...": "..."
  },
  "revision": "9f2c..."
}
```

The PATCH body takes one or more of these fields and nothing else:

| Field | Type |
|---|---|
| `name` | string, 1 to 200 characters |
| `ui`, `ephemeral` | `"yes"` or `"no"` |
| `roles` | array of `master`, `qa`, `frontend`, `eng`, `ci`, `plan`, `dispatch` |
| `prefer` | object of role to whole number rank |
| `max_agents` | integer, 1 or more |

The server checks the body against that schema, then applies it with the same rules as `agentihooks hive set`, so a
UI role on a `ui=no` hive, a `prefer` role the hive does not take, or a rank or max agents below 1 is refused. The
credential is the ledger token in `X-Ledger-Token` and the ledger it belongs to in `X-Ledger-Slug`.

```bash
curl -X PATCH http://127.0.0.1:8765/api/v1/hives/pod-a \
  -H "Content-Type: application/json" \
  -H "X-Ledger-Slug: $SLUG" -H "X-Ledger-Token: $LEDGER_TOKEN" \
  -d '{"roles": ["eng", "ci", "plan"], "max_agents": 4}'
```

| Status | Code | When |
|---|---|---|
| 400 | `schema_invalid` | The body has no field, an unknown field or a wrong type (`Request does not match the resource schema`), or the hive registry refuses the value and names why. |
| 403 | `forbidden` | `Missing or wrong ledger credential`: no `X-Ledger-Slug`, an unknown ledger or a wrong token. `Hive settings need the operator`: the credential is an agent's. |
| 404 | `resource_missing` | `No such hive`. A PATCH never creates a hive; register one with `agentihooks hive add`. |
| 405 | `method_not_allowed` | Anything but GET on the list, or GET and PATCH on one hive. |
| 503 | `storage_error` | Redis is unreachable or a hive record is unreadable. |

### Limitations

- Masters do not count in the agents figure, matching max agents.
- Settings the hive reports itself (`harnesses`, `slots`, `interactive`, `repos`, `heartbeat_at`, `version`)
  cannot be changed through the API.

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Could not read hives.` | The server answered an error, usually 503 with Redis down. | `curl` the list to read the error, then restore Redis. |
| An edit shows `Hive settings need the operator` | The page was opened with an agent's credential. | Open the ledger with the operator's credential. |
| An edit shows `prefer names qa, a role this hive does not take` | `prefer` named a role missing from `roles`. | Add the role first, then the rank. |
| move master stays off | The hive is quiet, the master is already there, or a control is pending. | Wait for the hive's heartbeat or the pending control. |
| A hive stays `quiet` | `hive run` is not publishing. | `systemctl --user status agentihooks-hive` on that machine. |

## Troubleshooting

| Symptom | Check | Fix |
|---|---|---|
| A launch waits with "no hive for `<role>` lists the repo `<repo>`" | `agentihooks hive show <id>`, the `repos` list | Clone the repo under `HIVE_REPO_ROOT` with an `origin` remote matching the swarm's repo name, or point `HIVE_REPO_ROOT` at the clones. |
| A launch waits with "no live hive takes" a role | `agentihooks hive list`: `stale` hives, roles | Restart `agentihooks hive run` (`agentihooks hive install` keeps it running), or add the role with `agentihooks hive set`. |
| A master launch on a hive reports "account reservation is full" | `agentihooks hive show <id>`: the slot's `cap` for the account | A `cap` of 0 means the hive has no fresh quota reading for the account; otherwise end a session on that account or wait for its five hour window to recover. |

## Related pages

- [Deployment modes](../reference/deployment-modes.md): where the controller, ledger and Redis run, and the
  regulated-site recipe.
- [Swarm](swarm.md): the master, seats, the minute tick and the swarm commands.
- [Claude account load balancing](load-balancing.md): subscription and api slots.
- [Hosted ledger auth](../reference/hosted-ledger-auth.md): hive tokens and edge mode.
