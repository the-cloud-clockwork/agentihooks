---
title: Profile launch home
parent: Reference
nav_order: 18
---

# Profile launch home

`agentihooks init-agent --profile NAME` renders the profile once, records the
rendered home in the launch report, and starts the session on exactly that
home. The launcher it writes runs `agentihooks select-profile NAME
--home=<home>`, which checks the recorded home and mounts it without rendering
again. A change to the editable package between prepare and launch, such as a
pull of `dev` into the primary checkout, or a launcher running from another
checkout than the one that prepared the launch, no longer moves the session to
a different home, so the binding canary validates the home the launch recorded.

## Launch sequence

1. **Prepare.** `init-agent` renders the profile for the chosen harness, with
   its `--overlay` and `--bundle-revision` choices, into
   `~/.agentihooks/profiles/.homes/<profile>/<key>/<harness>` and sets
   `CLAUDE_CONFIG_DIR` (Claude) or `CODEX_HOME` (Codex) to it. It always
   renders here: an `AGENTIHOOKS_RECORDED_PROFILE_HOME` inherited from its
   caller is dropped first.
2. **Record.** It writes a pending launch report
   (`AGENTIHOOKS_PROFILE_REPORT`) holding the profile, the harness and the
   rendered binding of that home: home path, persona digest, source manifest
   digest and canary.
3. **Launch.** The launcher script runs, after the terminal or herdr pane
   opens:

   ```bash
   agentihooks select-profile NAME \
     [--overlay=<overlay> ...] [--bundle-revision=<commit>] \
     --home=<recorded home> --agent claude|codex -- <harness arguments>
   ```

   Fresh launches, `--resume` and `--handoff` all take this command.
4. **Mount.** `select-profile` sees `--home` and skips the render. It inspects
   the home, then exports it as `CLAUDE_CONFIG_DIR` or `CODEX_HOME` and starts
   `agentihooks claude` or `agentihooks codex`.
5. **Validate.** The session's opening prompt asks it to run the binding canary
   command from its mounted instructions. That writes `validated` to the
   launch report, and `init-agent` prints `profile_validation=validated` and
   `profile_binding=<json>`, whose `home` is the recorded home. The wait for
   that report has its own deadline per harness; see
   [Canary deadline](#canary-deadline).

## Canary deadline

Once the new session's route report arrives, `init-agent` waits for the
launched agent itself to run the canary command. That wait ends only when the
report reads `validated` or `failed`, or when the launch deadline runs out. By
default the canary wait gets the rest of the launch deadline, never
`--route-timeout`.

The wait counts from the route report, so it also covers the harness's own
start before it reads the opening prompt; a slow session start uses launch
time, never a separate fixed canary allowance.

Pass `--canary-timeout <seconds>` to cap the wait for one launch:

```bash
agentihooks init-agent --agent codex --profile engineer --dir "$PWD" \
  --prompt "Run the binding canary, then start." --canary-timeout 150
```

### Launch deadline

All waits of one launch share a 270 second deadline counted from the start of
`init-agent`, so a swarm spawn returns inside the runtime's 300 second spawn
timeout. The canary wait is granted what remains of the 270 seconds, or
`--canary-timeout` when that is less. A swarm Codex launch whose two ready
waits each run their full 120 seconds leaves the canary 30 seconds.

`--launch-until <epoch milliseconds>` shortens the deadline so the launch
returns by that wall clock time. A hive passes its launch order's expiry less
30 seconds (`ACK_MARGIN_MS`), counted on its own clock, so a launch that started
late in the order's 300 second lifetime still acknowledges before the order
expires instead of being stopped as `expired`. The margin holds the herdr
commands that run outside the deadline and the hive's own acknowledgement. An
order with no more than the margin left fails `expired` before any pane opens.
The route report wait also ends with the launch deadline.

### Timeout output

A canary wait that runs out prints `profile_validation=failed` with the rest of
the launch report on stdout, the reason on stderr, and exits 3. A canary that
fails or times out before the launch deadline runs out keeps the plain reason:

```text
agentihooks init-agent: mounted profile canary did not validate before timeout: the codex agent reported no canary within 90s
```

A wait that ran to the launch deadline first names the deadline, the wait it
ran out in and the seconds each wait spent:

```text
agentihooks init-agent: the 270s launch deadline ran out in the profile canary wait after Codex ready 120s, Codex ready for the opening prompt 120s, profile canary 30s: the codex agent reported no canary within 30s
```

A swarm spawn that fails this way reopens its task and writes the same reason
to the swarm timer journal. Count canary timeouts for one UTC day with:

```bash
TZ=UTC journalctl --user -u agentihooks-swarm.service \
  --since "2026-10-23 00:00" --until "2026-10-23 23:59:59" --no-pager -o cat \
  | grep -c "canary did not validate before timeout"
```

Before the deadline merged on 20 October, the journal held 8, 33, 2, 1 and 1
such failures a day from 13 to 17 October, as measured when the change was
made. From 18 to 22 October it held none, and on 23 October one, a Codex
launch that reported no canary within 90 seconds. The journal keeps only a few
days, so read older days from the swarm store history instead.

### Tests

`tests/test_canary_wait.py` pins the default wait to the rest of the launch
deadline, a canary reported at 150 seconds passing, `--launch-until`, the
`--canary-timeout` override, the flags' help text and the timeout message.
`tests/test_launch_deadline.py` pins the shared launch deadline and its message.
`tests/hive/test_orders.py` pins queued orders whose launches run to their
deadline all acknowledging live inside the order lifetime.

## Deferred master canary

A Claude swarm master launched by the tick returns to the tick before its new
session has routed or run its canary, and a later tick pass confirms the
canary. A master recycle therefore no longer holds every other swarm action
(spawns, merges, nudges, alerts) for the minutes a new session takes to start,
log in and validate.

On 25 October, before this change, the tick master step ran 318 seconds in one
pass and 344 seconds in another, almost all of it inside the herdr runtime
launch, which returned within seconds of the new master recording its canary.
The stale tick watcher had to restart the scheduler.

### Which launches defer

| Launch | Defers the canary |
|---|---|
| A Claude master launched by the host tick, the acknowledgement pass or the deployed controller tick | Yes: the runtime adds `--defer-canary` to `init-agent` |
| A Codex master | No: it waits for its canary as before |
| Engineer, CI and every other lane | No: the launch path is unchanged |
| A master launched outside the tick | No |
| A hand run `agentihooks init-agent` | Only with `--defer-canary` and `--profile` |

`--defer-canary` without `--profile` writes no launch report, so the launch
waits as usual.

### What a deferred launch does

1. `init-agent` writes the pending launch report as in
   [Launch sequence](#launch-sequence), opens the pane and waits for the
   launcher to start.
2. Once the launcher has started, the process forks. The parent prints the
   launch report and exits 0, ending with:

   ```text
   status=started
   launcher_at=<epoch milliseconds>
   route_status=pending
   profile_validation=pending
   profile_report=<path of the pending launch report>
   ```

3. The detached child, with its output sent to `/dev/null`, does the rest of a
   normal launch: it waits on the route, names the pane, answers the channel
   warning and waits for the canary. A route or canary failure is written into
   the launch report as `failed` with its reason, the only way the child
   reaches the tick.
4. The runtime accepts `route_status=pending` only together with
   `profile_validation=pending` and a `profile_report`. A pending validation
   with no report fails the launch with `pending profile validation names no
   request`. The agent is stored with a pending validation naming the report,
   profile, harness, account and the launch's timeout as its deadline.

A master on the master Claude login keeps the full login budget for the
child's waits: its `init-agent` call gets a 450 second route timeout and a 660
second launch timeout instead of the 300 second default, covering the five
minute browser login, the start and the canary.

### How a later pass confirms it

The `launch checks` tick step reads the launch report of each agent whose
validation is pending:

| Report | Outcome |
|---|---|
| `validated`, with the launch's profile, harness and account | The agent record takes the validation, the account and the validation time as its harness start, then the launch check judges the agent as for any launch |
| `pending` | The launch check holds its verdict until the deadline below |
| `failed` | Failed launch check on `canary`: `expected validated; observed <reason>` |
| Validated on another account | Failed launch check on `canary`: `observed live process account differs from requested route` |

A pending canary is judged once the time since the session started passes the
larger of the harness's canary deadline (120 seconds for Claude) and the
deadline the launch recorded: 660 seconds for a master on the master login,
300 seconds otherwise. A canary still pending then is a failed launch check,
`expected validated; observed pending`. A failed check takes the normal failed
launch path and appears in `agentihooks swarm <slug> status` as a launch check
finding, `<master name> failed the launch check on canary`.

### Tick thread switch interval

Every swarm on a host ticks in its own thread of one tick process. At Python's
default switch interval of 5 milliseconds, a swarm doing CPU heavy work kept
the other threads, the master launch among them, waiting on their Redis and
process round trips. That is why the master recycle on 25 October still took
344 seconds after the deferral merged. The host tick sets the interval to 0.5
milliseconds (`TICK_SWITCH_INTERVAL_S` in `scripts/swarm/cli.py`) before any
swarm ticks. The value is fixed; no setting changes it.

### Checking a recycle

`tick-report` lists the `tick master` step with its median and p95 wall time
over recent passes; see [Tick report](tick-report.md):

```bash
agentihooks swarm <slug> tick-report --passes 60
```

After a master recycle, the `tick master` row stays in seconds, and a later
pass reports `<master name> passed its launch check in <n> seconds` once the new
master's canary validates.

### Tests

| Test file | Pins |
|---|---|
| `tests/test_canary_wait.py` | `--defer-canary` prints the pending request without waiting, its help text, a launch without a profile request still waiting, and `confirm` reading pending, failed, validated and another account |
| `tests/swarm/test_master_affinity.py` | A tick master launch returning before a slow canary and before its route, engineer launches and masters outside the tick still waiting, and the scheduled tick building a deferring runtime |
| `tests/swarm/test_launch_check.py` | A later pass confirming, holding or failing a pending canary, and its deadline |
| `tests/swarm/test_tick_switch_interval.py` | The host tick setting the switch interval before any swarm ticks |

## What the home check requires

`select-profile --home` passes the home to the same inspection the launch
report used. The home is refused unless all of these hold:

| Check | Refusal |
|---|---|
| The directory exists | `missing profile home: <home>` |
| `.profile-binding.json` in the home is readable JSON | `missing or invalid rendered profile evidence: <error>` |
| Its `profile`, `harness` and resolved `home` match the request | `profile mismatch in rendered binding` |
| The persona file (`CLAUDE.md` or `AGENTS.md`) still has the rendered digest | `mounted persona changed since render` |
| `<harness>.sources.json` beside the home still has the rendered digest | `rendered source manifest changed` |

A swarm Codex launch (`AGENTIHOOKS_SWARM` set) is also refused when the home
resolves to the operator's own Codex home:
`swarm codex profile resolves to the operator Codex home`.

On a refusal `select-profile` prints `agentihooks select-profile: <reason>` to
stderr, exits 2, and, when `AGENTIHOOKS_PROFILE_REPORT` is set, marks the
pending report `failed` with the reason `profile selection failed: <reason>`.
`init-agent` then reports the launch failed rather than waiting for the route
timeout.

## Using `select-profile` directly

Without `--home`, `select-profile` renders the profile itself and mounts the
current home for that profile and overlays, as before. Pass `--home` to pin a
run to a home rendered earlier. `--dry-run` prints the environment and the
harness command without starting it:

```text
$ agentihooks select-profile documentation \
    --home=$HOME/.agentihooks/profiles/.homes/documentation/<key>/claude \
    --agent claude --dry-run -- --model opus
AGENTIHOOKS_PROFILE=documentation
AGENTIHOOKS_BASE_CHANNELS=brain,amygdala
AGENTIHOOKS_OVERLAYS=
AGENTIHOOKS_BUNDLE_REVISION=
CLAUDE_CONFIG_DIR=/home/<user>/.agentihooks/profiles/.homes/documentation/<key>/claude
argv=agentihooks claude --model opus --effort medium
```

The same home given with another profile name is refused:

```text
$ agentihooks select-profile engineer --home=<documentation home> --agent claude --dry-run
agentihooks select-profile: profile mismatch in rendered binding
$ echo $?
2
```

`--home` maps to `AGENTIHOOKS_RECORDED_PROFILE_HOME` inside the selector. The
variable is internal: neither `init-agent` nor `select-profile` reads it from
the caller's environment, so only an explicit `--home` skips the render.

## Limits

- The Codex app-server transport (`--codex-transport app_server`) launches
  `scripts.codex_session serve`, which still renders inside its server
  process; it does not take the recorded home.
- A home whose persona or source manifest is rewritten after the launch was
  recorded is refused, not re-rendered. Launch again to render a fresh home.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `profile mismatch in rendered binding` | The recorded home belongs to another profile or harness, or was moved. Launch again with the intended `--profile` and `--agent`. |
| `mounted persona changed since render` or `rendered source manifest changed` | Files in the home changed after `init-agent` recorded it. Launch again to render and record a fresh home. |
| `missing profile home` | The home was removed between prepare and launch. Launch again. |
| `mounted profile canary did not validate before timeout: the <harness> agent reported no canary within <N>s` | The session started but did not run the canary command within its [canary deadline](#canary-deadline). Check the session pane for a stalled first turn or a refused command; the home it mounted is in its `CLAUDE_CONFIG_DIR` or `CODEX_HOME`. When the agent is slow but healthy, relaunch with a larger `--canary-timeout`. |
| `the 270s launch deadline ran out in the profile canary wait after ...` | Earlier waits used most of the launch deadline, so the canary wait got less than its harness default. The seconds each wait spent are listed; investigate the longest one first. |

See [`agentihooks init-agent`](cli-commands.md#agentihooks-init-agent) and
[`agentihooks select-profile`](cli-commands.md#agentihooks-select-profile) for
the full flag lists.
