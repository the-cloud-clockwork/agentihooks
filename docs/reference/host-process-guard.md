---
title: Host process guard
parent: Reference
nav_order: 5
---

# Host process guard

The host process guard ends processes that pile up on the workstation. It
groups the account's own processes by process name. If one name has at least
`threshold` live processes for `timeout` or longer, the guard ends every
process with that name. Exceptions are names in `excludes` and processes whose
command line matches an `excludeCommands` pattern.

Every swarm agent runs inside an `agentihooks select-profile` wrapper, and that
wrapper's process name is `agentihooks`. The guard never ends a wrapper, so a
busy workstation keeps its agent sessions. Other long lived `agentihooks`
commands are still ended when their group stays over the threshold.

## What the guard does each pass

The agentihooks scheduler starts `~/guard-daemon.sh` every 60 seconds under
the owner `agentihooks-host-guard`, inside `timeout 120` when `timeout` is
installed. One pass:

1. Reads `~/guard-config.json`. If the file is missing, is not valid JSON, or
   holds an `excludeCommands` entry that is not a valid pattern, the guard
   logs an `ERROR` line and exits without ending anything.
2. Counts the account's processes by name (`ps -o comm`).
3. Skips every name below `threshold` and every name in `excludes`. The
   `excludes` match ignores case.
4. Flags a name the first time it reaches the threshold and writes the time to
   `~/guard-daemon-state.json`.
5. Once a flagged name has stayed at or over the threshold for `timeout`,
   checks each process with that exact name. A process whose command line
   matches an `excludeCommands` pattern is logged `SKIPPED` and left running.
   Every other process gets `SIGTERM`, then `SIGKILL` half a second later, and
   is logged `KILLED`.

A name that drops below the threshold is dropped from the state file. Its one
hour clock starts again the next time it reaches the threshold.

## Configuration

`~/guard-config.json` is a symlink to the repository's
`scripts/host_guard/guard-config.json`. Change the config through a pull
request into `dev`. The live guard reads the new file once the primary checkout
pulls the merge.

| Key | Default | Meaning |
|-----|---------|---------|
| `threshold` | `30` | Live processes with one name that flag the name |
| `timeout` | `1h` | How long a flagged name stays over the threshold before its processes are ended: `<n>h`, `<n>m`, `<n>s` or seconds |
| `excludes` | shells, `node`, `python3`, `docker`, `claude`, `systemd` and others | Process names the guard never counts |
| `excludeCommands` | see below | Bash extended regular expressions matched against a process's command line |
| `logMaxSizeMB` | `10` | Size at which `~/guard-daemon.log` moves to `~/guard-daemon.log.1` |
| `stateFile` | `~/guard-daemon-state.json` | Where flag times are kept |

The guard joins a process's `/proc/<pid>/cmdline` arguments with single spaces
before matching it. The shipped `excludeCommands`:

```json
"excludeCommands": [
  "(^|[ /])agentihooks swarm tick( |$)",
  " -m scripts[.]swarm[.]scheduled_tick( |$)",
  "(^|[ /])agentihooks select-profile( |$)",
  "(^|[ /])agentihooks swarm (tick-watch|prober|waker)( |$)"
]
```

| Pattern | Keeps alive | Added by |
|---------|-------------|----------|
| `agentihooks swarm tick` and `scripts.swarm.scheduled_tick` | The swarm tick | [#4962](https://github.com/the-cloud-clockwork/agentihooks/pull/4962) |
| `agentihooks select-profile` | Every agent session wrapper, masters included | [#5006](https://github.com/the-cloud-clockwork/agentihooks/pull/5006) |
| `agentihooks swarm tick-watch`, `prober`, `waker` | The tick watcher, provider prober and waker | [#5107](https://github.com/the-cloud-clockwork/agentihooks/pull/5107) |

Any other `agentihooks` command, such as an old `ledger watch`, can still be
ended once its group stays over the threshold.

`claude` is in `excludes`, but that does not protect an agent session. The
guard counts the wrapper under the name `agentihooks`. Before [#5006](https://github.com/the-cloud-clockwork/agentihooks/pull/5006) a
wrapper ended with status 143 (`SIGTERM`), the `claude` it launched kept
running without its wrapper, and that session ended some time later.

## Install and drift check

The guard runs from two files in the account's home folder, and both are links
into the agentihooks checkout:

| Home file | Links to |
|-----------|----------|
| `~/guard-daemon.sh` | `scripts/host_guard/guard-daemon.sh` |
| `~/guard-config.json` | `scripts/host_guard/guard-config.json` |

A merged guard change reaches the running guard when that checkout pulls the
merge. No file is copied by hand. Before [#5143](https://github.com/the-cloud-clockwork/agentihooks/pull/5143)
both files were hand copies run by a hand written systemd user timer, so the
merged [#5006](https://github.com/the-cloud-clockwork/agentihooks/pull/5006)
exclusion applied only after a manual copy at 03:43 UTC on 20 October.

```bash
agentihooks host-guard install   # link both files from this checkout and schedule the guard
agentihooks host-guard check     # print each drift, exit 1 when any is found
```

Run both from the checkout the guard should follow. The links point at the
checkout of the `agentihooks` command that ran `install`.

### What install does

1. Replaces `~/guard-daemon.sh` and `~/guard-config.json` with links to the
   repository copies. Each link is written beside the file and renamed over
   it, so a hand copy or a link to another checkout is replaced in one step.
2. Removes the old `guard-daemon.timer` and `guard-daemon.service` from
   `~/.config/systemd/user`. In the account's own home it runs
   `systemctl --user disable --now guard-daemon.timer` before removing them
   and `systemctl --user daemon-reload` after.
3. Cancels a live guard entry for the same home left on the local Redis at
   `redis://127.0.0.1:6379/0`. This runs only when the swarm Redis the
   scheduler uses is another endpoint, so the guard never runs from two
   schedulers. An unreachable local Redis does not stop the install.
4. Adds an interval entry for the owner `agentihooks-host-guard` that runs
   every 60 seconds. Its payload names the home script. The new entry starts
   before this home's older guard entries are cancelled, so a failed install
   never stops a running guard. Entries for other homes are left alone.
5. Starts the scheduler runner when it is not running.

Each scheduler delivery runs the home script once, detached and inside
`timeout 120` when `timeout` is installed. A delivery that arrives twice runs
the script once.

| `install` prints | Exit |
|------------------|------|
| `[OK] Host guard installed in <home> from <repo>, every 60s on the agentihooks scheduler` | 0 |
| The same line ending `; a guard entry on the local Redis could not be checked or retired` | 0 |
| `[!!] Host guard linked in <home>; Redis is unreachable, so the guard is not scheduled` | 1 |

When Redis is unreachable the links are still written. Run `install` again once
Redis answers.

### What check reports

`check` changes nothing. It prints `[OK] Host guard in sync with <repo>` and
exits 0, or prints one line per drift and exits 1:

| Drift line | Meaning | Fix |
|------------|---------|-----|
| `<home>/guard-daemon.sh is missing` | No script, and no link | `agentihooks host-guard install` |
| `<home>/guard-config.json is not a link to <repo>/guard-config.json` | A hand copy, or a link to another checkout. A copy with the same text still counts | `agentihooks host-guard install` |
| `<home>/.config/systemd/user/guard-daemon.timer still runs the guard on systemd instead of the agentihooks scheduler` | An old unit is left, `.timer` or `.service` | `agentihooks host-guard install` removes it |
| `<home>/guard-daemon.sh is not scheduled every 60s on the agentihooks scheduler` | No live entry for this home at 60 seconds that calls the guard | `agentihooks host-guard install` |
| `the agentihooks scheduler is unreachable, so the guard schedule was not checked` | The swarm Redis did not answer | Restore Redis, then run `check` again |

A live entry is one in the `ready`, `active` or `pending` state.

### What agentihooks init does

`agentihooks init` reconciles the guard in the step that schedules the hourly
lifecycle sweep, and prints one line after the sweep line. It changes only a
guard that is already installed:

| Home state | `init` prints | Change |
|------------|---------------|--------|
| No guard file, no link and no old unit | `[--] Host guard not installed; agentihooks host-guard install adds it` | None |
| No drift | `[OK] Host guard in sync with <repo>` | Starts the scheduler runner when it is not running |
| Drift, and the scheduler is unreachable | `[!!] Host guard not reconciled: the agentihooks scheduler is unreachable` | None |
| Drift, and both home files are links, the home has a live guard entry, or an enabled old systemd timer was removed | The `install` lines above | Relinks and schedules |
| Drift otherwise, for example hand copies with no schedule and no enabled timer | `[--] Host guard linked in <home> from <repo>; agentihooks host-guard install schedules it` | Relinks only |

`init` never schedules a guard the operator turned off. A disabled systemd
timer, or hand copies with no entry, end up linked but unscheduled until
`agentihooks host-guard install` runs.

### Example

On this workstation on 23 October both links were in place, the guard was not
scheduled and the last guard log line was from 21 October:

```console
$ agentihooks host-guard check
/home/iamroot/guard-daemon.sh is not scheduled every 60s on the agentihooks scheduler
$ echo $?
1
$ ls -l ~/guard-daemon.sh ~/guard-config.json
lrwxrwxrwx ... /home/iamroot/guard-config.json -> /home/iamroot/dev/tcc-ecosystem/agentihooks/scripts/host_guard/guard-config.json
lrwxrwxrwx ... /home/iamroot/guard-daemon.sh -> /home/iamroot/dev/tcc-ecosystem/agentihooks/scripts/host_guard/guard-daemon.sh
$ tail -1 ~/guard-daemon.log
2026-10-21 08:45:15 TICK_COMPLETE flagged=0 kills=0 errors=0
```

`agentihooks host-guard install` schedules it again. A later `check` prints
`[OK] Host guard in sync with <repo>`, and a `TICK_COMPLETE` line reaches the
log on each 60 second pass.

### After the swarm Redis moves

The guard entry lives on the Redis the scheduler used when the guard was
installed. That Redis is the first of these that is set:

1. `AGENTIHOOKS_HIVE_REDIS_URL`, outside ledger local mode
2. `AGENTIHOOKS_SWARM_REDIS_URL`
3. `redis://127.0.0.1:6379/0`

The scheduler runner is started only for the current endpoint. When the swarm
Redis moves, for example from the local Redis to a remote swarm Redis, the old
entry stays `active` on the local Redis and nothing delivers it. The guard
stops, and `check` reports the home as not scheduled even though both links
are in place.

Symptoms:

- `agentihooks host-guard check` prints
  `<home>/guard-daemon.sh is not scheduled every 60s on the agentihooks scheduler`
  and exits 1.
- `ls -l ~/guard-daemon.sh ~/guard-config.json` shows both links.
- No new `TICK_COMPLETE` line reaches `~/guard-daemon.log`.

Count the minutes since the last guard pass. The log times are local time:

```bash
last=$(grep TICK_COMPLETE ~/guard-daemon.log | tail -1 | cut -c1-19)
echo $(( ($(date +%s) - $(date -d "$last" +%s)) / 60 ))
```

On a scheduled guard this prints `0` or `1`. It stood at 3122 when the stranded
entry was reported.

Fix it with either command, from the checkout the guard should follow:

```bash
agentihooks init                 # reconcile schedules a home whose two files are links
agentihooks host-guard install   # link and schedule, whatever the home holds
```

`agentihooks init` refuses to run in a rendered profile home or with
`AGENTIHOOKS_SWARM` or `AGENTIHOOKS_PROFILE` set, so an agent session cannot
run it. The operator runs it once from a plain shell. Both commands then:

1. Cancel every live guard entry on `redis://127.0.0.1:6379/0` whose payload
   names this home's `guard-daemon.sh`. Entries for other homes stay.
2. Add the 60 second entry on the current scheduler and cancel this home's
   older entries there.
3. Start the scheduler runner when it is not running.

The local Redis is checked only when the current endpoint is another URL. When
the swarm Redis is the local Redis, the new entry is the only one for the home
and nothing is cancelled. A guard already in sync is not scheduled again, and
its reconcile does not read the local Redis.

When the local Redis does not answer, the guard is still
scheduled on the current endpoint and the `[OK]` line ends with
`; a guard entry on the local Redis could not be checked or retired`. Run
`agentihooks host-guard install` again once the local Redis answers, so an old
entry there cannot run the guard a second time if that Redis gets a runner
again.

A home with only one of the two links, or with hand copies and no live entry,
stays unscheduled under `init`, as the table above shows. `agentihooks
host-guard install` schedules it.

Confirm the fix:

```console
$ agentihooks host-guard check
[OK] Host guard in sync with <repo>
$ tail -1 ~/guard-daemon.log
<time within the last minute> TICK_COMPLETE flagged=0 kills=0 errors=0
```

### Evidence

- [#5143](https://github.com/the-cloud-clockwork/agentihooks/pull/5143): red
  [run 39377373912](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39377373912),
  where a hand copied config passed the drift check; green
  [run 39384284773](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39384284773).
  `tests/test_host_guard_sync.py` covers the links, the old unit removal, the
  schedule, each drift line and the `init` reconcile.
- [#6311](https://github.com/the-cloud-clockwork/agentihooks/pull/6311) added
  step 3 and the scheduling by `init` when both files are links. After the
  swarm Redis moved to a remote endpoint, the guard entry stayed live on the
  local Redis and `init` relinked the home without scheduling it.

## Log lines

`~/guard-daemon.log` gets one line per event:

```text
2026-10-20 03:41:30 FLAGGED name=agentihooks count=30 first_flagged=now
2026-10-20 03:42:31 FLAGGED name=agentihooks count=30 first_flagged=1m_ago
2026-10-19 09:11:27 KILLED pid=<pid> name=agentihooks reason=ghost_detected count=31 elapsed=1.0h
<time> SKIPPED pid=<pid> name=agentihooks reason=excluded_command
<time> ERROR config_malformed excludeCommands pattern=<pattern>
<time> TICK_COMPLETE flagged=1 kills=0 errors=0
```

Read what a dry pass would end without ending anything. The dry pass still
writes flag times to the state file, as a scheduled pass does:

```bash
~/guard-daemon.sh --dry-run
grep -E 'DRY_RUN|SKIPPED' ~/guard-daemon.log | tail
```

Check which live processes a pattern matches before adding it:

```bash
pattern='(^|[ /])agentihooks select-profile( |$)'
for pid in $(pgrep -u "$(whoami)" -x agentihooks); do
  args=$(tr '\0' ' ' < "/proc/$pid/cmdline")
  [[ "$args" =~ $pattern ]] && echo "match $pid" || echo "other $pid"
done
```

## Troubleshooting several agent sessions ending together

1. Count guard kills of `agentihooks` by day:

   ```bash
   grep 'KILLED.*name=agentihooks' ~/guard-daemon.log ~/guard-daemon.log.1 | awk '{print $1}' | sort | uniq -c
   ```

2. Compare the kill times with the session exit records in
   `~/.agentihooks/session-exits/<launcher>.json`. A wrapper ended by the guard
   leaves `exit_status` `143` with an `ended_at` a few seconds after the
   `KILLED` line. The `claude` it launched can end hours later. That later
   loss can look like a memory kill under full swap.
3. A `KILLED` line for a command that should stay alive needs an
   `excludeCommands` pattern. Add it in a pull request with a test.
4. `TICK_COMPLETE` lines that stop appearing mean the guard is no longer
   running. Run `agentihooks host-guard check` and apply the fix for each
   drift line it prints. A home that has both links but no schedule usually
   follows a swarm Redis move; see [After the swarm Redis moves](#after-the-swarm-redis-moves).

## Evidence for pull request 5006

- On 19 October the guard logged `KILLED ... name=agentihooks reason=ghost_detected count=31`
  at 09:11 UTC. The session exit records of six watched launches carry
  status 143 from 09:11:27 to 09:11:42 UTC. Those six sessions ended at 12:09
  to 12:10 UTC while swap stood near full.
- A read only check on the host matched the new pattern against all 20
  `select-profile` wrappers among 25 `agentihooks` processes, and against none
  of the other 5.
- `tests/test_host_guard.py::test_guard_skips_a_long_lived_tick_and_agent_wrapper_and_still_reaps_the_other_old_processes`
  runs the real guard script against long lived probes. The tick and wrapper
  probes must survive and be logged `SKIPPED`, and the other probes must be
  `KILLED`. Without the pattern the wrapper probe was ended ([Proofs run
  39349226304](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39349226304)).
- The host log counted 30 to 66 `agentihooks` kills a day from 13 to 20
  October. The last was at 00:06 UTC on 20 October. [#5006](https://github.com/the-cloud-clockwork/agentihooks/pull/5006) merged at 03:40 UTC.
  The log holds no `agentihooks` kill after it: later flags reached
  54 minutes before the group fell below the threshold.
