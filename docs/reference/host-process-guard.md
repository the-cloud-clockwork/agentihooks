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
| `agentihooks swarm tick` and `scripts.swarm.scheduled_tick` | The swarm tick | #4962 |
| `agentihooks select-profile` | Every agent session wrapper, masters included | #5006 |
| `agentihooks swarm tick-watch`, `prober`, `waker` | The tick watcher, provider prober and waker | #5107 |

Any other `agentihooks` command, such as an old `ledger watch`, can still be
ended once its group stays over the threshold.

`claude` is in `excludes`, but that does not protect an agent session. The
guard counts the wrapper under the name `agentihooks`. Before #5006 a
wrapper ended with status 143 (`SIGTERM`), the `claude` it launched kept
running without its wrapper, and that session ended some time later.

## Install and drift check

```bash
agentihooks host-guard install   # link both files from this checkout and schedule the guard
agentihooks host-guard check     # print each drift, exit 1 when any is found
```

`agentihooks init` runs the same reconcile. When both links resolve to the
repository and the schedule is in place, it reports
`[OK] Host guard in sync with <repo>`. A host that never installed the guard
gets `[--] Host guard not installed`. `check` reports these drifts:

- `~/guard-daemon.sh is missing` or `... is not a link to <repo>/...`, and the
  same for `~/guard-config.json`
- `... still runs the guard on systemd instead of the agentihooks scheduler`
  for a leftover `guard-daemon.timer` or `guard-daemon.service`
- `~/guard-daemon.sh is not scheduled every 60s on the agentihooks scheduler`
- `the agentihooks scheduler is unreachable, so the guard schedule was not checked`

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
   running. Run `agentihooks host-guard check`.

## Evidence for #5006

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
  `KILLED`. Without the pattern the wrapper probe was ended (Proofs run
  39349226304).
- The host log counted 30 to 66 `agentihooks` kills a day from 13 to 20
  October. The last was at 00:06 UTC on 20 October. #5006 merged at 03:40 UTC.
  The log holds no `agentihooks` kill after it: later flags reached
  54 minutes before the group fell below the threshold.
