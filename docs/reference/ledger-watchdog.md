---
title: Ledger watchdog signal records
parent: Reference
nav_order: 72
---

# Ledger watchdog signal records

The ledger watchdog records each signal it sends before attempting to stop a
local ledger server. Use this evidence when a server exits cleanly but its
own output does not explain who requested the exit.

The records are newline-delimited JSON in `.watchdog.log` inside the ledger
folder. They remain on disk across server restarts. This is a local diagnostic
file, separate from the ledger database and the server's request logs.

## Find and read the records

The watchdog resolves the folder from `LEDGER_DIR`, expanding `~`. An unset or
empty value uses `~/development-ledger`. Read the file on the host where the
watchdog ran, with the same folder setting as that process:

```bash
export LEDGER_DIR="$HOME/development-ledger"
tail -n 20 "$LEDGER_DIR/.watchdog.log"
```

For a deployment with a different ledger folder, use that existing folder:

```bash
export LEDGER_DIR="$HOME/ledger-data"
tail -n 20 "$LEDGER_DIR/.watchdog.log"
```

Changing a reader's environment does not change the running watchdog's folder.
Keep the configured ledger folder on persistent storage if its evidence must
survive container replacement. The log name and signal fields have no separate
environment settings.

An example signal record is:

```json
{"at":1792000000.5,"reason":"it ran 151 threads, over the limit of 150","signal":"SIGTERM","pid":12345,"caller":{"pid":23456,"argv":["agentihooks","swarm","tick"]}}
```

| Field | Meaning |
|-------|---------|
| `at` | Unix timestamp in seconds, recorded before the signal attempt |
| `reason` | The watchdog's reason for restarting this server |
| `signal` | Signal name, `SIGTERM` or `SIGKILL` |
| `pid` | Target ledger server process id |
| `caller.pid` | Process id of the watchdog caller writing the record |
| `caller.argv` | That caller's argument vector, as held by `sys.argv` |

The caller identifies a process and its invocation; it does not identify a
human operator. Correlate the timestamp and target process id with the server's
exit and the caller's service logs. A record proves a signal was about to be
attempted, rather than proving that the process received it or exited.

To select signal attempts for one known server process id:

```bash
jq -c 'select(.pid == 12345 and .signal != null)' "$LEDGER_DIR/.watchdog.log"
```

## Restart reasons and signal order

The current watchdog treats a server as runaway when it exceeds 150 threads
or 1,048,576 KiB of resident memory. These limits are constants in the installed
code, rather than environment settings. The reason includes the observed
threads or memory and the limit; a runaway reason takes precedence over stale
code.

For a runaway restart, the watchdog appends a `SIGTERM` record, closes the file
and sends that signal to the server's own process id. It waits up to ten
seconds. If the process remains alive and still identifies as a ledger server,
it appends a second record for `SIGKILL` before sending that signal. Both records
carry the restart reason and caller. The watchdog then runs the server's
launch command with `--ensure` and the same `LEDGER_DIR`.

The current stale-code path uses `--reload` instead of directly signalling the
server. It writes a separate record in the same file: `reload: "completed"`
with `before` and `after` code identities, or `reload: "failed"` with `reason`,
`error` and `pid`. Reload records do not have the signal record's caller fields.
The watchdog skips local restarts for a remote ledger connection; a local file
does not provide attribution for a hosted server's lifecycle.

## Failures and evidence limits

If opening, writing or closing a signal record raises an operating-system
error, the watchdog aborts that restart before sending that signal. A failure
on the second record can therefore follow an already recorded and sent
`SIGTERM`. The normal watcher reports the error through its failed restart
incident:

```text
The ledger server needed a restart because <reason>, and the restart failed: <error>.
```

| Observation | Check |
|-------------|-------|
| Restart failed with a file error | Check available space and the watchdog user's write access to the ledger folder and `.watchdog.log`; ensure the log path is a file. Restore logging before expecting the restart to proceed. |
| `SIGTERM` followed by `SIGKILL` | The process survived the stop wait and still identified as a ledger server. Compare both records with its shutdown logs. |
| Signal record but no matching exit | The record precedes the attempt. The target may have disappeared already or the operating system may have refused the signal. Inspect the failed restart incident and caller logs. |
| Clean exit without a signal record | Check the correct host and ledger folder, then investigate other lifecycle callers. This contract covers signals sent directly by the watchdog, not every command, health check or external signal. |
| A `reload` record lacks `caller` | It describes the separate reload path. Filter on `signal` when inspecting watchdog signal attempts. |

The append is closed before signalling, but the implementation does not call
`fsync` or rotate the file. Retention and protection against storage loss belong
to the host's log and storage policy.

## Delivery evidence

[Pull request 4375](https://github.com/the-cloud-clockwork/agentihooks/pull/4375)
merged the signal records into `dev`. Its tests verify that each record exists
before the corresponding signal, that termination and forced termination name
the reason, target and caller, and that an unwritable log prevents signalling.
The delivery records a failing tests-only
[proof run](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39091440883)
and a passing
[mutation preflight](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39091642390).
The reload behavior above describes the current implementation.
