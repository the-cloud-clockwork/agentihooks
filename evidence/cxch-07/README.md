# CXCH-07 live proof: herdr and the push-mode master

Run 2026-10-16 15:23 to 15:29 UTC on branch head `03fd0e89` (task branch merged with dev `a20cba82`).

## Fixture

- Scratch proof swarm `proof-323133-cxc8-1`: scratch Redis database 12, scratch ledger server on port 8797, isolated herdr server (scratch XDG dirs and socket), eng/ci/plan caps 0.
- The swarm controls are operator or master only, so the fixture's `codex_inbox_transport=app_server` and master lane `agent=codex` were written into the scratch database by an in-process driver right after `swarm create`; ticks were driven in process with `scripts.swarm.cli.run_tick`. No shared swarm, timer or Redis database was touched.
- The tick launched `master@2572f1-0001`: harness codex, transport `app_server`, execution `exe-36e5cdccdc554952a1b7cf9822bc343f`, thread `01a1454f-45f4-7111-a07c-c247c6be6587`, profile binding validated on the worktree revision.

## Native terminal attach

`raw/proc.txt`: `scripts.codex_session serve` and `codex app-server --listen unix:///run/user/1000/cx/36e5cdccdc554952/s` run in their own session (sid 328131 and 328198) with the Codex inbox pump; the herdr pane runs only `codex resume --remote unix:///run/user/1000/cx/36e5cdccdc554952/s 01a1454f-…` (sid 327565).

## Rows

| Row | Result | Observation |
|---|---|---|
| 1. Unsent draft in the master's input | pass | `raw/row1-draft.txt`: composer shows `› unsent draft cxc8 alpha bravo` while the master works a ledger promotion alert. |
| 2. Message received, draft intact | pass | Item `26344595a991` arrives in the live thread as `[swarm delivery]`; the master runs `agentihooks msg reply 26344595a991 "proof one received"`; the composer still reads `› unsent draft cxc8 alpha bravo` (`raw/row2-received.txt`). The ledger alert `ab1fa261b192` reached the thread the same way. |
| 3. Viewer closed, another message accepted | pass | `herdr pane close w1:p1` ends the viewer; server, app-server and pump keep running (`raw/row3-closed.txt`). A tick keeps the master `working` (`raw/row3-tick.txt`). Item `31e22a374cd2` is answered with `proof three received` and closed done (`raw/row3-send.txt`). |
| 4. Reopen: same thread | pass | `agentihooks swarm proof-323133-cxc8-1 terminal exe-36e5…` returns `{"thread": "01a1454f-…", "pane": "w2:p1"}`; the new pane runs `codex resume --remote` on the same socket and thread, app-server pid 328198 unchanged, and the history shows all three proof messages including the one answered while no viewer was open (`raw/row4-reopen.txt`, `raw/row4-pane.txt`). |
| 5. Push master takes new work after a finished turn, no `wait --inbox` | pass | Idle after `Worked for 8s`, the master takes item `e6e02335f6aa` as a new turn and replies (`raw/row5-received.txt`). The rollout holds 16 tool calls and zero `swarm wait` calls (`raw/row5-no-wait.txt`); its `wait --inbox` mentions are the shared profile text and the push prompt telling it never to run that loop. |

## Retirement by server identity

`raw/retire.txt`: `HerdrRuntime.retire` on the managed master returns true with no refusal; serve, app-server and pump processes are gone, the unrelated herdr server survives. A reopened viewer is not closed by retire and shows `Reconnecting to server…` until its pane is closed; filed as a follow-up.
