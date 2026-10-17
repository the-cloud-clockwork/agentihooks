# CXCH-08 mixed-fleet qualification

Run on 2026-10-16 with codex-cli 0.162.1 and Claude Code 2.1.301, on the branch built on dev `572153ea` (CXCH-07 merged). The live run used branch head `eb5ae9c7` and later: the pump without the pre-connect hold. `qualification.json` is the manifest. It lists every row of the acceptance matrix with its status, its basis (live in this run, CI tests, or an earlier live proof), its evidence and its tests. Nineteen rows pass. Redis state lost is partial (below). No message bodies or credentials are kept here.

Full suite: merge group Tests run 38937215723 on `572153ea`. Changed tests on the head: pull request Tests run 38944785112 on `669c0319`.

## Fixture

- Isolated Redis: a `redis:7-alpine` container on `127.0.0.1:6439`. The shared Redis held no keys of the scratch slug afterwards.
- Scratch ledger `proof-323133-cxc9-1` on port 8839, an isolated herdr socket, and swarm caps of one engineer, no ci and no plan agents.
- The swarm repo is a `wt.sh tmp` checkout at the branch head, with the scratch venv installed editable from it. A Codex serve refuses a repo that is not the installed checkout.
- Ticks ran in process (`drive.py.txt tick`), with the promotion watch stubbed in the driver. The `promotion_watch` change in this pull request now does the same for every proof swarm.
- Transport `app_server`. Fleet A has a Claude master and a Codex worker. Fleet B has a Codex master and a Claude worker. Both Codex agents ran on the personal bearer account.

## Fleets

| Flow | Fleet A: Claude master, Codex worker | Fleet B: Codex master, Claude worker |
|---|---|---|
| Ledger writes | worker phase comments | worker phase comments; master chat and comments |
| Task message | master to worker seat; accepted 221 ms after send, steered into the running turn | the master, asked by operator chat, messaged the worker; delivered in 18 ms |
| Direct reply | worker reply delivered to the Claude master in 11 ms | Claude worker reply accepted by the Codex master pump in 84 ms |
| FYI fan-out | every agent; closed as information received | delivered to the worker in 12 ms |
| Viewer closed | pane closed; app-server and pump kept running; next message accepted in 115 ms | |
| Handoff | worker handed off; the successor took the seat as generation 4 and received seat mail in 151 ms | master handed off; the successor took the seat as generation 2 and received chat in 112 ms |
| Retirement | orderly: serve, app-server and pump ended (`raw/a-retire-*.txt`); the launch viewer outlived it and was closed by its pid | not orderly: the fleet ended through the Redis state loss fault and restore |

## Latency

There were 100 FYI deliveries to the busy Codex worker, one every 0.3 s, after its transport reported connected. Each sample runs from item creation to committed acceptance in the receipts journal.

- Median 0.1425 s, p95 0.224 s, max 0.239 s.
- No sample over 2 s or over 10 s.
- Codex worker launch checks passed 12 to 18 s after spawn.
- The same figures came from the item histories after the restore (`raw/latency.json`).

## Faults, live on the Codex master's pump

Command outputs: `raw/b-faults.txt` and `raw/b-snapshot-restore.txt`.

- **Redis unavailable.** The container was paused for 30 s. The pump logged `degraded: TimeoutError` and made no acceptance. Its owner lease lapsed during the pause, so after the resume it exited with `no longer delivers`. The swarm replaced the execution, and the next message went to the new one.
- **Socket loss.** The app-server was killed. The pump logged the lost stream. Both submissions were rejected uncommitted while the server drained (`Server is draining`). The item was held as pending, and the replacement execution accepted it exactly once. An unknown outcome after a send was not produced live; CI tests cover it.
- **Redis state lost (partial).** The swarm was snapshotted, then the database was flushed. Serve exited with `no transport record for this execution`, and nothing was resent. Restore was refused while agent processes lived. Once they ended, the swarm restored paused with both agents `awaiting-decision` and the inbox states kept. The Codex master's resume was refused as `unsupported resume: a managed Codex execution continues through its supervised server`. Gap: the restore does not bring back the receipts journal. A delivery in flight at the loss would return as pending with no journal to reconcile it against. The swarm master closed the row as partial: the defect has its own task in the runtime fixes phase.

## Late approval and the pre-connect hold

The probe is `late-run.py.txt` with `scripts.codex_app_server_probe late_approval`, run on both bearer accounts (`records/`).

- A client that only reads the thread never sees an open approval.
- A client that resumes the thread gets the open approval with the same id, after the resume reply and before the reply to its next `thread/read`.
- The pump starts with resume, then read, so it records an open approval before its first step.

The hold on a turn already running at connect is therefore dropped. Such a turn is now steered like any observed turn, and an open approval still holds delivery. Before the change, a live Codex worker whose opening turn never ended got no push delivery and pulled its mail through `msg inbox`.

## Live coverage limits

- The handoffs were ordinary, not quota triggered. The successor account choice rests on CI tests.
- No profile or model mismatch was launched live. CI tests cover it.
- The pump holding delivery on an open approval rests on CI tests. Those tests replay the approval in the order the live probe observed.
- The draft-intact case of the composing operator rests on CXCH-07 run 2.
- Both fleets ran on the personal bearer account. The business bearer account was qualified by the late approval probe only, and it was at its week quota warning.

## Not run

Token account: no access token kind Codex account is configured on this workstation. The swarm master ruled the row out for this run and filed a follow up.

## Observations

- Swarm restore does not restore the receipts journal. Item states are restored, and the swarm resumes paused awaiting decision.
- A launch viewer outlived retire once and was closed by its pid.
- Several replacement launches failed with `the harness did not report its route within 30s`. This stretched recovery from the socket loss to about 270 s.
- A proof swarm over a real checkout filed promotion fixer tasks, and its ci agent opened a real pull request, which was closed unmerged. `promotion_watch.refresh` now returns early for proof swarms.
