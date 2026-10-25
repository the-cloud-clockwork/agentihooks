# CXCH-07 live proof: herdr and the push-mode master

Two runs on 2026-10-16 against branch head `03fd0e89` (the task branch merged with dev `a20cba82`). CI: focused Proofs run 38928222292 on head `ffb5c4c0` (attach, lifecycle, managed lifecycle and viewer, push master, rename, resume, herdr host, init agent suites); the pull request checks grade the final head.

Commits after the proof change no reopen behaviour: the `typed_wake` docstring is removed, `_viewer_lines` reads `environ.get("AGENTIHOOKS_SWARM") or ""` in place of `environ.get("AGENTIHOOKS_SWARM", "")` (the same value for every string environment), and the rest are tests, clearances and evidence. Run 2 (15:35 to 15:41 UTC, `raw/run2/`) is the proof of record. Run 1 (15:23 to 15:29 UTC, `raw/run1/`) passed the same rows using engineer inbox messages in place of ledger chat lines.

## Fixture

- Scratch proof swarm `proof-323133-cxc8-1`: scratch Redis database 12, scratch ledger server on port 8797, isolated herdr server (scratch XDG dirs and socket), eng/ci/plan caps 0.
- Only the operator or the swarm's own master may use the swarm controls. After `swarm create`, `drive.py.txt` (run as `drive.py fixture`) wrote `codex_inbox_transport=app_server` and master lane `agent=codex` straight into the scratch database. Ticks ran in process through `drive.py tick`, which calls `scripts.swarm.cli.run_tick`. `drive.py chat` posts a page chat line as the operator does, and `drive.py rollout` summarises the master's Codex rollout. No shared swarm, timer or Redis database was touched.
- The tick launched `master@3ddefe-0001`: harness codex, transport `app_server`, execution `exe-f371eb9d95614ab2aae7186330f90f69`, thread `01a1455a-2695-78e3-b8c4-f55d9282b0d0`. Its profile binding was validated on the worktree revision with canary `0337244809274417c70b4c6a` (`raw/run2/binding.json`).

## Native terminal attach

`raw/run2/proc.txt` shows the processes. `scripts.codex_session serve` and its Codex inbox pump share session 387684, and `codex app-server --listen unix:///run/user/1000/cx/f371eb9d95614ab2/s` runs in its own session, 387872. The herdr pane runs only `codex resume --remote unix:///run/user/1000/cx/f371eb9d95614ab2/s 01a1455a-…`, in session 386937.

## Rows (run 2)

| Row | Result | Observation |
|---|---|---|
| 1. Unsent draft in the master's input | pass | `raw/run2/row1-draft.txt`: the master is working its first turn and the composer holds the typed draft `unsent draft cxc8 charlie delta`. The launch-time `/rename master@3ddefe-0001` sits in front of the draft; see Known issues. |
| 2. Ledger message received, draft intact | pass | An operator page chat line (`OPERATOR rev=35 message added on chat`) arrives in the live thread as `[swarm delivery]` with `"sender": "operator"`. The master runs `agentihooks msg reply 781639a37857 "chat one received"`, and the composer line is unchanged (`raw/run2/row2-received.txt`). On the ledger chat the line is answered by `swarm: Received by the master inbox.` and then `master@3ddefe-0001: chat one received` (`raw/run2/ledger-chat.txt`). |
| 3. Viewer closed, another message accepted | pass | `ps` before and after `herdr pane close w1:p1` shows the viewer gone while serve, app-server and pump keep running (`raw/run2/row3-closed.txt`). A tick keeps the master `working` (`raw/run2/row3-tick.txt`). Chat line three closes `done` on the master seat (`raw/run2/row3-send.txt`), and `chat three received` appears on the ledger chat. |
| 4. Reopen: same thread | pass | `agentihooks swarm proof-323133-cxc8-1 terminal exe-f371…` returns `{"thread": "01a1455a-…", "pane": "w2:p1"}`. `ps` after the reopen shows the same serve, app-server (387872) and pump pids, plus a new `codex resume --remote` on the same socket and thread (`raw/run2/row4-reopen.txt`). The history shows chat lines one to three, including chat three, answered while no viewer was open (`raw/run2/row4-pane.txt`). The draft lived in the closed viewer and is not restored. |
| 5. Push master takes new work after a finished turn, no `wait --inbox` | pass | The pane is idle after `Worked for 15s` (`raw/run2/row5-before.txt`). Chat line two then starts a new turn, which replies `chat two received` (`raw/run2/row5-received.txt`). `raw/run2/row5-rollout-summary.txt`: 13 tool calls, `swarm wait calls: 0`. Its 4 `wait --inbox` mentions are the shared profile text and the push prompt `never run agentihooks swarm proof-323133-cxc8-1 wait --inbox`. |

## Retirement by server identity

`raw/run2/retire.txt`: `HerdrRuntime.retire` on the managed master returns true with no refusal. Serve, app-server and pump are gone, and the unrelated herdr server survives.

## Known issues (filed as follow-ups, outside this change)

- The launch names the Codex session by typing `/rename` in the viewer. The server starts the first turn on its own, so the rename lands mid-turn and stays in the composer, and the thread keeps its default name.
- A reopened viewer is not closed by retire. It shows `Reconnecting to server…` until its pane is closed (captured in `raw/run1/retire.txt`; in run 2 the viewer still showed the composer when read three seconds after retire, `raw/run2/retire.txt`). The agent record keeps the launch pane id after a reopen.
- Codex 0.163.0 was released between runs. The run 2 reopened viewer first stopped at Codex's interactive update prompt (`Update available · 0.162.1 → 0.163.0`, options Update now, Skip, Skip until next version; herdr reported `"agent_status":"blocked"` in `raw/run2/row4-reopen.txt`). It was skipped with Escape. That pane read was overwritten by the history capture in `raw/run2/row4-pane.txt`. The viewer launch does not suppress the update check.
- The shared Codex profile text still maps Monitor to `swarm wait --inbox` for long-running work. The push prompt overrides it (`never run … wait --inbox, even where your profile instructions describe that loop`). Row 5 covers a run with no long-running work, and the full rollout stays outside the repository because it carries the whole profile prompt.
