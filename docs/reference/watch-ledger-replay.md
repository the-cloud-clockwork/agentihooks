---
title: Ledger watcher replay verification
parent: Reference
nav_order: 24
---

# Ledger watcher replay verification

The live replay regression verifies that the ledger watcher reconnects after a
stream drop, replays missed changes and closes its stream generators even when
heartbeat frames arrive between changes. This reference is for contributors
investigating watcher test failures and operators reading their CI evidence.

[PR #3872](https://github.com/the-cloud-clockwork/agentihooks/pull/3872) corrects
an assertion in `test_live_replay_finishes_with_forced_heartbeats` and adds a
controlled server-heartbeat condition. It changes test coverage and expectations;
watcher behavior, production heartbeat timing and CLI configuration are unchanged.

## Events and heartbeats

The watcher consumes server-sent events from the ledger server. A `snapshot`
establishes state; a `ledger` event applies a patch. A `heartbeat` keeps the
connection active without changing ledger state or printing an operator change.
The server can emit one whenever its event wait returns no changes.

The regression previously compared every received event name with exactly
`["snapshot", "ledger", "ledger", "ledger"]`. A legitimate server heartbeat
between those events made that assertion fail. The corrected comparison excludes
only `heartbeat`, so an extra or missing snapshot or ledger event still fails.
Other unexpected event names also fail the comparison.

## Use the watcher

For an existing ledger, follow operator changes with the normal CLI:

```bash
agentihooks ledger watch my-ledger \
  --as my-agent
```

Restart from the last printed revision when earlier changes must be replayed:

```bash
agentihooks ledger watch my-ledger \
  --as my-agent \
  --since-rev 12 \
  --interval 3
```

`--as` applies the existing owner filter. `--since-rev` selects the revision after
which changes are printed. `--interval` sets the delay before reconnecting after
a dropped stream; its default is three seconds. It does not set heartbeat timing.
The live connection reconnects with its opaque cursor. An expired cursor causes
a fresh snapshot. See [Presentation layer architecture](presentation-layer.md)
for stream retention and cursor expiry.

## Regression configuration and assertions

These are test parameters and internal constants, not new environment variables
or operator settings:

| Input | Values | Purpose |
|-------|--------|---------|
| `heartbeats` | `1`, `3` | Inject heartbeat frames after the snapshot and before reading the reconnect stream. |
| `server_beat` | `False`, `True` | Keep the ordinary server wait as a control, or force its first wait at each sequence to return no events and emit a real server heartbeat. |
| Live fixture `HEARTBEAT_S` | `0.2` seconds | Let the isolated HTTP server emit idle heartbeat frames during the test. |
| Production `HEARTBEAT_S` | `5.0` seconds | Default server heartbeat wait. The watcher socket timeout is three times this constant. |

The two parameters produce four cases: `[1-False]`, `[1-True]`, `[3-False]` and
`[3-True]`. Forced server heartbeats reproduce the extra frame deterministically;
the test does not depend on a loaded runner producing it by chance.

Every case retains the inherited live replay checks: exactly two stream calls,
an initial call without a cursor, a reconnect with a cursor, one snapshot and
the exact two operator note lines. The outer regression also requires the full
snapshot/ledger sequence after heartbeat filtering, a cursor on the final
received frame and closed states for every retained wrapper and source
generator. The forced-heartbeat wrapper fails explicitly if the watcher does
not reconnect after the first ledger event. Cleanup closes retained generators
even when an assertion fails.

## Verify in CI

Run the focused proof on a pushed branch. Tests run in CI; do not run this test
on the shared swarm host.

```bash
gh workflow run proofs.yml \
  --ref my-pushed-branch \
  -f proof=tests \
  -f tests=tests/swarm_ledger/test_watch_ledger_replay.py
```

Read the selected unit job for all four parameterized cases. A green job alone
does not establish a first-attempt pass: check each case's outcome and the retry
summary too.

The delivered correction has the following retained evidence:

- [Red Tests run](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39000078910),
  unit job `117301111120`: both `server_beat=True` cases fail the received-name
  assertion, including after retries. The unchanged server-wait controls isolate
  the forced heartbeat condition.
- [Green Tests run](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39000290092),
  unit job `117301760577`: all four cases pass, with no case reruns.
- [Issue #3871](https://github.com/the-cloud-clockwork/agentihooks/issues/3871)
  records the original retry-only failures and the controlled reproduction;
  [PR #3872](https://github.com/the-cloud-clockwork/agentihooks/pull/3872) merged
  the correction into `dev`.

This proves the controlled heartbeat condition passes on its first attempt.
It does not measure the rate of future natural flakes.

## Diagnose a failure

An extra `heartbeat` in a raw event capture is normal. Inspect the assertion
that failed before assigning a cause: replay output, cursor use and generator
closure failures test separate requirements. Preserve the exact node id,
workflow run, job and attempt when comparing failures; a later successful
attempt does not erase the earlier failure.

Keep the forced server-heartbeat case beside its ordinary-wait control when
changing this regression. A timeout increase, retry wrapper, skipped case or
weaker replay or shutdown assertion does not repair the event expectation.
