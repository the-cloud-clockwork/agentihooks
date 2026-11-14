# Releases

One entry per change merged into dev, newest release first. Each entry names its date, merge commit, pull request and author, so a change can be found by date and hash.

## v2.17.883 · 2026-11-14

### 2026-11-14 · ad199ea068e3 · #11174 · @agenticore-app

**Move claim cap tests to hub admission and map legacy spawn launch tests**

This change retires ten legacy tick-spawn tests for claim caps, launch budget, pool, steering, and capacity, replacing them with equivalent coverage in the new hub admission test suite so removing the legacy spawn step won't silently break untracked assertions. Tests without an events counterpart are kept in place and remapped under explicit retiring coverage entries per prior agreement, and the swarm-claim-lives coverage entry is dropped since no remaining test reaches the legacy spawn path. This keeps the coverage map consistent and prevents unannounced regressions as the legacy spawn code is phased out.

## v2.17.882 · 2026-11-14

### 2026-11-14 · 808f873261ab · #11160 · @agenticore-app

**Seat test agents through hub admission instead of swarm start**

This change replaces the heavyweight `swarm start` flow in test setup with a direct `seat_agent` helper that drives agent admission and launch acknowledgement straight through the `Admission` API, skipping tick passes and runtime spawning. The `test_block_gate` and `test_block_push` fixtures now use this seam, with new tests confirming the seated agent record's contract. This makes test setup faster and more deterministic while still exercising the real admission path, reducing flakiness and runtime overhead without changing any existing assertions.

## v2.17.880 · 2026-11-14

### 2026-11-14 · fbfbbb52f00c · #11139 · @nestorcolt

**Events admission claims carry reclaim, workspace, protected branch clear, lane kind, overlaps and group**

This change brings the events hub's admission claim up to parity with what the legacy place prepare used to set, so tasks admitted through the hub now carry reclaim resolution, workspace folder naming, cleared protected branches, lane kind defaults, overlaps and group data. Since the hub has no checkout, branch heads are read remotely via a time-bounded GitHub call that degrades gracefully to "unknown" rather than stalling admission. It also deliberately changes continuation ordering so a cleared protected branch now continues from an earlier life's branch instead of the stale protected one, closing a gap from the legacy behavior.

## v2.17.879 · 2026-11-14

### 2026-11-14 · 450e2bde4d4a · #11149 · @agenticore-app

**Prove pane evidence and exits sweeps on an events engine swarm**

This PR adds regression coverage proving the pane evidence and exits sweeps still work correctly on the events-only engine, ahead of removing the legacy launch engine. It seats agents the same way the events path does (via `next_name`, `start_execution`, and an `execution.started` event) rather than the legacy tick spawn, then verifies both the dead-pane case (incident saved, execution exited, agent dropped, task reopened, slot returned, mail moved) and the live-agent case (nothing touched). This closes a gap that would otherwise let the legacy removal silently break sweep behavior.

## v2.17.878 · 2026-11-14

### 2026-11-14 · 9cf5fabefd00 · #11145 · @agenticore-app

**Keep rows when the engine migration skips and drop the Doctor's stuck launch rows**

This fixes the engine migration so a refused config update is correctly reported as skipped while preserving all its rows, instead of silently succeeding and losing data. Separately, for idle swarms with no live agents, it now drops only the stuck "acknowledging"/"launching" launch rows (reporting the count), leaving started, failed, waiting, and pending rows intact since those still feed claim caps and live counts. This stops the Doctor's stuck launches from endlessly reporting as unchanged.

## v2.17.876 · 2026-11-14

### 2026-11-14 · d8b7eae05db7 · #11138 · @nestorcolt

**Events admission carries the handoff document and transfer attach to a successor**

This change updates the events hub admission claim so it attaches any pending handoff transfer left by a predecessor (from a recycle handoff or a stopping hive's recovery handoff) to the newly admitted successor and seat, mirroring legacy place-prepare behavior, so conversations resume properly across handoffs. If the attach is refused due to a concurrent write, the claim, agent, pending life and ledger row are cleanly released. Reopen and late admission paths now correctly detach the transfer for the next successor, preventing stale or duplicate transfer bindings, with new tests and a semgrep rule guarding against future regressions.

## v2.17.872 · 2026-11-14

### 2026-11-14 · 4ff3b4902c47 · #11115 · @nestorcolt

**Reopen claimed tasks with no live agent under the events engine**

This change fixes a long-standing gap where, under the events engine, tasks claimed by agents that no longer exist (no agent record, no claim) stayed stuck forever, since the hub was supposed to reclaim them but never did. The `orphans` tick step now actually runs `_orphans` instead of returning early, with a guarded reopen that respects claimant and claim generation so it can't clobber a claim taken after the check was read. Covered by new regression tests, a Semgrep rule catching the old early return, and updated feature docs; effects-port routing is deliberately left for a separate PR.

## v2.17.870 · 2026-11-14

### 2026-11-13 · a4fd1fdf2d4b · #11120 · @agenticore-app

**Start lane spokes on the root hive outside a distributed deployment**

`scripts.hive.lanes.serve` previously skipped starting the `lane-spokes` thread whenever `workstation.runs_engine` was true, which silently included the workstation root hive in non distributed deployments, leaving it with no eng, ci, or plan spoke and no presence beat, so pinned workstation offers had no taker. This change starts lane spokes on every hive with a NATS bus instead, keeping the memory bus refusal, and moves `fill_operator_env` before `lanes.serve` in `hive run` so operator env bus settings reach the spokes. A draining hive still polls no spoke but now beats presence on every events swarm, fixing hive loss detection.

## v2.17.869 · 2026-11-13

### 2026-11-13 · 0dc82496228c · #11108 · @agenticore-app

**Use the session harness and profile in a hive join report when the agent record has none**

This change fixes a false failure in hive launch checks: when admission writes an agent record without a harness or profile, `hive_join.py` was building the join context from that empty record, resolving `home` to an empty string and causing `join_checks.cheap` to wrongly flag profile, settings, and brain as missing even though the session was healthy. Now, at session start, the join context falls back to the current harness target and `AGENTIHOOKS_PROFILE` when the record has none, while still honoring a recorded harness or profile when present, eliminating spurious failures for correctly running agents like engineer 8508.

### 2026-11-13 · 1fbec8b0a56f · #11105 · @agenticore-app

**Download torn shard coverage again before building the dependency manifest**

This change addresses a flaky failure where `download-artifact@v4` reported success but silently delivered torn shard files, corrupting coverage data and truncating dependency manifest inputs. The dep-manifest job now verifies every downloaded shard by running `tests.coverage_check` and parsing each dep-inputs line before proceeding; if anything looks incomplete or corrupt, it automatically retries the download once, following the same wait-and-attempt pattern used elsewhere. New tests confirm the check correctly catches truncated lines and torn databases, passes clean input, and properly triggers the retry. This should eliminate the intermittent SQLITE_CORRUPT and JSONDecodeError failures seen in CI without requiring manual intervention.

## v2.17.866 · 2026-11-13

### 2026-11-13 · f9462f07f300 · #11099 · @nestorcolt

**Carry the ledger intent on events admissions so hives pick a profile without a ledger read**

This change fixes a bug where tasks admitted through the events pipeline could be claimed repeatedly without ever running, because hives had no identity to look up the correct profile from a hosted ledger. Now the hub attaches the project's intent and the task's own phase directly onto the admitted task at admission time, so profile_choice.state can classify the task locally on the hive using that carried data instead of requiring a ledger read the hive cannot perform. This closes the gap that caused repeated claim churn on the workstation route task and includes a regression test guarding against recurrence.

## v2.17.865 · 2026-11-13

### 2026-11-13 · e6f1fb92c592 · #11093 · @agenticore-app

**Close the dropped spoke bus before restarting NATS in the mid launch drop test**

This change fixes a flaky mid launch NATS drop test by explicitly closing the dropped spoke's bus before the server is restarted. Previously, the old client's pending buffer could flush on reconnect, overwriting the stranded claim's status with a stale "started" instead of "launching," causing intermittent mutation stats failures. Closing the bus first prevents this race; nats-py's close call raises a harmless TypeError while writing the pending buffer to the dead transport, which NatsBus.close already suppresses via its finally block.

## v2.17.864 · 2026-11-13

### 2026-11-13 · 4f66ffcbe4cd · #11091 · @agenticore-app

**Drop agents whose hive was removed from the hive index**

This change fixes a bug where agents assigned to a hive that had been revoked and removed from the hive index were never reaped, because `Reports.vanished` treated a missing executions report as "not vanished," leaving retiring and suspect records stuck forever and blocking their tasks from returning to ready work. Now, past startup, `Reports.vanished` returns True when the admitting hive is absent from the hive index and has no live heartbeat, correctly releasing those agents and freeing their claimed tasks, as confirmed by live production evidence and new passing regression tests.

## v2.17.863 · 2026-11-13

### 2026-11-13 · 70f0bc23f481 · #11079 · @agenticore-app

**Record each ledger export read's shape and caller, and each operations write's kinds**

Request records now capture the shape of each ledger export read (bare, full, since, refused, plus parts/scoped/events) alongside a bounded caller token from request_caller, and each operations write records its distinct op kinds, letting request_metrics split both routes by these labels. This matters because write p95 is far over target and some export and operations posts are unusually slow, but without shape, caller or operation kind recorded there was no way to trace that cost back to the periodic job or endpoint causing it. Write counts and p95 stay unchanged since writes are still classed by method.

## v2.17.862 · 2026-11-13

### 2026-11-13 · 15203fd4d20a · #11084 · @agenticore-app

**Renew the claim lease of hub owned agents in the reap pass**

This change fixes a bug where hub-admitted agents lost their task claim after ten minutes because the tick reap pass only renewed claims for legacy agents, ignoring hub-owned ones. A new `scripts.hub.owned_leases.renew` routine now extends the claim lease for every swarm agent the legacy list skips, acting only on claims still naming that agent, and is called on every reap pass. This stops hub-admitted agents from being wrongly stopped mid-execution while the ledger still names them as claimant, including across engine switches. Regression tests, a feature map entry, and a Semgrep rule guard against recurrence.

## v2.17.861 · 2026-11-13

### 2026-11-13 · 98e27b946d1b · #11086 · @agenticore-app

**Pin packaging in the swarm image lock so the hub admission imports**

This change adds the packaging==26.3 entry, with matching hashes, to docker/swarm/requirements.lock so the swarm image actually ships the dependency that scripts.hub.admission needs; previously that image installed only from the lock with --no-deps, so adding packaging to pyproject in #11075 didn't reach it, and compose-task-smoke and compose-recovery-drill crashed with ModuleNotFoundError. It also adds a regression test that walks real imports from the CLI, ledger server, and hub modules to confirm every third-party import is covered by the swarm lock, catching this class of gap automatically going forward.

### 2026-11-13 · a0d5021f7114 · #11081 · @agenticore-app

**Skip the SCM git scan in ledger load installs**

This change sets `SETUPTOOLS_SCM_PRETEND_VERSION_FOR_AGENTIHOOKS` to `0.dev${{ github.run_number }}` on both ledger-load install steps (load and compare), so setuptools_scm no longer runs a git status scan while building the editable install. That scan was timing out after 40 seconds in CI, failing the ledger-load workflow at the Install dependencies step. Since no ledger-load step actually reads the installed version, pretending the version is safe and matches the approach already used elsewhere (writer lock, feature suite). A new test asserts the env var is present on both install steps, and a dispatched run confirms the install now completes without invoking git.

### 2026-11-13 · df34cfaaa700 · #11075 · @agenticore-app

**Hub refuses events admission from a hive on an older release**

This change fixes a bug where an outdated hive could grab an events offer, fail to launch three times, and never reopen it, stalling the queue for fifteen minutes. Now, before any claim, execution, seat, or ledger write happens, the hub compares the hive's reported release version against its own using proper version ranking, and refuses admission outright if the hive is behind. Unknown or unparsable versions set no floor, and refusals leave any existing claim to normal lease recovery, so stale hives can no longer silently swallow work they can't complete.

## v2.17.860 · 2026-11-13

### 2026-11-13 · d27434f98bc8 · #11077 · @agenticore-app

**Hub readiness pass and admission log one line per offer pass and per refused or late admission**

This change adds the diagnostic logging that was missing during the events no-offers incident, where both hub pods ran silent and the cause had to be reconstructed from NATS and Redis. Readiness.poll now emits one line per events swarm per pass showing offer and placement counts, and Admission.admit logs each silent refusal (held by another hive, stopped claims, withdrawn offers, lost claim races) and each late admission, naming the swarm, task and hive involved. Tests in tests/hub/test_readiness.py and tests/hub/test_admission.py cover the new log lines, so future admission gaps can be diagnosed from logs alone.

### 2026-11-13 · 5c379676c83b · #11074 · @nestorcolt

**Complete a retire when no herdr server runs on the host**

This change fixes a stuck-engineer bug on the automation VM hive: when herdr itself has already stopped running, `_close_owned_pane` was raising an error instead of completing the retire, because it only treated "pane not found" as safe to ignore, not "no herdr server running." Now that error is handled the same way, since no server means there's no pane to close, so `HerdrRuntime.retire_process` can finish cleanly. This matters because the old behavior silently blocked over thirty retire attempts on a dead engineer process, holding a seat open despite the agent being gone and inbox/wake nudges never reaching it.

## v2.17.859 · 2026-11-13

### 2026-11-13 · b8cf932ead71 · #11068 · @agenticore-app

**Keep the repository feature map validity test under the worker timeout**

This change fixes an intermittent CI crash where the repository feature map validity test ran under the coverage tracer for up to 60 seconds, exceeding the worker's timeout and taking down the xdist worker. It narrows `locked_tests.measure` to only grade the mapped tests instead of all 9636 functions, cutting workstation runtime from 11.74s to 9.23s, and moves the validity test into a subprocess without the coverage tracer with a 50s timeout safely under the worker limit, plus a regression test guarding against unwanted functions being measured.

## v2.17.858 · 2026-11-13

### 2026-11-13 · 9df1a1d3620a · #11062 · @agenticore-app

**Inbox channel registration reclaims an abandoned Claude config lock**

Inbox channel registration previously took the Claude config lock with its own mkdir loop that never reclaimed stale locks, so one abandoned empty lock could block every later CI launch on a host, as happened on hive three. Registration now calls the same `_reclaim` helper used by trust marking whenever it hits `FileExistsError`, removing locks past `STALE_LOCK_SECONDS` while still waiting out younger ones. New regression tests cover both reclaim and non-reclaim cases, and the Semgrep incident rule and feature map entry now name this fix.

### 2026-11-13 · 45cfb7e99772 · #11065 · @nestorcolt

**Pass the controller liveness probe on a standby replica**

This change fixes a flaky kind-smoke failure where a standby controller replica got killed by kubelet because the liveness probe demanded a live swarm lease even when another replica legitimately held the lock and was doing nothing. The probe now checks `controller_probes.standing_by` and skips the lease assertion while another replica of the same hive holds the lock, avoiding spurious restarts. Unit tests cover standby, owning the lock, no lock, another hive's lock, and unnamed replicas, and a combined kind run with the related fix confirms the issue is resolved.

### 2026-11-13 · dd151d9ba8bc · #11060 · @agenticore-app

**Scoped ledger export reuses cached task bytes and change replies carry only new events**

Scoped ledger exports now rebuild each row from its task's cached fragment bytes instead of re-encoding everything, so only changed tasks cost CPU on a new generation—cutting scoped export time on a 41 MB ledger from 0.30 s to 0.21 s, close to the plain export's 0.15 s. Change replies also now send only events newer than the caller's held revision via `events_after`, shrinking a 5000-event reply from 634 KB to 353 bytes. Older servers reject the new parameter with a 400, and clients already handle that by retrying without it, so mixed-version deployments keep working until the server updates.

### 2026-11-13 · 6b784087b046 · #10866 · @nestorcolt

**Keep Codex-native MCP keys in the Codex entry and add the quick swarm**

This change fixes two things together. First, `codex_mcp_entry` was silently discarding Codex-native MCP keys such as env_vars, bearer_token_env_var, cwd, and timeouts whenever an entry was rebuilt during init or profile render, undoing manual repairs; it now carries those native keys forward, merging and secret-scanning them the same way Claude-shaped keys already are. Second, it adds a lightweight "quick swarm" tier that runs entirely in one herdr tab with no Redis or ledger server, including pane setup, seat state, prompt injection, handoff, idle-nudging ticks, and messaging between master and engineers, plus fixes from a live stress test.

## v2.17.857 · 2026-11-13

### 2026-11-13 · 928bacf42a44 · #11047 · @agenticore-app

**Add CI lab capacity document, contract metrics and ClickHouse rows**

This change adds the CI lab capacity document and metrics plumbing: `scripts.ci_lab.capacity` computes load, predicted wait, and utilisation to back the `GET /capacity` endpoint, while `scripts.ci_lab.metrics` builds a contract-driven registry (HELP/TYPE rendering, observe helpers) serving both `/capacity` and `/metrics`, plus `UNITS` and `GRADES` ClickHouse outbox tables for recording rows. It lays the groundwork for the scheduler to expose live load and task metrics once wired in, enabling the planned Prometheus query proof.

## v2.17.856 · 2026-11-13

### 2026-11-13 · 752f49ee78e3 · #11064 · @nestorcolt

**Pass the launch signing key in the controller rollout chart check**

This change fixes a failing helm-kind check by supplying the `controller.api.signingKey.keyId` and `secretName` values that the controller template now requires when rendering API test cases; without them, rendering the chart for any case with the API enabled failed outright, breaking kind-based CI on dev. The fix mirrors the same signing key values already used by kind-smoke's own API install, and was verified locally with `helm template` across replica counts, so the rollout chart check now renders successfully and CI can pass again.

### 2026-11-13 · 7d1002bc8fde · #11043 · @agenticore-app

**Refuse a monkeypatch of a target an autouse fixture mocks**

This adds a new CI lint, `scripts/ci_double_patch.py`, that scans every test file for cases where an autouse fixture mocks a target via `mock.patch`/`patch.object` while another fixture monkeypatches the same target, since the monkeypatch teardown can silently reinstall the mock after the fixture exits. It fixed the one real offender in `tests/context/conftest.py`, where three `prod_lockdown` getters were mocked by an autouse fixture but monkeypatched elsewhere; those now consistently use monkeypatch. The lint runs as a new protected grader step, with fixtures proving it catches real and clean cases.

### 2026-11-13 · e54a1dcafb4d · #11053 · @agenticore-app

**Count a timed out or failed reuse artifact download as no record**

This change fixes a flaky CI failure where test selection crashed if downloading a reuse artifact from a prior run timed out or errored. Previously `scripts.ci_test_plan.find_record` let a `CalledProcessError` or `TimeoutExpired` from the GitHub artifact zip download propagate, failing the select job outright (as seen in run 43380505546). Now `_usable` treats either failure as the run simply being unavailable, so the search continues to older runs or falls back to no reusable record, letting selection proceed normally instead of aborting the whole workflow.

### 2026-11-13 · 0970e06a4348 · #11033 · @agenticore-app

**Controller runs a standby replica and rolls out with no tick downtime**

This change lets the controller run multiple replicas so image rollouts no longer stall tick processing. A Redis-backed replica lock elects one leader to run passes and the control service, while standbys handle health checks and forward other API calls to the leader. The chart now supports a configurable replica count with RollingUpdate and a PodDisruptionBudget above one replica, and a new holder-scoped credential keeps the leader's session alive during handoff. This release still ships as a single replica; a follow-up change bumps replicas to two once this is deployed, enabling zero-downtime rollouts going forward.

### 2026-11-13 · 525494d016eb · #11046 · @agenticore-app

**Add CI lab queue state, leases and the lease API**

This change introduces the backend for the CI lab job queue: a Redis-backed queue tracking pending, leased, and completed jobs per head, a leasing module that hands out jobs to workers with expiry-based reaping and token-guarded completion, and an HTTP API exposing lease, heartbeat, complete, and upload-url endpoints. It ensures no job is ever claimed by two workers, expired leases are retried once before being marked lost, and all mutating requests require a valid live lease token, making concurrent CI worker scheduling safe and auditable.

### 2026-11-13 · d310f799a706 · #11050 · @agenticore-app

**Add the CI lab service contract: entry points, environment, ports, metric names**

This change establishes the formal service contract for the CI lab's scheduler and worker: it defines the environment variables each expects, fixes their network ports (8800, 9108), and names the twenty Architecture metrics as importable constants so platform tests can reference them reliably. It adds CLI entry points with offline self-tests, and crucially makes the worker refuse to start if any GITHUB_APP_* credential variable is present in its environment, enforcing the design rule that the worker holds no credentials. Documentation now tracks this contract and is tested against it.

### 2026-11-13 · 7d9f3e5f6201 · #11051 · @nestorcolt

**Release the hub admission when an events launch fails**

This fix closes a gap where an admitted hub task stayed permanently claimed if the hive's launch failed afterward: the hive would reset its own claim and nak, but the hub never withdrew the admission, so no other hive could pick up the task and the agent sat idle for about eight minutes until the controller retired it. Now any launch failure or admit-request failure explicitly reopens the task through the hub, and admissions that resolve after the hive's own request timeout are automatically released and refused as late, preventing the stuck-claim scenario that lost eight engineers on November 13.

## v2.17.855 · 2026-11-13

### 2026-11-13 · 094549c13daa · #11034 · @agenticore-app

**Ledger task update accepts the confirmed pull request number and head**

This change fixes a bug where confirming a pull request via `swarm pr` silently failed to update the ledger: since #10948, that command writes `pr_number` and `pr_head` fields, but the ledger's task update allowlist didn't recognize them, so every such update was rejected as schema_invalid and the task never advanced to the pr state. The fix adds both fields to the allowlist, validating `pr_number` as a positive integer and `pr_head` as a lowercase commit hash (or empty), with regression tests confirming malformed values are refused and that a real confirmed-pull-request update now passes the ledger check end to end.

### 2026-11-13 · 698e43894f38 · #10969 · @agenticore-app

**Health findings list the ledger rows behind their count**

This change makes health findings show their evidence, not just a count: each finding now carries the ledger rows (time, kind, target, short text) that produced it, up to the latest 20 plus a total, along with the agent's last outcome. `swarm status` and the ledger page's Swarm health row display these rows beneath every finding, labeled as required by doctrine or by the task, so operators can see exactly what triggered a finding instead of guessing. It also adds a regression test pinning that repeated parks don't falsely register as a proof loop, locking in existing scouter-verified behavior.

## v2.17.853 · 2026-11-13

### 2026-11-13 · 371b65b5cb50 · #11009 · @agenticore-app

**Document stored fragment reuse by identity on ledger writes and refreshes**

This change adds documentation to docs/reference/presentation-layer.md explaining how the ledger reuses stored fragments by identity during writes and refreshes, covering the fragment tree, the three ordered reuse checks and their cost, why identity-based reuse is safe, the write/cross-pod-refresh/rebuild paths, and the measured refresh CPU improvement from the earlier fix (#8569), plus a troubleshooting entry and index update. It matters because it records verified, source-checked behavior so operators can diagnose refresh CPU regressions and understand the performance guarantees without re-reading the implementation.

## v2.17.852 · 2026-11-13

### 2026-11-13 · d4fe5d5f72c8 · #11040 · @nestorcolt

**Type SwarmConfig.links so scripts.swarm.store meets its type baseline**

This change adds an explicit `list[Any]` type annotation to `SwarmConfig.links` in `scripts/swarm/store.py`, dropping the file's strict type errors from 742 to 740, back under the 741 recorded in `tests/TYPE_BASELINE.json`. Without this, any pull request that touches this file fails the type gate, as happened twice with #10866. Landing it as a standalone fix avoids bundling it with that larger PR, which already pushed mutation stats across hundreds of files past their budgets.

## v2.17.850 · 2026-11-13

### 2026-11-13 · a95ac2f0f362 · #11031 · @agenticore-app

**Implement lab fairness supersession and debounce policy**

This change introduces a standalone library that decides which pending lab jobs should be superseded versus kept, using priority classes and deficit round robin scheduling across branches so no branch starves while busier ones still make progress, and it adds a debounce mechanism that only restarts a quiet window when the active head actually changes. Leased and running work is always preserved. Acceptance tests exercise many heads, carried credit, uneven work sizes, and exact timing boundaries, giving confidence that scheduling stays fair and stable before this logic is wired into production.

### 2026-11-13 · 4d2c9746cc1e · #11029 · @agenticore-app

**Run the strict type gate before push in the CI test environment**

This change adds `scripts/type_env.py`, which builds the exact Python 3.12 virtual environment the CI type gate uses (via `uv venv` and locked constraints), caching and pruning per lock key, so the strict type check can run locally with CI-identical packages. `scripts/ci_prepush` now runs a "strict types" step using this environment, comparing against the merge-base baseline the same way CI grades it. This fixes false mismatches caused by the local dev venv having different stub/optional packages than CI, ensuring prepush accurately predicts whether the type gate will pass before pushing.

## v2.17.849 · 2026-11-13

### 2026-11-13 · 089cb1d9a547 · #11024 · @agenticore-app

**Add immutable lab models and computed unit pricing**

This change introduces frozen `Unit` and `Grade` dataclasses to represent lab test and mutant groupings immutably, along with a `Priority` ordering (hotfix, pull request, queue, promotion, backfill) derived from event metadata via `priority_of`. It also adds `DurationHistory` and `estimate` to compute expected run durations from historical data or sensible fallbacks, and `weight()` to size execution slots from CPU quotas. Together these give the scheduler a stable, well-tested foundation for prioritizing and sizing lab work, verified through red/green proof runs and full CI checks before merge.

## v2.17.848 · 2026-11-13

### 2026-11-13 · c2e574bf3730 · #11030 · @agenticore-app

**Preserve readiness publication after concurrent offer removal**

This change fixes a race where a consumer's acknowledgement or termination deletes a work queue offer right after readiness withdrawal has already read it, causing NATS to raise ServerError 10057 on the subsequent delete and aborting the sweep before fresh offers could publish. Now that specific "message already gone" error is treated as zero removals so the sweep continues normally, while any other server error still propagates and fails loudly. This prevents silent stalls in offer publication caused by benign timing overlaps, backed by real acknowledgement and termination regressions, a feature map entry, and a Semgrep rule guarding against regressions.

## v2.17.847 · 2026-11-13

### 2026-11-13 · b125476a6ef1 · #10891 · @agenticore-app

**Free dead managed Codex server slots and count reserved executions in host room**

This change fixes two gaps found in managed Codex execution tracking: when a Codex server process dies, its transport record was staying marked as active and continuing to hold an account slot indefinitely, so the liveness check now moves dead-server records to a failed state and frees that slot automatically. Separately, host room calculations were only counting live processes, making reserved-but-not-yet-started executions invisible to capacity accounting; read_host now counts these reserved slots so memory and load room correctly reflect pending work. Together these prevent stuck slots and inaccurate capacity reporting from silently blocking the swarm.

## v2.17.845 · 2026-11-13

### 2026-11-13 · e407a5efb737 · #10990 · @nestorcolt

**Report Gate Required from the Tests aggregator and announce the protected report step**

This change closes the gap that let PR 10904 merge with a known type-gate failure: Gate Required now only reports success once both the protected grade and the newest Tests aggregator check run have actually succeeded, stays pending while the aggregator has no run yet, and fails if either side fails. A new workflow reports this status from trusted default-branch code on every completed pull request Tests run, with a frozen gate-protected.yml blob and added checks:read permission. Tests, feature map, and runner registry entries back the change; checks are green on the head commit.

## v2.17.844 · 2026-11-13

### 2026-11-13 · 5ac8c4ecc5e3 · #11006 · @agenticore-app

**Keep the controller running when the ledger closed check cannot connect**

Previously, if the ledger connection dropped during the controller's binned or closed check, the raw connection error escaped as an unhandled `OSError` or a generic `SwarmError`, which the controller's pass loop didn't catch, causing the whole controller to crash. Now these failures are wrapped as `LedgerUnreachable`, which `_passes` already handles by ending that pass cleanly and letting the next pass retry. This means a temporary ledger outage no longer takes down the controller process; it just skips a pass and keeps running.

## v2.17.843 · 2026-11-13

### 2026-11-13 · 0aaa831b0a7b · #10963 · @agenticore-app

**Read every key of a swarm snapshot export in one atomic read**

This change fixes a race in swarm snapshot export where reading each key's type, value, and ttl as separate Redis commands could capture inconsistent state if a delivery closed mid-read, pairing stale items with newer journal entries or showing addresses as waiting with no pending mail. Now `_dump` reads every selected key plus every candidate membership across the waiting, indexed, open-indexed, and wake sets in a single Lua EVAL, making the whole read atomic. Key selection still happens beforehand, so mail sent during that window can still be missed; that gap is tracked separately as a follow-up.

## v2.17.841 · 2026-11-13

### 2026-11-13 · cbe9e4d00461 · #10972 · @agenticore-app

**Write a detached agent's harness and account onto its record at spawn**

This change fixes a blind spot in swarm status where a freshly spawned detached agent showed an empty harness and a dashed account while already working, making it impossible to tell which runtime it was using until well after launch. Now `HerdrRuntime.spawn` records the decided harness, routed account, model, effort and profile onto the agent record at the moment the launch job is queued, so `swarm status` reflects reality immediately; the launch result still overwrites these fields once collection completes, keeping them accurate end to end.

### 2026-11-13 · 06945e408047 · #10996 · @agenticore-app

**Bound the swarm history list in Redis**

This change fixes unbounded growth of the swarm history list in Redis, which was accumulating indefinitely with every retired agent and ballooning snapshot sizes across the swarm. Now, when an agent is retired, the history list is trimmed to only the newest rows worth keeping, applied consistently on both the watched transaction path and the compare and delete fallback path. This keeps Redis memory and snapshot sizes in check long term, directly addressing the ledger bloat observed in production where a single swarm's history list had grown to over 14,000 rows and 49.6 MB.

## v2.17.834 · 2026-11-12

### 2026-11-12 · 6383b192940c · #10894 · @agenticore-app

**Cap the mutation planner at 12 oss batch runners**

This change lowers the mutation planner's BATCH_RUNNERS from 40 to 12, with SHARD_CEILING adjusted to 24 (two waves), bringing the oss batch runner pool in line with the available CPU capacity identified after the November 9th scheduling incident where 43 runner pods sat pending. Planner tests and docs were updated to match the smaller pool, and the 12-runner cap is justified by historical data showing the largest mutation plan over 150 runs used only 23 shard jobs. Note the lint check comparing this against antoncore stays red until the paired antoncore PR (#1704) merges; rerun checks afterward.

## v2.17.833 · 2026-11-12

### 2026-11-12 · e529cc2768a9 · #10878 · @agenticore-app

**Keep web links whole when reshaping swarm notices**

`notice_text.plain` was mangling web links inside swarm notices, turning pull request and job links like `https://.../runs/.../job/...` into broken fragments such as `https: runs/ /job/` before they reached the ledger, even though the ledger's own check already strips links before applying its rules. This fix changes `plain` to reshape only the prose around links, leaving every link exactly as written, so follow-up comments like done gate and ready reopen notices keep working links to their gate jobs while still passing the ledger's plain words check.

### 2026-11-12 · 0fe5e9a3eebd · #10879 · @agenticore-app

**Close the writer lease lock file when writing the holder record fails**

When WriterLease.acquire took the flock but then failed to write the holder record (pwrite, ftruncate, or fsync), it left the file descriptor open and unreachable, so release could never close it and the flock stayed held for the rest of the process, blocking any future acquire. This fix closes the descriptor before re-raising on any of those three failure paths, so the lock is properly released and a subsequent acquire succeeds. Tests plant each failure mode and verify the lease is freed and the descriptor closed exactly once.

## v2.17.832 · 2026-11-12

### 2026-11-12 · 336e8a6e2c52 · #10862 · @agenticore-app

**Cite scout sources in a checkout relative to the repository root**

This change fixes scouts citing absolute worker checkout paths that the evidence schema rejects, which was causing hive-placed workflow runs to fail with scout retries and ultimately end in failure. Now `scripts.scout.loop` detects when a file source lies inside a git checkout and records it relative to that checkout's repository root, satisfying the schema, while sources outside any checkout remain absolute and are still refused. Guarded by two new CLI tests and a feature map entry, this unblocks hive-placed runs on swarm-hive-003 and similar environments.

### 2026-11-12 · 68e2fbb1572f · #10864 · @agenticore-app

**Log one line when each controller pass ends**

This change makes the swarm controller emit one structured JSON line to standard output each time a pass completes, recording the pass number and its outcome (ok, ledger unreachable, or redis grace). It matters because the antoncore controller start check previously burned over four of its six minutes on two fixed two-minute sleeps waiting for passes to finish; it can now watch for two pass-end lines instead, cutting wasted wait time while still confirming the controller actually ran with the new settings before anything reaches Anton.

## v2.17.831 · 2026-11-12

### 2026-11-12 · 71157ae8f253 · #10860 · @agenticore-app

**Stop a scheduler whose state store stays silent past a bound**

This change fixes schedulers that kept running forever against a dead scratch Redis after a proof finished. Now a runner that gets no answer from its state store for 300 straight seconds exits with code 3, and its supervisor stops instead of spawning a replacement, so the bound scheduler actually ends rather than logging failures indefinitely. Any successful response resets the counter. Callers of `ensure_running` and the systemd unit will start a fresh supervisor the next time the scheduler is actually needed, closing the orphan-process gap on the workstation.

## v2.17.830 · 2026-11-12

### 2026-11-12 · aba036f8e1d6 · #10854 · @agenticore-app

**Register the capabilities a worker hive's chart settings declare**

This change adds a `workers.capabilities` chart setting that renders into a `HIVE_CAPABILITIES` environment variable for every worker hive, including extras under `workers.hives`, with validation guarding against invalid characters like commas in names. Worker startup now passes this value to the hive set registration, meaning the chart becomes the authoritative source for capabilities: an empty list clears previously stored ones, while pods running an older chart without this setting preserve whatever was already registered, allowing the image to roll ahead of chart releases safely. A new smoke test confirms the render behaves correctly.

## v2.17.829 · 2026-11-12

### 2026-11-12 · 30ac392deb9f · #10859 · @agenticore-app

**Restart the scheduler runner when the checkout revision changes**

The scheduler supervisor now watches the checkout revision every second and restarts the runner whenever it changes, sending SIGTERM so the in-flight pass finishes before the old runner exits and a fresh one starts on the new code; an unreadable revision simply skips the restart rather than crashing it. This fixes the root cause of the workstation outage, where a stale runner missing the workflow slot kind kept failing hive timer deliveries with KeyError until someone restarted it by hand, and adds regression tests plus a feature map entry so future revision bumps pick up automatically without manual intervention.

## v2.17.827 · 2026-11-12

### 2026-11-12 · d2c225bec1ed · #10857 · @agenticore-app

**Expire placed hive run orders an hour after send in milliseconds**

Fixes a units mismatch that caused every hive-placed scheduled run to be rejected as expired on arrival: the placement sender computed expires_at in seconds-plus-one-hour while the hive run consumer compares against its millisecond clock, so orders expired instantly. The sender now writes expires_at in milliseconds, matching the consumer, with new tests covering both the sent value and the hour-long validity window. This unblocks scheduled workflows actually running on the Anton hives instead of failing immediately on receipt.

### 2026-11-12 · 47e310d513cb · #10845 · @agenticore-app

**Send a hive placed scheduled run's case text, never its case file location**

This fixes a bug where a scheduled workflow slot placed on a hive sent the case file's path rather than its contents when triggering a run, causing the hive run consumer to write the literal file location as the case instead of the actual case text. The slot now reads and sends the case file's contents, with a missing file correctly falling through to the scheduler's existing retry logic for failed deliveries. A regression test and feature map entry cover this, and two existing tests were updated to supply real case files.

### 2026-11-12 · b9f355a6ebc8 · #10842 · @agenticore-app

**Gate the legacy fencing import scan on the legacy controller epoch**

This change stops the hive join report from scanning the entire shared Redis keyspace on every session start. Previously, swarms without a fencing document triggered a full legacy import, walking all keys three times, which pushed master respawn past the 270s launch deadline and broke the canary. Now the scan only runs if the swarm's legacy control-epoch key exists, replacing a costly keyspace walk with a single cheap existence check, restoring timely master launches for new swarms while preserving correct behavior for legacy ones.

## v2.17.826 · 2026-11-12

### 2026-11-12 · bb262302eb29 · #10840 · @agenticore-app

**Export a proof swarm's scratch Redis setting into its panes**

This change fixes a bug where masters launched for scratch proof swarms inherited the operator's shared Redis setting instead of their own, causing SessionStart hive-join checks to scan the wrong Redis and time out past the 270 second launch deadline. The launcher script now exports a proof swarm's scratch Redis URL after the operator env, so its pane correctly points at the scratch Redis, while live swarm panes keep using the operator env setting unchanged.

## v2.17.823 · 2026-11-12

### 2026-11-12 · eabff56d720b · #10833 · @agenticore-app

**Report a night watch drill recovered only when a master is seated**

The night watch drill was marking a run as recovered just because a respawn timestamp existed, even when the front door master seat was actually empty — three live incidents on paused scratch proof swarms slipped through this way. This change tightens the recovered check in scripts.swarm.night_watch_drill.run to also confirm a live master is seated on the front door seat after the recovery pass, either actively working or having written to the ledger since launch, so the drill's report now reflects whether the swarm truly came back, not just whether a respawn was attempted.

### 2026-11-12 · f63887223740 · #10797 · @agenticore-app

**Read account slots from an index instead of scanning the Redis keyspace**

Account reconciliation used to find account keys by scanning the entire Redis keyspace with `SCAN MATCH`, costing 97 round trips over nearly a million keys and 0.85 s on the live swarm, enough to push the closing status-publish tick over its 2 s budget. Account slot writes now add entries to an index set, and reads use that index instead; a backfill walk runs once on an unindexed Redis and at most hourly afterward to catch older writers, with `reconstruct` always rewalking first. A Semgrep rule and feature map entry guard against reintroducing keyspace scans.

## v2.17.822 · 2026-11-12

### 2026-11-12 · d4a41f1e6d1a · #10827 · @nestorcolt

**List legacy state keys from stream subjects in the state migration command**

This change fixes the state migration command so it lists legacy state keys by reading key subjects directly from stream info instead of calling `KeyValue.keys()`, which internally opens a flow-controlled watch. On large buckets like the 49121-key legacy `inbox`, this watch hung indefinitely because the hub controller credential lacks publish permission on flow control subjects, causing the migration to time out. Keys whose last entry is a delete or purge marker are now excluded, preserving correct behavior while avoiding the permissions violation.

### 2026-11-12 · 6d07283dcbdd · #10825 · @agenticore-app

**Check github as the user login on the root hive**

This change fixes the root hive join check so it correctly detects github readiness for root sessions: previously, lacking HIVE_GITHUB_AUTH_MODE and AGENTIHOOKS_HIVE_ID, root sessions defaulted to app mode and probed gh api installation/repositories, which root always refused, falsely marking github false despite every other check passing. The join check now recognizes the user login mode for root, matching observed behavior from engineer launches 8254 and 8255. It ships with a regression test, a feature map entry, and a Semgrep rule to prevent this app-mode default from recurring in future join checks.

### 2026-11-12 · 0044d40f5fc3 · #10799 · @agenticore-app

**A retired master seat can never own a hive, and the retired seat list survives a manual master count racing the tick**

This change fixes two race conditions in master seat handling: a retired seat could still be assigned ownership of a hive, and a manual change to the master count could race against the tick's retirement logic, leaving a seat that should be retired off the retired list. Now `own_hive` and `check_hives` reject retired seats within a single watched transaction, and `set_count` rewrites the count together with the retired list atomically, so the two operations can no longer interleave badly. Regression tests and a feature map entry cover both cases, keeping the swarm's seat bookkeeping correct without operator intervention.

## v2.17.819 · 2026-11-12

### 2026-11-12 · fc578f1734f0 · #10806 · @agenticore-app

**Refresh the run order receipt on claim so an order outliving it starts once**

This change fixes a bug where a run order could start twice: if a worker crashed between starting a run and acknowledging it, the message receipt (good for one day) could expire before a long-lived order (up to two days) did, letting a second poll re-trigger the same run. The fix refreshes the receipt when the order is claimed, extending its lifetime to one day past the order's own expiry, so the receipt always outlives the order and a crashed-then-resumed run cannot be started a second time. A failing test proved the race before the fix landed.

## v2.17.818 · 2026-11-12

### 2026-11-12 · 451c79285b8e · #10808 · @agenticore-app

**Write the hive join report on the root hive**

This change fixes hooks.context.hive_join.run so it writes a join report for root hive sessions, which previously had no AGENTIHOOKS_HIVE_ID and were skipped entirely. Without that report, the controller's launch_check.misses logic treated root agents as missing and retired them, breaking every workstation launch (8241, 8242, 8243, 8128). The fix falls back to scripts.swarm.commands.hive_id when the env var is unset, accepting one extra Redis lookup per call, and keeps hook_manager._hive_join failures non-fatal. Includes a regression test, feature map entry, and Semgrep rule to prevent recurrence.

### 2026-11-12 · 379666f069da · #10812 · @agenticore-app

**Guard a day long launch order against a second start after a crash and an aged receipt**

This change adds a regression test to tests/hive/test_orders.py that locks in a bug fix for scripts.hive.orders Consumer.poll: a launch order whose expiry outlasts the one-day receipt window, crashed after spawn but before finish, and then polled again past a day must spawn exactly once and keep its started mark. No source code changed, only the test and a feature map entry, so this guards against a real double-start failure mode being silently reintroduced in future edits to the launch order logic.

### 2026-11-12 · eef55f6b0452 · #10785 · @agenticore-app

**Emit hive and agent presence over OpenTelemetry and read it in the HOME monitor**

This change makes every hive and every running agent announce themselves over OpenTelemetry, so the HOME resource monitor can see live status directly instead of relying only on the Redis registry and herdr panes. Hives emit presence every 10 seconds with id, name, headed flag, release and per-harness agent counts; agents emit presence on every hook event. The monitor reads these metrics from the collector's Prometheus endpoint, falling back safely to the old registry when that source is unset, silent or empty, so no previously working counts regress while the automation VM and headless hives finally show up.

## v2.17.814 · 2026-11-12

### 2026-11-11 · 2c85ec91e3c6 · #10788 · @agenticore-app

**Docs sanity: agnostic matchers for lab names, cloud hosts, private IPv6 and secret values**

The docs sanity checker now has built-in, keyword-list-independent matchers for lab names, cloud-host identifiers, private IPv6 ranges, and secret-like values, so public docs stay protected even without a private keyword list. A baseline mechanism tracks per-file/rule hit counts that can only shrink, with address and secret hits never allowed to be grandfathered in, and CI grades against a protected baseline copy so pull requests can't inflate their own allowance. Existing docs and fixtures were redacted in place to pass cleanly, with the 730 pre-existing lab-name lines held by baseline pending future scrub work per docs area.

### 2026-11-11 · 29aceef80cc7 · #10781 · @agenticore-app

**Count every live interface hive's herdr reading in the HOME monitor**

Fixes the HOME resource monitor so it counts the herdr reading from every live interface hive instead of stopping at the first one that publishes. Previously only one hive's reading was used (whichever came first in the registry), so the monitor undercounted hives and agents whenever multiple interface hives, like root and the automation VM, published readings. Now each hive is counted once from its own reading, or from another hive's reading that names it if its own is silent, giving an accurate total agent and hive count regardless of registry order.

### 2026-11-11 · 976534001ea9 · #10741 · @agenticore-app

**Add the workflows MCP category**

This change introduces a new "workflows" category to the MCP tool registry, exposing five tools—workflow_list, workflow_run, workflow_status, workflow_schedule, and workflow_unschedule—so agents can discover, launch, check, and manage scheduled workflow runs directly through MCP instead of needing separate interfaces. It builds on the earlier scheduler work that added workflow targets to scheduling commands, completing the wsc3 task in the scheduled workflows phase and giving the scheduler a full set of tools to start workflow runs and surface results back to the owner's inbox.

## v2.17.813 · 2026-11-11

### 2026-11-11 · 63e2b3923645 · #10756 · @agenticore-app

**Hub withdraws stale work offers before publishing fresh ones**

Before publishing a fresh work offer, the hub readiness pass now walks every lane queue in the swarm and withdraws stale offers first, so tasks that are gone, claimed, closed, moved to another lane, or otherwise invalid no longer sit ahead of a valid offer in the queue. This fixes the October backlog of roughly 7000 withdrawn eng offers blocking fresh delivery. The withdraw capability was added to memory, NATS, and scoped work queues, with NATS deleting stale messages directly from the lane stream, and the controller's NATS credentials were extended accordingly, contingent on a matching grant in antoncore.

## v2.17.810 · 2026-11-11

### 2026-11-11 · ded55f07ab36 · #10671 · @agenticore-app

**Document how a stopped swarm tick releases its locks inside the service timeout**

This change adds documentation to docs/pillars/swarm.md explaining how a swarm tick behaves when stopped inside its service timeout: the SIGTERM and 450-second deadline paths, the stop sequence (flag, hook check, grace period, lock release, exit), the in-process holder record, and how the deadline relates to measured tick timings from 17 October. It also adds read-only verification steps, a troubleshooting table, and links from docs/pillars/index.md, giving operators a clear, checked reference for diagnosing timeout kills and confirming locks are correctly released rather than leaked for the next tick to clean up.

### 2026-11-11 · 503709678944 · #10731 · @nestorcolt

**Pass refused accounts into hive master selection so a launch retry leaves them**

This fixes a master-launch retry loop found while profiling master scale CPU cost: when a master launch is refused because the claude:unrouted account is full, `_placed` reselects but `_master_selection` only honored CLOSED capacity rows, so `placement.place_master` kept re-picking the same full native login tens of thousands of times until the tick lock went stale. The fix threads refused `harness:account` keys into `_master_selection`'s closed set so a retry moves to a subscription slot or waits instead of spinning, cutting the CPU burned while holding the placement lock that blocks spawn.

### 2026-11-11 · 1428a13fd627 · #10675 · @agenticore-app

**Document the scheduled swarm tick holder and its journal identifier**

This change adds a new "The tick holder" section to the swarm pillar docs, describing how the minute tick now runs under the agentihooks-scheduler cron rather than a systemd timer: the shared `swarm-tick` machine lock, stop and kill-grace timings, process group sweep, fixed environment, cron re-registration, log locations, and the fallback unit's `--unit` flag with its own start timeout and troubleshooting notes. It also restores the swarm-specific journal query convention to keep tests passing, and adds an index link and changelog entry, giving operators accurate documentation matching the already-merged scheduler migration from #4546.

## v2.17.808 · 2026-11-11

### 2026-11-11 · 15c38d20d319 · #10658 · @agenticore-app

**Keep a hive hosted launch's pane off the controller's failed pane hold**

This change fixes the tick observe step so that retiring a launch hosted on another hive now correctly recognizes the agent as remote by checking `runtime_target.host_id`, letting the hive retire its own pane instead of leaving it on the controller's failed-pane hold. Previously the controller kept panes like 8036, 6052, 5871 and 7491 in its hold despite having no herdr server to read or close them, causing repeated retry failures. A new regression test, `test_a_hive_hosted_launch_keeps_its_pane_off_the_controller_hold`, guards against this recurring.

### 2026-11-11 · 261f5120b777 · #10693 · @agenticore-app

**Refuse unbounded whole store reads in swarm and tick code**

This change adds a semgrep rule that flags unbounded whole-store reads (hgetall, uncounted scans, keys) in scripts/swarm and scripts/tick, since these can block or overload the store as data grows. Existing uncounted reads are temporarily allowlisted per-module, tied to follow-up cleanup tasks, and a CI check now enforces that this allowlist only shrinks over time rather than growing. Fixture tests verify the rule catches the right 12 cases and ignores 10 safe ones, keeping future code from reintroducing this performance risk.

## v2.17.807 · 2026-11-11

### 2026-11-11 · 0a68dfacb590 · #10713 · @agenticore-app

**Document the swarm tick OTEL metrics and Swarm Tick dashboard**

This change adds the missing documentation for the swarm tick OTEL metrics and the Swarm Tick Grafana dashboard delivered earlier in #9549, closing a gap where the feature worked but was unexplained. The new reference page covers the five metric names, labels, buckets, when each is recorded, delta temporality over OTLP HTTP, relevant environment variables, the antoncore collector path, dashboard panels and PromQL queries, plus verification, limitations, and troubleshooting guidance, so operators can confidently find, interpret, and debug tick metrics in Grafana.

### 2026-11-11 · c3d18e75507b · #10652 · @agenticore-app

**Ledger writes report time per write substep**

This change breaks down each ledger write's recorded time into its four substeps—hierarchy sync, outbox write, refresh record, and event append—and attaches those timings to request metrics write records, summary totals, and journal samples, the same way lock, client, and Postgres waits are already tracked. It matters because recent production measurements showed slow writes (up to 18.9 seconds) with large unattributed time gaps, and the freeze list rewrite theory was ruled out; this instrumentation lets the next live measurement pinpoint exactly which substep is causing the slowdown instead of guessing.

## v2.17.805 · 2026-11-11

### 2026-11-11 · 587c2e283847 · #10679 · @agenticore-app

**Document the swarm tick Redis round trip reductions**

This change adds a new reference doc explaining why the swarm tick and `swarm set` now finish in under a minute on the shared Anton Redis, covering the per-call round trip cost and the specific fixes that cut calls: one placement read per agent, showing only the newest 100 ended lives via `HISTORY_SHOWN`, dropping unread stream records, and batching account scans with `COUNT 10000`. It includes measured before/after timings, limits, and troubleshooting steps, verified against current code, giving operators a clear reference for diagnosing future latency regressions on this path.

## v2.17.802 · 2026-11-11

### 2026-11-10 · b12b8c4d290c · #10567 · @agenticore-app

**Ignore the project local enforcements store so checkouts holding it stay clean**

This change adds the `.agentihooks/enforcements.json` local enforcements store, and its `.tmp` write sibling, to `.gitignore`, without touching or moving the store itself. Previously, running `agentihooks enforcement --local` left an untracked file that git saw as dirty, which caused the push stop gate to wrongly refuse stops on otherwise clean checkouts. Regression tests confirm checkouts with the store now read clean to the stop guard, the post `wt.sh done` sync check, and plain `git status`, fixing the false refusals engineers were hitting.

### 2026-11-10 · aadcd1521ca4 · #10555 · @agenticore-app

**Guard tick site hook pass box reads at runtime**

This change extends the recorded pass box discipline, previously checked only in a dedicated test, into runtime for both tick site hooks and the wrapper's hooks. A new `scripts.tick.hook_reads.guarded` wraps hook maps so every pass box read made while a hook runs is checked against its declared keys, with `PassBox` reporting reads to the guard. Behaviour is controlled by `AGENTIHOOKS_TICK_HOOK_READS`, defaulting to log-only `observe` mode, with `enforce` and `off` available. This closes a gap where undeclared reads outside the test harness went unnoticed, matching the enforcement-rollout rule of logging before raising.

### 2026-11-10 · 50675d08affc · #10515 · @agenticore-app

**Add a project agnostic docs sanity check and scrub public docs**

This change adds a standard-library docs sanity scanner that flags private IPs, personal emails, internal hostnames, and secret-like tokens without printing matched text, wired into Tests' lint job via a reusable workflow so pull requests can't weaken the rules that grade them. It also scrubs 145 real hits across 18 docs/evidence files, replacing them with documentation-safe placeholders (RFC 5737 addresses, example.com, example.internal), preserving table meaning while preventing accidental leakage of sensitive data in public documentation going forward.

### 2026-11-10 · abd90fb95a28 · #10554 · @agenticore-app

**Count workstation agents by headed hive, not the sampler's hive id**

This fix corrects `metrics_swarm.placement_counts`, which was miscounting workstation agents as zero because it compared each agent's `runtime_target.host_id` against the sampling process's own hive id, now the Anton controller pod rather than a workstation. The sampler now identifies workstation agents as local agents on a headed hive (`ui=yes`) from the hive registry, falling back to its own hive if none is headed, matching the existing `placement.workstation_hives` rule and preserving old behavior for agents with missing host ids. This restores agreement between the Grafana panel and the hive registry's local agent counts, regardless of where the sampler runs.

## v2.17.801 · 2026-11-11

### 2026-11-10 · 79882da9fbeb · #10512 · @nestorcolt

**Add an opt-in step profiler for named tick steps**

This change adds an opt-in, in-process profiler to `timing.within()`, the single seam where a tick step's body executes, so that steps named in the new `AGENTIHOOKS_TICK_PROFILE_STEPS` env var run under `cProfile` and emit a `swarm_tick_step_profile` JSON line with the top 25 functions by cumulative and own time. It exists to diagnose the `collect acks` step burning 9.8-22.9s of per-thread CPU against a 7s budget, stalling sibling threads, since py-spy can't attach in the pod. With the env var unset, behavior is byte-for-byte identical to before; this is diagnostics only, verified by tests that the profiler is never invoked by default.

## v2.17.800 · 2026-11-10

### 2026-11-10 · 725ec6c56d38 · #10523 · @agenticore-app

**Admit the hosted only docs sanity reusable workflow in the runner trust gate**

This change updates scripts/ci_runner_trust.py so the runner trust gate explicitly admits calls to agentihooks' docs-sanity.yml@main reusable workflow, since its only job is pinned to ubuntu-latest and needs no runs_on input or private runner guard. Other refs, filenames, or owners remain rejected. This unblocks agentibrain-kernel's lint job, which runs this gate over agentihooks workflows, letting it call the docs sanity workflow as its required public docs check once the callee lands on main. Tests confirm the admitted call passes while @dev refs, renamed callers, and foreign owners stay red.

## v2.17.799 · 2026-11-10

### 2026-11-10 · a3f23306a0a4 · #10537 · @nestorcolt

**End a Codex viewer once its app server exits**

This change fixes a hang where a Codex viewer stuck on "Reconnect failed" after its app server died, since the launcher never returned and session_exit never ran. Now `attach.watched` wraps the viewer and polls `attach.server_ended` every WATCH_S seconds, terminating it if the transport record shows exited or failed, if the recorded server pid is no longer alive, or if the execution has lost its seat; an unreadable store is treated conservatively and leaves the viewer running. `attach.main` now uses this as its default runner, ensuring dead app servers reliably release their viewers instead of leaving zombie sessions.

### 2026-11-10 · 6685a6180f80 · #10538 · @agenticore-app

**Read the intent check pull request batches at the same time**

This change speeds up the intent check step by running the batched pull request reads in `ledger_events.pull_texts` concurrently instead of sequentially, within the existing GitHub pool limit, while properly carrying context into each worker. Previously each 20-pull GraphQL batch ran one after another, adding up to a median 3.4 s; local testing on 29 watched pulls shows this drops from roughly 3.2-3.3 s sequential to about 1.7-2.0 s in parallel, directly reducing the intent check step's overall runtime and improving tick reliability.

### 2026-11-10 · 48bb2beed421 · #10535 · @agenticore-app

**Resolve bundle overlays on the target hive instead of the hosted controller**

This change fixes launches that previously failed when a hosted controller held only package roles but a bundle overlay, like frontend, belonged to the placement target hive rather than the controller. Instead of rejecting the order with an overlay-not-found error, the controller now proceeds without resolving overlays, and the hive member resolves the task's overlays (or its base role's default) against its own bundle, attaching them to the launch assignment so the agent launches correctly dressed. If the hive itself cannot resolve an overlay, the order still fails cleanly before launch, preserving safety while fixing the false-negative failures.

## v2.17.797 · 2026-11-10

### 2026-11-10 · 0c8dbac66380 · #10355 · @agenticore-app

**Count a merge stall and merged tasks left in pr as swarm blockers**

This change adds two new swarm blocker categories: a one-hour merge stall with no dev merge, and a task left in a pull request twenty minutes after it merged with no wait excusing it. Both feed into a new `stall-blockers` gate, defaulting to observe-only logging, so these stalled states now trigger the master coding window automatically instead of going unnoticed. Enforcement can follow once observed data is reviewed, closing a gap where real work stoppages weren't recognized as blockers.

### 2026-11-10 · 0642edb5db4a · #10522 · @agenticore-app

**Document plan read through the hosted ledger on a remote hive**

This change is documentation only, adding a new section to the deployment modes reference that explains how plan read behaves when run from a remote client: it covers which ledger address and credentials get used, local versus remote read sources, a worked hive example, the no-plan-lines output, the two rewritten error messages plus passed-through HTTP errors, and the tests backing that behavior. It also adds two troubleshooting rows for the "no ledger" and "no stored artifact" errors and a changelog fragment, so operators can diagnose remote plan-read issues without digging through code.

## v2.17.796 · 2026-11-10

### 2026-11-10 · c92747fcbd2e · #10528 · @agenticore-app

**Document the ledger ports, adapters and import boundary**

This change adds a new reference page, `docs/reference/ledger-ports.md`, documenting the earlier work that decoupled the ledger package from direct imports of swarm, hive, and hooks internals. It explains the `Swarm`, `Hive`, and `Hooks` interfaces the ledger now owns, their adapters, the composition root wiring, how to bind a fake in tests, how to add a port member, and how the import check guards the boundary, plus troubleshooting guidance. It also adds a reference index row and a changelog fragment, so the decoupling behavior delivered earlier is now discoverable and maintainable rather than only living in code and commit history.

## v2.17.794 · 2026-11-10

### 2026-11-10 · 3b2109b8a64d · #10474 · @agenticore-app

**Document hive invite and the timed hive reproduce run**

This change fills a gap in the reproducible hive runbook by documenting two previously undocumented pieces: the `agentihooks hive invite` command, including its flags, the OpenBao keys it writes, NATS signer scoping, rerun and failure revocation behavior, and the force sync and restart a running pod needs; and `scripts/packaging/hive-reproduce.sh`, covering its seven timed steps, every `HIVE_*` setting, the step and timing output, and the exit-time `hive remove` unless `HIVE_KEEP=yes`. Each statement was verified against the actual source on dev, so operators now have an accurate, complete reference for standing up and timing a new hive from scratch.

### 2026-11-10 · 526a1fd90bee · #10469 · @agenticore-app

**Document hive first placement with local machine fallback and its cap**

This change adds documentation to docs/pillars/hives.md explaining how worker spawns are placed when hive placement is enabled: hives with available room are tried first, with a fallback to the local machine as long as it stays under its own cap, defined as the smallest of local-max-agents, the machine's hive max-agents, and host room. It includes a concrete eight-agent workstation example, verification steps using the local slot claim journal line, and a troubleshooting table, all cross-checked against the actual placement and overflow logic in the codebase so operators can understand and verify this previously undocumented but already-shipped behavior.

### 2026-11-10 · 3a676ba3a2c3 · #10345 · @agenticore-app

**Remove dead hive machines from herdr**

This change teaches the operator's terminal sync to prune hive machines that are no longer alive, so stale entries stop cluttering the registered list. Explicit removal now writes a shared retirement record, while registered hives that fail herdr status checks for fifteen straight minutes are automatically expired, including ones whose worker never reports in; successful pings or target swaps reset that timer, and brief restarts or manually added machines are left alone. Cleanup deletes the host key before the saved registration record so a failed key removal can be retried safely, and the reason for removal is logged for traceability.

### 2026-11-10 · 1c3f2b5b5472 · #10468 · @agenticore-app

**Document hive refresh trust and test environment isolation**

This change adds documentation only, with no code behavior changes. It explains how hive refresh decides which CA to trust (LEDGER_CA_PEM first, then system roots plus an existing CA file, then system roots on first refresh) in docs/pillars/hives.md, and clarifies in CONTRIBUTING.md how to safely set environment variables in tests using monkeypatch inside an empty patch.dict to avoid leaking values between tests, referencing the earlier LEDGER_CA_PEM incident that caused a confusing test failure. This closes the gap left by the earlier fix so future contributors understand the trust logic and avoid repeating the same test isolation bug.

## v2.17.790 · 2026-11-10

### 2026-11-10 · d81e8386cf38 · #10443 · @agenticore-app

**Document hive settings API with the excluded list**

This change updates the hive settings documentation to match the already-shipped exclusion-list behavior from PR #5309: the GET/PATCH examples, field tables, and curl samples now reflect `excluded` and `max_agents` (integer or `auto`) instead of the retired `roles` field, which is explicitly marked as refused. It also adds worked examples for setting, clearing, and auto-resetting these fields, documents the default placement rules for headless (`ui=no`) versus headed (`ui=yes`) hives, and explains migration for hives still using the old `roles` field. This keeps the docs accurate and prevents confusion for operators configuring hive placement.

## v2.17.789 · 2026-11-10

### 2026-11-10 · c236b2942905 · #10458 · @nestorcolt

**Make proven-cause-first master culture default doctrine**

This change codifies the 10 November rule into the package master role's rule file, CLAUDE.md, and toolbelt rules so it's enforced by default rather than relying on memory or ad hoc reminders: no fix may be written, pushed, queued, or merged until a scouter has proven its root cause with evidence. This closes a gap where unverified fixes could slip through, protecting against wasted effort and regressions caused by patching symptoms instead of actual causes.

## v2.17.788 · 2026-11-10

### 2026-11-10 · fdc245c03f36 · #10459 · @nestorcolt

**Give the tick view's pull request batch cache its own TTL**

This change fixes tick_view's pull-request batch read repeatedly refetching cold from GitHub every pass, costing 35-39 seconds each time, because its cache entries expired under the shared 60-second TTL while tick passes only run 88-114 seconds apart. It adds an optional ttl parameter to github_reads.snapshot (defaulting to the existing shared value, so no other caller is affected) and threads it through ledger_events.views, with tick_view now passing a new 180-second TICK_VIEW_TTL_S constant. The batch snapshot now survives across tick passes instead of expiring every time, eliminating the redundant large GraphQL refetches without touching any other caching behavior.

## v2.17.787 · 2026-11-10

### 2026-11-10 · 050450aafb26 · #10386 · @agenticore-app

**Start the handed tick lane with the pass graph and keep spawn out of site barriers**

This change lets the handed slow lane begin right alongside the critical pass instead of waiting on it, using its own executor so a busy slow lane can't steal workers from critical steps, and fixes site gating so sites wait safely without blocking across sites. It also stops spawn from acting as a false barrier for later sites, while settle still correctly waits on it. Live testing showed this cuts pass p95 from 122.5s and fixes 18 steps that were previously dropped before they even started, closing out issue #10385.

## v2.17.786 · 2026-11-10

### 2026-11-10 · c38be736bdbc · #10452 · @nestorcolt

**Fix O(n^2) admitted-focus recomputation behind slow time_left writes**

This change fixes the real cause of slow `time_left` writes on large ledgers: `ledger_stats.frozen()` was recomputing the admitted-task set once per pending task and rescanning all waiting tasks every propagation pass, producing quadratic blowup (3.3s at 5000 tasks, confirmed by profiling), not the storage-layer slowness originally suspected. It now computes the admitted set once and stops the dependency loop as soon as nothing new is added, cutting the same workload to 80ms (41x) with identical results, verified by new deterministic call-count tests.

## v2.17.780 · 2026-11-10

### 2026-11-10 · ee32580eb0da · #10319 · @agenticore-app

**Reserve hive create's chosen name atomically before the manifests pull request opens**

This change fixes a race where two concurrent hive create runs could both pick the name swarm-hive-001, causing failures. Now the chosen name is atomically reserved in Redis with SET NX plus an expiry before the manifests pull request opens, so a name already taken by another run is skipped or rejected if explicitly requested. The reservation is restored on resume until its pull request exists, and safely released via compare-and-delete on failure or completion; dry runs reserve and release nothing.

### 2026-11-10 · e6c194d166e6 · #10301 · @agenticore-app

**Keep controller probes responsive on busy nodes**

This change replaces the controller's exec-based liveness and startup probes with lightweight HTTP checks against the loopback host when API access is enabled, fixing the root cause of the 9 November kill where a timed-out exec probe terminated a controller under CPU load. Readiness now queries Redis with a four-second deadline and limits concurrent background checks. The CPU request rises from 50m to the master-approved 250m, matching real usage so the pod still schedules on constrained nodes without going Pending. Lease-based liveness when API access is disabled is untouched, and the chart test now enforces these probe and request values.

### 2026-11-10 · 3290dc809498 · #10382 · @agenticore-app

**Document ledger watchdog signal attribution**

This change adds documentation for the ledger watchdog's restart-signal logging, covering where records are written, what fields they contain, how target and caller processes are attributed, how folder configuration affects behavior, and how to inspect records for restart order and logging failures. It links the new reference page from the existing docs index and clarifies the difference between current reload records and direct signal attempts, so operators can now trace why a ledger server exited and who triggered it, closing a prior gap where clean exits left no attributable evidence.

## v2.17.779 · 2026-11-10

### 2026-11-09 · b26469f58586 · #10363 · @agenticore-app

**Keep the tick intent check inside its eight second budget**

This change fixes the intent check step blowing past its eight second tick budget under load. Per-task GitHub reads for every pending pull are replaced with one batched GraphQL read per pass; drafts are recorded without a view; and only two changed pulls per pass get judged, with their view build and model call moved to background workers that outlive the pass and are picked up by a later pass once done. Guarded by new budget tests, a Semgrep rule against per-task reads, and three rounds of review; stale judgment keys are now cancelled and unwatched tasks dropped.

## v2.17.778 · 2026-11-10

### 2026-11-09 · 7e1d7f59a4c2 · #10371 · @nestorcolt

**Compute the spawn claim order once per pass and fit the spawn budget**

This change fixes spawn passes timing out and abandoning under load. Previously, each lane recomputed launch order from scratch and readiness checks rescanned all freeze and focus records twice per task, building discarded reason text along the way, causing 26–41 second spawn runs that ran out of budget while urgent tasks waited. Now launch order and freeze targets are computed once per pass and reused across lanes, cutting `_launch_order` calls from 3 to 1 and eliminating 3.3 million redundant freeze coverage checks, with identical results verified against a real ledger replay.

## v2.17.775 · 2026-11-09

### 2026-11-09 · 3c0e3c23216f · #10285 · @nestorcolt

**Keep a passed hive join grade through the heartbeat**

This fixes a bug where join_report.heartbeat re-ran its cheap checks every 180 seconds, sometimes overwriting a check that had already passed at join with a false "missing" result if the agent's pane or home vanished after its session ended but before end_gone finalized the report. Live data showed 193 ended reports incorrectly marked failed despite passing their launch checks. The heartbeat now only re-probes checks still missing at join, preserving any check that already passed, so finished agents no longer get wrongly downgraded.

## v2.17.772 · 2026-11-09

### 2026-11-09 · 8210750ced17 · #10188 · @agenticore-app

**Avoid duplicate worker base downloads and daemon unpacking in kind proof**

This change fixes the kind proof so the worker base image is no longer downloaded twice and unpacked redundantly by the Docker daemon. It switches to a container buildx builder that publishes directly through the local registry, only warming Redis separately, while preserving base freshness, the fallback build path, pod proofs, and final image publication. The registry now stays alive until publication completes and is torn down in an always-run cleanup step, closing a premature-cleanup regression caught by new tests; remaining work is confirming green full-runtime results across a few more integrated runs before closing the issue.

## v2.17.770 · 2026-11-09

### 2026-11-09 · 6b19680cbc60 · #10281 · @nestorcolt

**Fill a hive invite's bus address from the hub signer record**

When a hive invite is minted with the hub's NATS signer but no explicit `--bus-url` or `--like` address, it now looks up the signer record before minting and uses its published `url` to fill `AGENTIHOOKS_BUS_URL`. An explicit `--bus-url` flag or a `--like` reference still takes priority. This closes the gap where such invites carried no bus address at all, forcing a manual rejoin using internal service names; now a rejoined hive connects straight to the published NATS address with no hand setup, matching the stated project goal.

## v2.17.768 · 2026-11-09

### 2026-11-09 · 79a5cbd3c5a0 · #10256 · @agenticore-app

**Guard controller quota fallback from pod local cache regressions**

This change prevents a fresh controller, which starts with no pass quota rows or local observations, from mistakenly falling back to local capacity caches in HiveLaunch, rotation, and controller setup, ensuring it relies only on its runtime quota reader or Redis hive reports. It adds a Semgrep incident rule with red/clean fixtures to catch such fallback references, registers new placement tests for deployment readers and Redis reports, and fixes the fixture check by running it outside an ignored folder so it actually executes, closing a gap where regressions could silently slip through.

## v2.17.767 · 2026-11-09

### 2026-11-09 · 6279ae288465 · #10251 · @agenticore-app

**Delete the unused assign and taken options of the hive machine name map**

This change removes the unused `assign` and `taken` options from the hive node-to-name map function (`machine.name`), along with the tests that only existed to exercise them. Call-site review confirmed `hive add`, the sole production caller, passes neither option, and `hive create` names machines through a different path entirely, so these options were dead code. The two `taken` tests were rewritten to cover real surviving behavior instead, keeping coverage meaningful while trimming code that no longer serves any caller.

## v2.17.765 · 2026-11-09

### 2026-11-09 · 74dba094031e · #10245 · @agenticore-app

**Hold every tick scenario step to its floor and place from a fresh controller**

This change strengthens the tick CI gate so regressions caught in production incidents cannot silently slip back in: every scenario step is now checked against its protected base floor and flagged if a pass abandons it, and a fresh-controller-with-empty-quota-cache scenario verifies placement still works from a clean cache. The existing ten-dead-seat hive-roll scenario is replayed and must complete within two passes. Wall-time records are uploaded per scenario for visibility, and workflow tests enforce check ordering, protected-grading, and separate artifact directories. Proven with matching red/green CI runs per the task's planted-fault requirement.

## v2.17.764 · 2026-11-09

### 2026-11-09 · 628324ad6ec6 · #10262 · @nestorcolt

**Remove gate declaration expiry and preserve admin recovery**

This change drops the expiry field and date validation from gate wiring declarations, since expiry rules were causing false failures on missing, past, or malformed dates without adding real safety. Admin recovery is preserved by explicitly allowing RepositoryRole 5 via pull requests, matching the payload from #10260. New regression tests cover wiring validation and protected grading, plus a Semgrep rule flags any reintroduced expiry keys. No alerting or renewal automation is added, simplifying gate declarations while keeping recovery access intact.

## v2.17.762 · 2026-11-09

### 2026-11-09 · 73508edcf249 · #10260 · @nestorcolt

**Announce the dev history admin recovery payload**

This change announces the upcoming dev-history admin recovery payload, which grants RepositoryRole 5 a bypass in pull_request mode, ahead of actually enforcing the frozen ruleset that depends on it. By registering the new payload now while compatibility tests still accept both the current and announced versions, it lets CI catch regressions early without breaking existing runs. The actual removal of gate declaration expiry and activation of the admin recovery path is deferred to a follow-up PR, keeping this step low-risk and reversible, limited to CI.

## v2.17.761 · 2026-11-09

### 2026-11-09 · 9d2473b750dc · #10214 · @agenticore-app

**Show hive admission room and the binding limit in swarm status and autoscale**

This change makes `swarm status` and `swarm autoscale` report, per live hive, its admission room alongside quota seats and host room, and explicitly names whichever of the three is currently binding and blocking spawns; when every hive is full it instead names hive admission plus each hive's specific refusing limit. This closes the gap where autoscale and status showed quota seats and host room as free while hive cpu room or the shared six-slot workstation cap silently refused every spawn, so operators can now see at a glance why urgent tasks are stuck waiting instead of assuming capacity is available.

## v2.17.757 · 2026-11-08

### 2026-11-08 · b046f80e8d0d · #10239 · @nestorcolt

**Stop the chart release from stamping a per-release digest into worker hives**

This change reverts the part of #10198 that had the chart-release workflow stamp a fresh image digest into the worker hive pod template on every release build. Because releases happen roughly every 20 minutes, that stamping was quietly rolling both worker hives each time, killing every in-progress agent on them and degrading launches for over an hour on 2026-11-08. The `agentihooks.io/release` annotation now only changes when an operator explicitly sets `workers.rollout.release`, restoring worker hives to their intended operator-controlled roll mechanism while keeping the rest of #10198 intact.

### 2026-11-08 · 66066c7ed1dd · #10150 · @agenticore-app

**Settle retirement after a hive stops acknowledging**

This change fixes a bug where retire orders could get stuck forever on a hive whose agent had stopped responding, leaving seats falsely marked as running even after the underlying worker was long gone. Now HerdrRuntime.retire checks for bounded heartbeat loss or newer hive membership evidence to recognize when the remote agent is actually gone, clears the stale refusal, and settles the seat as retired, while still retaining the normal retire-and-wait behavior for hives that are alive and acknowledging. This closes a real production issue where seats stayed occupied after pod rollouts, without ever force-killing a live agent.

## v2.17.755 · 2026-11-08

### 2026-11-08 · 2e2dcf3f1b64 · #10187 · @agenticore-app

**Reuse qualified worker layers across release tags**

This change lets release-tag builds reuse the already-qualified worker image layers from the development branch instead of starting cold, since tagged builds can't access the dev branch's Actions cache. It does this by pulling the dev image from the registry as a cache source and baking in inline cache metadata so future builds can chain off it too. Local caching and all existing qualification and publication checks stay intact, so the worst case if the registry cache is missing is simply a normal full rebuild, not a broken release.

### 2026-11-08 · f47b56578c27 · #10131 · @nestorcolt

**Compute each hive's agent ceiling from its own resources unless capped**

Each hive's maximum agent count is no longer a fixed default like twenty or ten; instead it's recalculated every tick from that hive's own reported memory, swap and load, using the same host room rule the workstation autoscaler already applies. If the operator sets an explicit cap via the hive command or the Hives panel, that cap bounds the computed ceiling from above; otherwise placement uses the live, resource-derived number. The Hives panel's max agents cell now shows this computed ceiling and flags when a cap is in effect, so capacity tracks actual hardware headroom rather than a guess.

## v2.17.753 · 2026-11-08

### 2026-11-08 · efceb0164343 · #10198 · @nestorcolt

**Roll a release tag hive worker each time the release image moves**

The release chart build now resolves the digest behind the floating `agentihooks-hive-worker:release` tag and stamps it into the packaged chart's `workers.rollout.imageDigest`, without touching source values. This gives hive worker pods running that tag a template label that changes on every release, so ArgoCD actually rolls them through the existing PreSync lease and `hive drain --wait` preStop hook instead of sitting on a stale pod until someone manually restarts it mid-task. Pinned semver or `dev` tags, and explicit rollout overrides, are unaffected, so only swarm-hive-001 and swarm-hive-003 gain automatic, graceful rollout on release moves.

## v2.17.752 · 2026-11-08

### 2026-11-08 · 91969b1713ec · #10200 · @nestorcolt

**Controller reads account quota from the hive reports after a restart**

After a controller restart, hive placements were stalling with waits like "sessions of None" because the fallback path read the pod-local balancer cache, which starts empty and reports every account as UNKNOWN. This fix makes the fallback and rotation logic read quota directly from the hive reports stored in Redis via capacity.hive_accounts, falling back only when no runtime quota reader is set, so controllers immediately see fresh account quotas after a roll instead of waiting on a cold local cache. The wait message is also renamed to "no fresh quota reading" for clarity.

## v2.17.751 · 2026-11-08

### 2026-11-08 · 3be08c9c40f7 · #10160 · @agenticore-app

**Spawn step acts through the runtime and coordination ports**

This change routes the spawn step's interactions with runtime and swarm-store state through the new Runtime and Coordination ports instead of touching raw tools directly, so placement, reaping, admission, and store reads/writes all go through tracked, effect-gated operations with a shared recording deadline. It also moves inbox cleanup for dropped spawn claims onto a new `Inbox.settle_exit` port call. The result is that the spawn pass and its helpers no longer hold raw store or runtime references, making their side effects observable, testable with fakes, and consistent with the rest of the tick pipeline's port discipline.

## v2.17.750 · 2026-11-08

### 2026-11-08 · f26a1b5d41d1 · #10192 · @agenticore-app

**Events admission skips a hive that is stopping for a release**

This change fixes a bug where the hub would keep assigning new task seats to a hive that was already draining in preparation for a release, because the admission check never consulted the hive's drain flag before claiming and seating a task. As a result, the same seat was handed to a departing hive three times in a row, forcing immediate handoffs each time. Now `Admission._placeable` checks the registry's drain status upfront and refuses placement on a draining hive before any claim, execution, seat, or ledger write occurs, preventing wasted work and churn during releases.

## v2.17.748 · 2026-11-08

### 2026-11-08 · 9fe0d9519892 · #10195 · @agenticore-app

**Hive create waits only on required checks and the merge path of its manifests pull request**

Hive create's merge step now checks only the status checks the base branch ruleset actually requires before proceeding, so an optional failing check (like Gate — Protected) no longer blocks a create unnecessarily. Once required checks pass, it follows whatever merge path the base branch expects: enqueuing via the `enqueuePullRequest` GraphQL mutation for merge-queue bases (without re-enqueuing an already-queued PR on rerun), or using auto merge when allowed and falling back to squash merge otherwise. Reruns after a timeout correctly resume the same saved pull request and hive member instead of starting over.

## v2.17.747 · 2026-11-08

### 2026-11-08 · fe9d758d1ef4 · #10162 · @agenticore-app

**Record the runner pod kill reason in the lost runner watchdog report**

This change makes the lost runner watchdog read each runner pod's container termination in cluster mode and append the actual kill reason, such as "OOMKilled exit 137", to the report it logs and records when cancelling or rerunning a stalled run. Previously these reports only noted that a runner was lost, with no indication why, which caused a memory-killed job to be misread as a timeout for a full day. The hosted Actions watchdog, which has no pod access, is unaffected since it relies on the organisation runner list, which carries no termination data.

### 2026-11-08 · 01108f73573b · #10164 · @agenticore-app

**Hive create prints each finished step with its time as it happens**

This change fixes hive create so that each step's completion line is written and flushed immediately with its UTC timestamp, instead of sitting buffered in stdout until the process exits. Previously, when stdout was piped, drill timing tables recorded when output happened to arrive rather than when each step actually finished, making timing data unreliable. The fix introduces a stamped, auto-flushing reporter as the default output handler for hive create, verified by a test that pipes stdout through stub steps and confirms each line appears before the next step runs, ensuring accurate, real-time timing visibility.

## v2.17.744 · 2026-11-08

### 2026-11-08 · 19249e2e7bb0 · #10142 · @agenticore-app

**Skip hives that begin draining before launch**

This change fixes a race where a cached hive selection could still route a launch to a hive that had already started draining due to removal or a pod preStop hook, causing orders to land on unavailable targets. Admission now rereads the hive registry right before reserving room or publishing an order, so draining or removed hives are caught and routed through the existing retry loop, which picks another eligible hive or waits instead. This closes issue #10140 and prevents orders from being sent to hives that can no longer accept them, improving placement reliability.

## v2.17.742 · 2026-11-08

### 2026-11-08 · 44c9796ca964 · #10170 · @agenticore-app

**Budget 850 MB per agent in hive admission from the measured hive three peak**

Hive admission's per-agent memory budget (`scripts.hive.admission.AGENT_MB`) rises from 700 MB to 850 MB, matching the measured peak from hive three (16503 MB pod peak across 20 agents, ≈825 MB/agent, rounded up, and corroborated by a 756 MB CI mean). The previous 700 MB figure sat below observed memory use, risking out-of-memory failures as a hive fills toward its 48-agent max. The host room default in `scripts.swarm.host_budget` stays untouched since it already takes the larger of measured agent trees or its own configured value. Tests were updated to expect 850 MB while preserving each case's room count.

### 2026-11-08 · 17d6f6597de9 · #10166 · @agenticore-app

**Hive release rechecks the newest release right before moving the release tag**

This fixes a race where overlapping Hive releases could stomp on each other: previously the job checked if it was the newest release early on, before images built, so a slower older release could finish last and yank the `:release` tag back onto stale code. Now `release-hive.sh` re-reads the newest stable version tag from the remote right before actually moving `:release`, skipping the move if a newer release has since landed, and fails loudly if that check can't be performed. The version tag is still always pushed. Tests cover newer/older releases, unreachable remotes, and prerelease tags being ignored.

## v2.17.741 · 2026-11-08

### 2026-11-08 · 99931e716c95 · #10158 · @agenticore-app

**Reuse stable worker hive build layers in the kind proof**

This change updates the kind proof so the worker hive image build reuses cached layers across ephemeral runners instead of rebuilding from scratch each time, while still checking base image freshness and falling back to a full rebuild when needed. It keeps the complete kind pod proof and publication path intact. This matters because cold builds were taking up to 270 seconds with no layer reuse, and consistently restoring stable layers should cut build time and make CI runs faster and more predictable, pending three after-push samples to confirm the new median.

## v2.17.740 · 2026-11-08

### 2026-11-08 · 7e1b8c4b82ba · #10134 · @nestorcolt

**Answer hive refresh store refusals with a fixed reply and log the Redis text**

When the hive refresh endpoint hits a store refusal from Redis, it previously echoed the raw Redis error text straight back in the API reply, leaking internal detail to callers. Now it answers with a fixed message naming what was refused (e.g. "Redis refused the refreshed member"), while the full Redis error text is written to server stderr for operator diagnosis. This is implemented via a new `auth.StoreRefused` HiveError carrying both the fixed reply and the full message, with the server route answering the fixed reply and logging the detail. A test covers the behavior.

## v2.17.736 · 2026-11-08

### 2026-11-08 · cbd9f747d01c · #10122 · @agenticore-app

**Print the failed worker build log tail in the smoke job log**

This change makes smoke.sh print the last 40 lines of the Docker build log to stderr whenever a `docker build` step fails, instead of just reporting the exit code. It directly addresses the dev outage where the job log showed only a generic failure line while the real cause, an HTTP 504 during tool downloads, was buried in an uploaded artifact. With the log tail surfaced in the job output itself, future transient or upstream failures in the worker image rebuild proof will be diagnosable immediately, without needing to dig through separate build log uploads.

### 2026-11-08 · c032d047edb5 · #10145 · @nestorcolt

**Reconnect every shared NATS bus after a drop and log lifecycle events**

This change fixes a shared NATS bus bug where connections, after their initial bounded connect succeeded, would never reconnect once dropped, leaving controllers, hubs or webhooks silently running without the bus until manually restarted. Now, after the first successful connect, the bus enables unlimited automatic reconnection, restores subscriptions, and logs disconnect and reconnect events through the scripts.events.nats logger, so a bus outage self-heals and is observable without requiring a pod restart. The original fast-fail behavior for an unreachable bus on first connect is preserved.

## v2.17.735 · 2026-11-08

### 2026-11-08 · e095530d8f39 · #10102 · @agenticore-app

**Stop the spawn pass at its pass deadline and route it through the ledger port**

This change makes the spawn pass write all ledger updates and comments through its declared ledger port rather than touching storage directly, and enforces its pass deadline strictly: once time runs out, it stops preparing new tasks, settles whatever batch it already prepared, and hands off the remaining work to the next pass instead of leaving unresolved writes behind. This closes a gap where expired passes could still perform risky writes, and keeps ledger access cleanly scoped as part of the broader tc-h15b split separating coordination, inbox, and ledger responsibilities.

## v2.17.734 · 2026-11-08

### 2026-11-08 · 87f8576b7e49 · #10126 · @agenticore-app

**Retire the workstation tailnet hive join front**

This change removes the `hive-tailnet` service from the workstation tailnet overlay, since all Anton hives have joined through the hosted endpoint since #5518, leaving the workstation front unused. `start-tailnet.sh` now recreates only the ledger and Redis fronts with `--remove-orphans`, so the next run cleans up the retired join container automatically. Tests and docs were updated accordingly. This closes off the old workstation Redis as a path for minting hive members, reducing attack surface and operational complexity now that joins flow exclusively through the hosted endpoint.

## v2.17.733 · 2026-11-08

### 2026-11-08 · 2d484dfd5cf1 · #10105 · @agenticore-app

**Document agent cards: the bundle catalogue replaces the registry**

This change updates the documentation to reflect that the bundle catalogue of profile cards (using `description` and `capabilities`) now replaces the old agentibridge agent registry, as outlined in Swarm-v2 section 9.2. It adds a new reference page covering agent cards, the `agentihooks agents` commands, MCP tools (`agent_list`, `agent_show`, `agent_run`), workflow files, and plan normalizer behavior, plus corresponding toolbelt rule rows. This matters because it gives operators and engineers accurate, discoverable documentation for the new agent-card system as it lands alongside the MCP `agents` category work, keeping docs in sync with the actual implementation and passing the docs build check.

## v2.17.730 · 2026-11-08

### 2026-11-08 · fa42cf6f2616 · #10090 · @nestorcolt

**Serve agents over MCP: list, show and a one-stage run**

This change exposes agents over MCP so external tools can discover and invoke them without shelling out to the CLI. `agent_list` and `agent_show` mirror the existing `agents list`/`show` profile cards, while `agent_run` drives a one-stage workflow for a given profile and input, either waiting for the result or detaching a background `workflow resume` process and returning the running state immediately. To keep things safe, `agent_run` refuses to start inside an existing worker run, preventing nested executions. The registry test now names the new `agents` category, completing the planned MCP slice.

### 2026-11-08 · 14a535038561 · #10083 · @agenticore-app

**Accept operator plan authorship, record plan source and quiet plan relays**

This change lets the operator credential upload plan artifacts specifically, while every other operator artifact type remains refused, closing a gap in authorship control. It also lets plan_add carry an optional source record—harness, session, path, sha256—so you can trace where a plan actually came from, with validation enforced in the ledger and API schemas. Finally, operator_mail.relay now skips operator "added" events on plans and slices, preventing a flood of mail to the master every time an operator ingests a plan.

## v2.17.729 · 2026-11-08

### 2026-11-08 · 70b6a9438eff · #10081 · @agenticore-app

**Run filtered workflow stages only through agentihooks judge**

Workflow stages that declare judge filters now bypass headless execution entirely and run exclusively through `agentihooks judge`: the new `Judge` launcher writes the case and gathered evidence into a fresh attempt folder, invokes `agentihooks judge` with the case, evidence, tier, and harness, and treats the resulting verdict.json as the stage outcome, retrying on nonzero exit or timeout. `HeadlessProfile` now refuses any filtered stage, and a new `Routed` launcher dispatches filtered stages to `Judge` and everything else to `HeadlessProfile`, with `workflow` execution switched over to it. This closes a gap where filtered stages could silently skip judging.

## v2.17.728 · 2026-11-08

### 2026-11-08 · 0eae1886de77 · #9987 · @nestorcolt

**Type the first half of the top level scripts files**

This change adds full type annotations to the first alphabetical half of the top-level `scripts` directory (60 files, `__init__.py` through `codex_refresh.py`), bringing them to zero strict basedpyright errors except for intentionally-baselined private-usage warnings. It closes #9457 and only touches types—annotations, casts, and TYPE_CHECKING imports—with no logic changes, verified by syntax-tree comparison against the base. Two new tests catch real mutation survivors introduced by the stricter typing, and some function parameters were widened so existing test fakes still satisfy type checks, improving long-term maintainability and catching latent bugs without altering runtime behavior.

## v2.17.725 · 2026-11-08

### 2026-11-08 · be13269b2622 · #9976 · @agenticore-app

**Type the profiles package for the strict checker**

This change adds type annotations throughout every module in the scripts/profiles package so it passes the strict basedpyright checker, reducing strict errors from 813 to just 29 remaining items tied to other untyped modules and a few deprecated client calls. No logic was altered—only annotations, casts, and type-checking imports were added, verified by comparing the annotation-free syntax tree against the current dev branch. This strengthens static type safety across the profiles package, making future bugs easier to catch automatically and clearing the path toward full strict-mode coverage for the project.

## v2.17.723 · 2026-11-08

### 2026-11-08 · 751c9cf7af31 · #10061 · @agenticore-app

**Count a profile a placement target hive advertises as installed**

This change fixes profile placement so a bundle profile is treated as installed on a candidate hive as soon as that hive advertises it, and the controller now only places a profile on hives that actually hold it, with configuration snapshots covering both linked and bundle profiles. This matters because previously the controller could pick an incompatible hive for a given profile, risking misplaced workloads; the fix closes that gap and ships with new regression tests and a clean mutation/test run, keeping placement decisions reliable going forward. It is a two-way change, reversible via a follow-up PR if issues surface.

### 2026-11-08 · eccec2438af5 · #10072 · @nestorcolt

**Keep the chart version out of pod templates so a release rolls no pod that did not change**

This change splits chart labeling so pod templates use a new `swarm.podLabels` helper (name, instance, managed-by, component only) while `helm.sh/chart` stays on object metadata via `swarm.labels`; selectors are untouched. A CI check (`tests/ci_charts/chart_pod_templates.py`, run in the semgrep charts job) now renders every chart at its current and a bumped version and fails if any pod template differs, catching regressions like the one observed where hive revisions 45–49 rolled pods solely because the chart version string changed. The first release with this fix will roll every workload once; afterwards, chart-version-only bumps roll nothing.

## v2.17.722 · 2026-11-08

### 2026-11-08 · d8b8fe71d290 · #10056 · @nestorcolt

**Autoscale preview mirrors the tick: placement refusals and the tick host room**

This fixes a mismatch between the autoscale preview and the actual scaling tick: the preview was projecting from stale, mismatched inputs (all ready tasks ignoring fresh placement refusals, and the wrong host room sample) rather than what the tick itself uses, making it look stuck at "pending raise held" when the real tick was already raising seats correctly. The drifting-target theory was disproven by a new regression test. Now the preview subtracts fresh placement refusals from demand and uses the host room the tick last granted, so the preview accurately mirrors real autoscale behavior.

## v2.17.719 · 2026-11-08

### 2026-11-07 · d700cb581cda · #10009 · @agenticore-app

**Profiles connectors drop deprecated MCP client calls and import only typed functions**

This change updates the profiles connectors code to use the current MCP client instead of the deprecated wrapper, listing tools via `streamable_http_client` with explicit `httpx.AsyncClient` headers, timeouts, and paginated cursor requests. It also adds type annotations to eight previously untyped functions imported across the profiles modules and lowers the type baseline file to reflect the reduced error counts. This removes reliance on soon-to-be-removed MCP APIs, improves type safety and tooling confidence, and keeps the type gate green without changing runtime behavior.

## v2.17.716 · 2026-11-07

### 2026-11-07 · 89100789a438 · #10013 · @nestorcolt

**Announce the reuse gate blob that carries its strict type annotations (Refs #9457)**

This change registers the exact blob hash of scripts/ci_reuse.py from PR #9987 in the frozen gate registry, allowing the strict typing PR for the top-level scripts (Refs #9457) to pass the protected reuse gate. The registered blob only adds type annotations and casts; stripping those out leaves its syntax tree identical to the current dev version, so the gate can safely recognize it as an allowed variant without altering behavior or requiring changes to #9987 itself.

### 2026-11-07 · 7349a524f3e2 · #9966 · @agenticore-app

**Grade the feature map only through the protected validator**

This change closes a gap where the Semgrep feature-tests job graded the feature map using its own head copy of the validator, letting a pull request that loosened that validator pass its own check. Since the Tests lint job already grades the feature map correctly using the protected validator checked out from the base branch, the redundant and insecure head step is removed from Semgrep. A new test confirms no Semgrep job runs the validator directly, and a both-ways proof verifies that a pull request tampering with the validator or planting a duplicate feature map entry is still caught.

## v2.17.713 · 2026-11-07

### 2026-11-07 · b65c0635ce60 · #10011 · @agenticore-app

**Read app forbidden check rollups as unchecked in the open listing**

This change fixes the fleet red hold, which was silently skipping every pass because GitHub's App token can't read statusCheckRollup on most open antoncore pull requests, causing GraphQL to return FORBIDDEN errors that crashed the listing entirely. Now, GitHub_SourceControl.open_changes treats FORBIDDEN-only rollup errors as "no checks" for that pull request (failing open, by design, since missing checks already means unseen) while still raising on any other error type. The skip line also now includes gh's stderr for easier debugging. Granting the App proper read access remains a separate follow-up.

## v2.17.712 · 2026-11-07

### 2026-11-07 · 347f23ec112a · #10048 · @agenticore-app

**End unschedulable worker executions and place fresh restores through hives**

This change fixes a bug where retried worker pods that no node could schedule kept occupying every worker quota slot, starving new hive launches. Now, unschedulable pods are detected and their executions ended after five minutes, their owned tasks reopened, and capacity accounting excludes stuck Pending pods from pool and quota usage. Fresh restores and node_lost retries route through hive placement rather than relaunching directly on the original backend, and cancellation handling correctly resumes after interruptions. This prevents worker starvation and restores correct scheduling behavior.

## v2.17.711 · 2026-11-07

### 2026-11-07 · 2e6ddcf016e8 · #10052 · @nestorcolt

**Remove network zsh setup from unit shards**

This change removes the apt-based zsh installation step and the ZSH_REQUIRED flag from unit test shards, letting tests run with whatever shells are already on the runner. It was prompted by shards 8 and 10 timing out during zsh setup before tests could even start. Since the existing zsh-specific test already skips gracefully when the binary is missing, dropping the setup step is safe and eliminates install timeouts, confirmed by all 16 unit shards passing cleanly in the follow-up CI run.

## v2.17.710 · 2026-11-07

### 2026-11-07 · 07d40ab0fa3e · #9995 · @nestorcolt

**Serve the tick ledger in process from the shared database when its URL is set**

When the shared database URL is set, a tick pass now reads and writes the ledger directly in process through the shared ledger repository instead of calling the ledger server over HTTP, so a slow or unreachable ledger server no longer raises LedgerUnreachable and kills the controller. The new DatabaseResourceClient runs the server's own route handlers locally as the trusted service caller, keeping epoch fencing, author refusal, worker write checks and the write gate intact. HTTP remains the fallback when the database URL is absent; the controller pod still needs that URL wired in before this takes effect there.

### 2026-11-07 · bd94265c2d6f · #10041 · @agenticore-app

**Release the tick pass lock even when closing the pass raises**

This change fixes a bug where the tick pass lock could leak if an error occurred while closing a pass, leaving later passes permanently refused with "another tick is running." The lock release is now guaranteed to run even when the close logic raises, so a failing shutdown no longer strands the lock behind a dead holder. A regression test confirms the lock is cleared after a pass whose closing stack throws, directly addressing the 7 November incident where a stuck lock blocked ticks for about 13 minutes until the pod was deleted.

## v2.17.708 · 2026-11-07

### 2026-11-07 · 8c6a18ef976b · #9984 · @nestorcolt

**Refuse a CPU saturated preferred hive at placement**

Placement was choosing a preferred hive purely by its memory based "first room" estimate, ignoring real CPU and pod limits, so a CPU saturated hive kept winning eng and ci placements even while idle hives sat empty. This change makes the first room check honor host and pod CPU limits the same way the real room does, so a saturated preferred hive now fails the room check and placement correctly spills to the next available hive, matching how load actually looks in the registry.

## v2.17.706 · 2026-11-07

### 2026-11-07 · 236181ac7ab9 · #9967 · @agenticore-app

**Share one ledger part list between the export schema and the tick view**

This change fixes a duplication risk where the ledger export schema and the tick ledger view each kept their own hardcoded list of ledger parts, which could silently drift out of sync as new parts were added. Both now read a single shared constant, LEDGER_PARTS, defined once in the schemas module, and a new seam test confirms they reference the same list object. This closes issue #9446 and ensures future ledger part additions stay consistent across export and tick views automatically.

## v2.17.704 · 2026-11-07

### 2026-11-07 · 4572e84f89a5 · #10007 · @agenticore-app

**Spawn moves to an account with room when the chosen account's reservation is full**

When a hive spawn is refused because its chosen account's reservation is full, the system now releases that hive's admission and local slot and retries placement with the refused accounts excluded, rather than aborting the whole spawn attempt. This lets `HiveLaunch.spawn` fall through to another account with available room in the same pass, with `_select` filtering out refused accounts and `_reselect` keeping the pass cache accurate so retries aren't double-charged across hives. If every account is full, the last refusal is preserved and admission is cleanly released, avoiding stuck reservations.

## v2.17.702 · 2026-11-07

### 2026-11-07 · 14c9069428ef · #9993 · @nestorcolt

**Hold the controller lease through the shared database, never the ledger server**

The controller's epoch lease previously went through the ledger HTTP client for acquire, current, transfer, renew, release and fenced checks, which meant a ledger server timeout could throw SwarmError during reap, as happened on 7 November. Since the ledger only proxies the same shared swarm state the controller already writes to, the lease now talks directly to the shared database when AGENTIHOOKS_SWARM_DATABASE_URL is set, bypassing the ledger entirely. This removes a needless dependency on the ledger server's availability for core controller operation, making reap and lease handling resilient to ledger outages.

## v2.17.701 · 2026-11-07

### 2026-11-07 · 64def43b5c0f · #9391 · @agenticore-app

**Publish controller recovery over TLS for callers outside the cluster**

The controller previously advertised only its in-cluster plain-http address for recovery callbacks, which hive pods couldn't reach past the network policy and the workstation couldn't resolve, breaking restore decisions with "controller could not be reached" errors. This change adds a second TLS listener on the controller serving just the recovery routes, publishes that address as swarm config `control_url`, and has restore decisions prefer and verify it against the ledger CA. The chart gains matching TLS config (secret, port, public URL, `ingressFrom`) and now fails fast on a missing `ingressFrom`, so recovery commands work correctly from both hives and the workstation.

## v2.17.700 · 2026-11-07

### 2026-11-07 · fc1e8bd4a01c · #10003 · @agenticore-app

**Bound the hub runner's first NATS connect; reconnect forever only after one success**

Previously the hub runner's NatsBus enabled reconnect with max_reconnect_attempts=-1, which nats-py also applied to the initial connect, so with NATS down `agentihooks hub run` would retry its first connection forever instead of failing clearly. Now the first connect is bounded to 5 attempts and raises a clear ConnectionError if no server answers; only after a successful first connect does the client's reconnect limit switch to unlimited, preserving the old forever-reconnect behavior for later disconnects. This gives operators a prompt, legible failure when NATS is unreachable at startup instead of a silent hang.

## v2.17.699 · 2026-11-07

### 2026-11-07 · c19e2e56473d · #9962 · @agenticore-app

**Admit the dispatcher seat ahead of lane launches**

This change fixes a bug where a freed hive slot was always claimed by a waiting lane task instead of a pending dispatcher spawn, because the dispatch seat check ran after lane placement in the tick pass. Moving the dispatch seat site earlier, alongside the master seats, ensures a pending dispatcher trigger gets priority for the next free slot ahead of lane launches. This matters because dispatcher spawns were silently being starved whenever a lane task was also competing for the same freed slot, delaying dispatch work indefinitely.

## v2.17.697 · 2026-11-07

### 2026-11-07 · 7f3751e66f2b · #9971 · @agenticore-app

**Controller survives a ledger read timeout instead of exiting**

Previously, if a controller pass hit a ledger read timeout (LedgerUnreachable), the whole process exited with code 1, costing a pass and forcing a pod restart. Now the run loop catches that error, logs it, backs off for one tick, and starts the next pass instead of exiting, so the controller stays alive through transient ledger hiccups. A `--once` invocation still reports failure via exit code 1. This should keep restart counts flat during ledger slowness, matching the swarm's resilience goals.

## v2.17.696 · 2026-11-07

### 2026-11-07 · 99108c3d7454 · #9960 · @agenticore-app

**Exchange the local operator credential in the auth service outside edge mode**

This change lets the auth service's token exchange, when running outside Cloudflare Access edge mode, verify the ledger's own stored page credential instead of requiring an edge assertion. That means the workstation and the Docker Compose developer build can now obtain a signed operator token directly from the auth service without going through Cloudflare Access. Inside edge mode nothing changes, since the page credential is never read there. Tests confirm correct exchanges succeed, wrong or missing credentials are refused, and edge mode behavior is untouched, closing a gap that previously blocked local/developer operator authentication.

## v2.17.695 · 2026-11-07

### 2026-11-07 · 16a03370e50c · #9957 · @agenticore-app

**Restore feature suite and swarm smoke images from the Actions cache**

This change caches the Redis, Postgres, and swarm runtime images (Python base, NATS) in GitHub Actions cache so CI stops repeatedly pulling them from public registries, which had been failing with "toomanyrequests" rate-limit errors and breaking both the feature suite and promotion runs. On a cache miss it pulls once, saves the images, and loads them locally thereafter with `--pull never` or `PULL_REUSE_LOCAL`; a failed load falls back to pulling. This makes CI resilient to registry throttling, at the cost of needing to bump the cache key version if a saved archive ever becomes corrupt.

## v2.17.694 · 2026-11-07

### 2026-11-07 · 58f463bd4497 · #9900 · @agenticore-app

**Move the prepare function into the place step module**

This change relocates `_prepare` from `scripts.swarm.tick` into `scripts.tick.steps.place`, next to `_spawn`, its sole caller, continuing the tick sweep's module reorganization. The place module now reads tick names via `tick.NAME` so existing patches on `tick.primed`, `tick._room_wait`, `tick._release_admission`, `tick._not_launched`, and `tick._prepare` still work. `scripts.swarm.tick` re-exports `_prepare` and drops imports it no longer needs, while allowlists, type baselines, and tests are updated to reflect the move. This keeps related logic colocated, trims unused imports, and maintains full backward compatibility for tests and callers.

### 2026-11-07 · dac3441ae969 · #9804 · @nestorcolt

**Document the shared tick helpers in the common step module**

This change adds documentation for the shared tick helpers introduced in PR #9708, covering all eleven helpers in `scripts.tick.steps.common`, the re-export in `scripts.swarm.tick`, the late tick import, and the reasoning behind test patching of the tick module, plus guidance for relocating additional helpers through the allowlist and feature map. It also adds a tests reference row, two troubleshooting entries, updates the index to name the new section, and includes a changelog fragment. This closes the documentation gap left after Sweep one of the tick development pattern work, ensuring contributors understand and correctly use the shared helper layer going forward.

### 2026-11-07 · 32f41c8aaab3 · #9841 · @nestorcolt

**Document the tick step scaffold**

This change adds the reference page docs/reference/tick-step-scaffold.md for the `python -m scripts.tick.new_step` scaffold delivered in #9788, documenting the five generated artefacts, the `MODULES` registry, every CLI option with its exact example output, `--check`, refusal cases, limits, tests, and troubleshooting, with every quoted output verified against a real run. It's linked from the reference index, the tick pattern gate page, and the tick engine steps page, so anyone adding or changing a tick step now has one authoritative, verified source describing what the scaffold does and why its outputs look the way they do.

## v2.17.693 · 2026-11-07

### 2026-11-07 · cd76f171c962 · #9941 · @agenticore-app

**Key the herdr runtime name reap on the execution as well as the name**

This fixes a bug where reaping a remote name could be silently dropped because the hive command key was based only on the name, so a stale receipt from an earlier reap of that same name would incorrectly answer a later reap tied to a new execution. HerdrRuntime.reap_name now includes the execution id and generation, via the new Reports.executions_of, in the order key so reaps are correctly scoped per execution, not just per name. A seam test covers this case. A known low-severity gap remains for names the hive lists without an execution entry, tracked separately.

## v2.17.689 · 2026-11-07

### 2026-11-07 · 0e4e93d1fcae · #9862 · @agenticore-app

**Document the agentihooks headless run command**

This change adds reference documentation for the `agentihooks headless run` command introduced in #7684, covering usage, flags, live examples for each harness, JSON report format, exit codes, internals (harness selection, run folder layout, `select-profile` launch, worker mode), model tiers with env overrides, and known limitations and troubleshooting steps. It also links the new page from the reference index and CLI commands list, and adds a changelog entry. This closes the documentation gap so operators running full profiles headlessly have an accurate, verified reference instead of relying on source code alone.

## v2.17.688 · 2026-11-07

### 2026-11-07 · 7bc0366b1925 · #9869 · @agenticore-app

**Move the claim order and launch records into the claims step module**

This change relocates the claim ordering logic and launch record types out of the monolithic tick module into a dedicated `scripts.tick.steps.claims` module, with `scripts.swarm.tick` re-exporting everything so existing patches and seams keep working. It was split off from a larger sweep because the mutation gate rejected the full change as too large for one review. The move shrinks the tick module's type-check surface substantially (1495 to 1274 baseline) and lets the new claims module pass strict checks cleanly, improving maintainability without altering behavior. Follow-up parts (#9891 and others) continue the same decomposition.

## v2.17.687 · 2026-11-07

### 2026-11-07 · 859e0ed7c833 · #9930 · @agenticore-app

**Keep an automatic panic automatic when a fix is added**

This change ensures that when a fix is applied to a panic that was triggered automatically, the panic remains automatic rather than being converted to manual, so the alarm can still release it once the fleet stabilizes; only when a person manually presses panic without supplying a fix does it become manual. It also tightens the engage logic to materialize fixes exactly once, with a seam test using a one-shot generator to catch regressions where fixes read twice would be silently dropped, which previously caused both seam tests to fail before this fix.

## v2.17.686 · 2026-11-07

### 2026-11-07 · c507833b3986 · #9943 · @agenticore-app

**Reap a later expired launch of the same name by its own execution**

This change hardens the hive launch controller so that when a launch expires without acknowledgement and is retired, its reap command is keyed on the specific launch execution rather than just the agent name. Previously, a later relaunch sharing the same name could have its reap command dropped as a duplicate, potentially leaving a stale pane running. Now a controller test confirms that two separate expirations of the same agent name each produce their own reap command in the hive stream, both correctly executed, closing the gap where expired launches could silently fail to be cleaned up.

## v2.17.685 · 2026-11-07

### 2026-11-07 · 517e2667cd96 · #9893 · @agenticore-app

**Move master down, night watch and the group runner into the masters step module**

This change relocates the master-down check, woken detection, night watch, pass-view helper, group runner, action printer, and codex hook ordering out of scripts/swarm/tick.py into scripts/tick/steps/masters.py as part of the ongoing tick rework, continuing the module-splitting effort from prior sweeps. The move is verbatim, with tick still exposing run_group for backward compatibility and patched seams routed through tick.NAME so existing tests keep working. It shrinks the tick module's pattern registry by seven names and reduces its type-checking baseline, while leaving caller modules and masters' own baseline unaffected, and is backed by new seam tests confirming equivalence with the prior behavior.

## v2.17.682 · 2026-11-07

### 2026-11-07 · a325a76a3d95 · #9938 · @agenticore-app

**Ask the master whether a quiet urgent claim is a panic mode call**

This change extends the degradation panic check so it also catches urgent tasks that are claimed but stuck: if a claimant shows no progress past the stale window (default 30 minutes), the master seat now gets a single panic question naming the task, claimant, and quiet minutes, repeated hourly. It reuses one shared quiet-minutes measure across the stale claim check, quiet gate, and panic call, keeping all three consistent. Existing panic signals are unaffected since their hourly timing is unchanged; five new seam tests confirm the behavior end to end.

## v2.17.680 · 2026-11-07

### 2026-11-07 · 42a19a8f0d15 · #9920 · @agenticore-app

**Keep the controller lease through a pass and reissue stale epoch launch orders**

This fixes the stale-epoch launch failures where seats stayed empty: the controller lease was dropping during slow passes because any hosted lease outage (network blip or 5xx) stopped the heartbeat entirely, letting the epoch roll over mid-pass; now only genuine 4xx refusals stop the beat, so transient failures no longer end renewal. Additionally, when the hive refuses an order for a stale epoch, HiveLaunch now reissues it under the pass's current epoch within the same pass instead of repeatedly failing against the old one.

### 2026-11-06 · e661f2277669 · #9891 · @agenticore-app

**Move territory, parked dependencies and the swarm settle into the territory step module**

This continues the sweep splitting the monolithic tick module into focused step modules: territory overlap checks, parked and stacked dependency handling, and swarm settle/close logic are relocated verbatim into a new `scripts.tick.steps.territory` module and registered there, while `scripts.swarm.tick` re-exports the names so existing patched seams keep working. New tests pin the re-exports and behavior directly. The move trims the tick module's type-check baseline from 1495 to 1264 issues and removes 14 names from the allowlist, continuing the gradual, mutation-tested decomposition started in #9869.

## v2.17.678 · 2026-11-06

### 2026-11-06 · c115a32d08c8 · #9931 · @agenticore-app

**Cover the blocker window refusal in the shell guards**

This change restores coverage lost when the master tools change stopped the blocker window from denying the master, which had left the `BlockAction` raise in `_check_shell_guards` untested and failing the coverage ratchet on promotion. It adds a targeted test simulating panic mode holding the fleet, confirming that a non-master session's `git push` is correctly refused with the exact frozen message, ensuring the next promotion run passes and the master outage fixes can release.

### 2026-11-06 · b8fb324b101d · #9924 · @agenticore-app

**Skip the merge stall read in a pod without the checkout**

The hosted controller pod runs without a workstation checkout, which previously caused the merge stall GitHub read to fail every tick with a FileNotFoundError and count as a failed step. This change detects the pod environment via the AGENTIHOOKS_POD_NAME variable set by the controller chart, and when the checkout is missing in that context, records the read as skipped with an explanatory message instead of failing, mirroring how a missing gh binary is already handled. Outside a pod, a missing checkout still fails the step as before, so this only quiets noise from an expected, unfixable condition in hosted deployments.

### 2026-11-06 · 2e7d00b43f5b · #9927 · @agenticore-app

**Lock the master outage fixes against regression**

This change turns the five fixes from the 6 November master outage into permanent, enforced guarantees rather than tests that could quietly be deleted or unmarked. It adds feature-map.json entries linking each regression (master fallback tick, panic tool retention, tick pass fencing, pod controller hive placement, starting master hooks) to its marked pytest tests, adds a semgrep incident rule forcing every subprocess.run in master_fallback to pass timeout= (the cause of the 19:42Z hang), bounds the systemctl calls accordingly, and corrects the docs to state the master keeps every tool during panic while only other sessions freeze.

### 2026-11-06 · 89e576d87748 · #9910 · @nestorcolt

**Keep a starting master whose own hooks beat**

This fixes a false-positive retirement where a healthy, locally-started master was killed because the hosted controller never received a hive report for it—even though the master was actively heartbeating via its own hooks. Previously `master_start.observe` only trusted `runtime.reported()`, so a controller-side reporting gap could wrongly tear down a working master and reopen an outage. Now a heartbeat written by the master's own hooks at or after launch also counts as confirmation of successful startup, so masters running fine on the workstation but invisible to the controller's report pipeline are correctly kept alive instead of retired.

## v2.17.677 · 2026-11-06

### 2026-11-06 · b71beb085c1e · #9913 · @nestorcolt

**Fence the tick pass runtime at its first site**

This change moves where the controller fences its runtime from the "wrapper health" hook to the "wrapper probes" hook, since probes fires first on every tick while health only runs once a slow-lane step is ready. Previously, "spawn due" could start before fencing happened, so spawned tasks used the bare runtime without a controller_epoch, causing every hosted Kubernetes launch to fail with a KeyError and keep the swarm stuck in panic mode. Fencing earlier ensures all spawns carry the proper epoch from the start.

### 2026-11-06 · d2447b90e0a3 · #9912 · @nestorcolt

**Place the login front door master on a hive when the controller runs in a pod**

When the controller runs as a hosted pod, it previously launched the Claude.ai login front door in its own isolated pod, which meant init-agent couldn't find a workstation checkout, leaving masters 0610 and 0611 permanently stuck and never starting up. This change detects the AGENTIHOOKS_POD_NAME environment variable and routes every master seat, including the login front door, to hive placement instead, ensuring it runs alongside a proper checkout and actually comes up reliably in pod deployments.

## v2.17.676 · 2026-11-06

### 2026-11-06 · 6dd5a8cdd900 · #9909 · @nestorcolt

**Replace a finished seat execution in the distributed launch**

This fix updates DistributedLaunch so it passes the previous execution id when replacing a seat whose occupant finished without a launch ending, matching the behavior already used by the hive launch. Previously it only did this for occupants in launch-endings, leaving an empty previous execution id for finished occupants and causing admission to fail with "replacement does not match the current execution." That was stalling controller spawns across t2396, tc-h16, wrn5, wrn6, and wrn7, so aligning the two code paths resolves the spawn stalls while keeping the live-occupant conflict check intact.

## v2.17.675 · 2026-11-06

### 2026-11-06 · f62b9a6fa8f1 · #9898 · @agenticore-app

**Start controller tick passes one tick apart from pass start**

The controller's run loop previously waited a full tick after every pass regardless of how long the pass itself took, causing passes to start a median 109 seconds apart instead of the intended 60. This change makes the wait equal to the tick duration minus the pass's own elapsed time, skipping the wait entirely if the pass already ran longer than a tick, so passes reliably start one tick apart as required for the sixty-pass scale proof.

### 2026-11-06 · 5fb69274e8ca · #9894 · @agenticore-app

**Move the master placement operations into the masters step module**

This change relocates the master placement logic (`_master`, `_recover_master`, `_retire_master`, `respawn_master`, `place_master`, `_under_tick_lock`) from `scripts.swarm.tick` into `scripts.tick.steps.masters`, with behavior unchanged and backward-compatible re-exports left in place so existing callers and test seams keep working. It shrinks the tick module's pattern registry from 76 to 70 names and cuts its type baseline nearly in half, while new tests exercise every branch through the masters module. Tick equivalence runs confirm identical dispatch on clean and faulted scenarios, and review found no standards or spec violations, making the module easier to maintain going forward.

## v2.17.674 · 2026-11-06

### 2026-11-06 · 3bf0db589686 · #9906 · @nestorcolt

**Close the ledger view hold safely when another thread's context entered it**

This fix addresses the crash loop that began after 2.17.673, where the Anton controller restarted 70 times in six hours due to a ValueError when resetting a context variable token created on a different thread. The underlying cause was the fence site hook entering the ledger view hold on a pass executor thread while the exit stack closed it on the main thread. The fix makes holding() fall back to clearing the context variable directly when the token's context doesn't match, so closing a hold entered on another thread no longer raises, restoring normal tick, master, and launch operation.

### 2026-11-06 · 0396b88f8bb5 · #9896 · @agenticore-app

**Close a task in the merge pass once its recorded pull request merges**

This change teaches the slow-lane merge pass to notice when a task's recorded pull request already merged outside the normal flow—for example, when an owner waited on merge instead of releasing through ready—and closes that task automatically, along with its group members, clearing the ready record and settling linked issues. Tasks with unmet proof, failed verdicts, pending values, or splits are left untouched, and split tasks still close via swarm done. This prevents stuck tasks from silently blocking dependents, though same-pass placement is deferred as a follow-up ledger fix.

## v2.17.673 · 2026-11-06

### 2026-11-06 · b9a84b19bc5c · #9904 · @nestorcolt

**Hold every step of the earlier site in the tick site barrier**

This fixes a regression from the earlier barrier-pruning change, which skipped waits for steps or hooks that didn't explicitly declare reads, causing the quiet pass to run immediately after ledger state and crash on missing data before any agents launched. The barrier now again holds every non-slow step of the prior site, regardless of what a pass declares, while still excluding the slow lane via `_late`. The now-unnecessary `hooked` parameter from the previous fix is removed, and tests confirm site edges, undeclared-read steps, hook-dependent sites, and the quiet pass all wait correctly.

## v2.17.672 · 2026-11-06

### 2026-11-06 · 1d79a7a89dc0 · #9902 · @nestorcolt

**Keep the whole site barrier for a site whose hook reads pass values**

This fixes the outage caused by #9839's barrier pruning, which only tracked reads and writes declared by steps, ignoring that site hooks also read values but aren't steps themselves. The relay site's `_judged` hook read `judged` without any step declaring that read, so its barrier got dropped and the hook ran before the intent check finished, breaking every rig-grade-swarm pass since the 07:42 UTC roll. The fix has `registry.site_edges` accept `hooked` sites that keep their full barrier regardless of step declarations, with `sites.run_sites` now passing the gate's hooked sites through.

### 2026-11-06 · ba0c1400886f · #9887 · @agenticore-app

**Stop the reap pass at its deadline and leave the rest for the next pass**

This change makes the reap pass respect its step's context deadline instead of running unbounded: it now stops judging agents once the deadline expires and records an action noting how many agents were left for the next pass. The deadline is passed through from the reap step's run via the registered step call, along with the existing runtime and ledger. This prevents a single reap pass from overrunning its allotted time and ensures leftover work is clearly handed off rather than silently dropped or blocking subsequent passes.

## v2.17.671 · 2026-11-06

### 2026-11-06 · f3203edbbe0a · #9839 · @agenticore-app

**Notice and watch steps on the slow lane, lanes sized for the minute**

This change moves notice and watch steps off the critical path and onto the slow lane, since nothing on the critical path actually depends on them, keeping each pass lane within the one-minute budget (60s critical, 59s slow). To satisfy the registry validator's rule against unordered readers and writers of the same pass domain, several steps' needs were reordered to point at the correct upstream step. site_edges now adds a barrier only where declared reads and writes genuinely cross, and run_tick goldens were regenerated to reflect the steps now running on the slow lane.

## v2.17.669 · 2026-11-06

### 2026-11-06 · db0748fb4973 · #9850 · @nestorcolt

**Read promotion runs by repository name without the workstation checkout**

Promotion watch previously shelled out to `gh api` with `cwd` pinned to the swarm's workstation checkout, so controllers like Anton that have no such checkout failed every pass with "promotion watch skipped, reading dev push runs failed" and filed zero fixer tasks on red runs. The watch now resolves the repository from the swarm's stored origin (`release_wait.repository`) and reads via `GH_REPO` with no working directory, falling back to the checkout only when a swarm has no GitHub origin. This restores fixer-task filing for promotion checks on controllers without a local checkout.

## v2.17.667 · 2026-11-06

### 2026-11-06 · ec5f4382735b · #9876 · @agenticore-app

**Leave the size allowlist to the size gate in the threshold pins check**

This change registers `tests/SIZE_ALLOWLIST.json` under `threshold_pins.GRADED_BY` with `scripts.size_limits`, matching how `TYPE_BASELINE.json` and `TICK_PATTERN_ALLOWLIST.json` are handled. Previously, the pins check flagged a unit moved to a new file (with the same parameters and no growth) as a new, disallowed entry, even though the size gate already permits such renames. Now that validation is deferred to the size gate, which correctly accepts non-growing moved units while still rejecting any entry that actually grows or is newly added without justification.

## v2.17.666 · 2026-11-06

### 2026-11-06 · a01068212818 · #9874 · @agenticore-app

**Grade the coverage promotion test in one process under a signal timeout**

This change fixes the coverage promotion test that was hanging CI by forcing it to grade coverage in a single process instead of forking a ProcessPoolExecutor, since the fork was happening inside a multi-threaded xdist worker and never completing within the thread-based timeout. It also adds a module-level signal-based timeout of 30 seconds, which can actually interrupt a stuck call unlike the previous approach, ensuring the test fails fast rather than hanging the whole unit shard and blocking merges.

### 2026-11-06 · bf87516dd72c · #9776 · @nestorcolt

**Retire the workstation tick units for swarms served in Anton**

This change stops the workstation from redundantly ticking swarms that now run on the Anton controller, which previously logged a misleading error every minute. It detects controller-served swarms from their stored api_url, and when no swarm needs local handling, removes the tick cron, waits timer and service, tick watcher, and inbox waker, while keeping the scheduler, quota prober, heartbeat, and herdr client intact. Local swarms still get their units restored on start, the CLI now refuses to manually tick a controller-served swarm, and the doctor's stale-tick check no longer false-positives on retired workstations.

## v2.17.665 · 2026-11-06

### 2026-11-06 · 7e9726556ba6 · #9847 · @nestorcolt

**Fleet panic spares release tag runs; merge stall read skips without gh**

This change fixes two issues that were undermining the panic workflow: fleet panic cancellation now spares runs tied to release events or version-tagged heads (vX.Y.Z), so builds shipping an actual fix on tags like v2.17.652 aren't killed mid-run and don't need manual rerunning. Separately, the merge stall GitHub read no longer fails its tick step when gh isn't installed on a host; it now records a skip in merge stall state instead, preventing repeated false failures that were holding the panic level down.

## v2.17.663 · 2026-11-05

### 2026-11-05 · d33a41d2f8e8 · #9853 · @nestorcolt

**Document the tick pass step and fan out executors**

This change adds documentation for the tick engine's pass executors to docs/reference/tick-engine-steps.md, covering the step and fan out executors, worker limits, thread naming, shutdown and queued future cancellation, the handed off wait rule with SETTLE_S, the refusal of never ending needs, and a usage example, along with a new test table entry for tests/tick/test_executors.py and two troubleshooting rows. It closes out the documentation task for pull request #9741, ensuring operators understand how the one-executor-per-pass behavior works and can troubleshoot related issues, keeping the reference docs in sync with delivered tick engine behavior.

## v2.17.659 · 2026-11-05

### 2026-11-05 · e4f4b5c7fd90 · #9840 · @nestorcolt

**Retry apt-get fetch on the worker image's Debian snapshot mirror**

This change adds `-o Acquire::Retries=5` to the `apt-get update` and `apt-get install` commands in the `worker` stage of `docker/swarm-node/Dockerfile`. Two separate promotion runs failed the worker-image build with transient 503 errors from `snapshot.debian.org`, each time on a different package, pointing to a flaky upstream mirror rather than a bad pin. Retrying fetches lets the build tolerate those transient backend hiccups instead of failing the whole promotion, improving CI reliability without touching the unrelated `worker_image.py` download-timeout issue.

## v2.17.658 · 2026-11-05

### 2026-11-05 · ea2e4f6cc602 · #9788 · @nestorcolt

**Scaffold a tick step and its artefacts in one command**

Adding a new tick step used to mean touching five places by hand: the step stub, its budgets row, a test file, the scenario index, and the feature map entry, each easy to get wrong or forget. This change adds `python -m scripts.tick.new_step`, which computes and writes all five artefacts together, refusing duplicate names and unknown ports up front, plus a `--check` flag that reports which artefacts exist. Step modules are now listed in `scripts.tick.steps.MODULES` and loaded automatically, so a scaffolded module needs no manual import. Catalogue rendering is deferred to a follow-up slice.

## v2.17.653 · 2026-11-05

### 2026-11-05 · 02015917fb1c · #9791 · @nestorcolt

**Resolve dashed controller account keys to the hive's underscore token slot**

This fix resolves the "account suffix not found" errors that were causing 59 SpawnError failures per hour during the fleet panic. Controller account keys use dashes (from Kubernetes Secret naming) while hive routing slots use underscores (AH_CC_TOKEN slugs), so exact-match lookups in `credential_for_slug` failed whenever a dashed controller key reached `init-agent --route`. The suffix lookup now normalizes dashes and underscores before matching, letting dashed controller keys resolve correctly to the hive's underscore token slots and allowing hive launches to proceed without manual intervention.

### 2026-11-05 · 16101d22a427 · #9778 · @nestorcolt

**Return the snapshot job line as its action instead of a pass box value**

This change fixes a race where a slow-running snapshot job thread could write `box["started"]` after `run_tick` had already read it, causing passes to end early with a spurious `Left` result. The snapshot step now returns its `started automatic snapshot` line as its own action with `writes=frozenset()`, so `run_tick` never reads a started value from it. This stops the early-end-on-started streak regardless of job timing, closing out the root cause identified from the tick journal and confirmed by the included red/green test proofs.

### 2026-11-05 · a369280d3a29 · #9708 · @nestorcolt

**Move the shared tick helpers into the common step module**

This PR moves eleven helpers shared across tick steps (skip_refused, skip_dropped, _drop, _rebind, _ended, _current_owner, and others) out of scripts.swarm.tick into a new scripts.tick.steps.common module, with the old module re-exporting each name so every existing caller keeps working unchanged. It is part of the tc-h sweep standardizing how tick steps are structured. Beyond cleanup, it shrinks basedpyright's unknown-type surface in tick.py and three downstream callers, since two of the moved helpers now return typed bool instead of unknown, and it trims the tick pattern allowlist from 130 to 119 names.

## v2.17.652 · 2026-11-05

### 2026-11-05 · 0640a9ceda29 · #9784 · @nestorcolt

**The controller keeps a hive placed lane agent live by its heartbeat**

This fix stops the controller from prematurely killing lane agents that were placed on remote workstation hives: previously, if the hive's local pane reporting looked "gone," the controller would retire the engineer and reopen its task even while the agent was still actively working, causing churn like tc-g3 cycling through four engineers in an hour. Now the reap and verify passes check the agent's own heartbeat before acting, keeping a remotely placed agent's record, claim, and lease alive as long as its heartbeat is recent and its task hasn't ended, while still correctly reaping truly stale agents.

### 2026-11-05 · d42b8b6294d8 · #9774 · @nestorcolt

**Tick load budget test fetches dev when origin dev is missing**

This change fixes a flaky unit test where the tick load budget check assumed `origin/dev` always existed, which failed on shallow checkouts used by unrelated pull requests. Now `base_budget` automatically fetches `dev` into `refs/remotes/origin/dev` when it's missing, and a new test confirms this fetch happens correctly and the base budget can still be read afterward. This matters because it stops unrelated pull requests from being incorrectly marked red due to a missing branch reference, keeping CI signal accurate and trustworthy for the tick workflow.

## v2.17.651 · 2026-11-05

### 2026-11-05 · 34baf8499588 · #9780 · @nestorcolt

**Match hive repositories across host checkout paths**

This change fixes a bug where a hosted controller would reject tasks simply because the workstation's repository path didn't match the hive's checkout path, even though they referred to the same repository. Placement logic now identifies repository identity across differing host paths and passes the correctly matched hive checkout through to the launch step and payload, while still rejecting genuinely conflicting remotes and ignoring unrelated repository advertisements. This matters because it unblocks legitimate tasks that were previously failing due to path mismatches alone, improving reliability for teams whose local checkout locations differ from the hive's recorded path.

## v2.17.650 · 2026-11-05

### 2026-11-05 · 139f184f2ae6 · #9754 · @nestorcolt

**Read each test file once per mutation plan and count mutants on one parse**

This change makes the mutation planner read each test file once per plan instead of reparsing it for every selection, and counts changed-line mutants from that single parse's metadata rather than rebuilding wrapper objects and repeating position passes per changed file. It also avoids a second module visit for possible mutants except when a graded visit refused a class, and logs the plan's CPU seconds for visibility. On the same runner, this cut plan CPU from a mean of 22.9s to 12.1s and wall time from 24-32s down to 13-19s, directly speeding up pull request checks.

### 2026-11-05 · be325b7e383c · #9760 · @nestorcolt

**Fetch dev on every selected unit leg**

This change fixes a CI reliability bug where the origin/dev fetch step on unit-selected legs was incorrectly gated behind the early collection download succeeding. Since 3.11 legs skip the collection step entirely, and any missing collection on other legs left dev unresolved in the shallow checkout, selected tests relying on origin/dev, like the tick load base budget test, would fail even on unrelated pull requests. The fetch now runs unconditionally on every selected leg, while reuse grading remains correctly gated on the collection inside the adopt step. Verified green across all selected legs post-fix.

## v2.17.649 · 2026-11-05

### 2026-11-05 · 28b6c43890eb · #9711 · @nestorcolt

**Refuse tick steps without a feature entry and a scenario**

This change adds catalogue-aware validation so the tick step registry now refuses any step whose feature isn't mapped or whose name no scenario covers, unless it's explicitly on an unmapped list; it also rejects unmapped entries that are actually mapped or unregistered. A new `catalogue` module provides `check` (validates the live registry against feature-map and scenario files) and `shrink` (keeps the unmapped list from growing, enforced against a base checkout so a head change can't loosen it). This closes the gap where steps could silently exist without a feature or scenario, keeping the tick development pattern consistent and gated.

## v2.17.648 · 2026-11-05

### 2026-11-05 · bb5b3dd5edbd · #9739 · @nestorcolt

**Send stall alerts for a never launched backend master seat to the front door**

This change fixes stall alerts repeatedly naming a second master seat that had started but never actually launched: it had an empty pane, zero launch time, and no process, yet the pool logic still counted it as a live, non-empty seat. Now a master record in that state is treated as unlaunched, its seat is excluded from live masters and counted empty, so mail addressed there reroutes to the front door instead of a dead seat, and the stall alert's action line correctly reports the address it was actually delivered to.

## v2.17.647 · 2026-11-05

### 2026-11-05 · 954838087aa8 · #9770 · @nestorcolt

**Hive launches name the hive's own account slot**

This fix resolves a naming mismatch between the hosted controller, which identifies accounts by hyphenated Secret keys, and a hive's launcher, which only recognizes underscore-based `AH_CC_TOKEN_` slugs. Previously, `Consumer.launch` passed the controller's slot name directly to the launcher, causing every hive launch to fail with an "account suffix not found" error. Now the consumer maps the order's slot through `placement.account_key` to the hive's own registry slot for that harness, launching with the correct name and checking placement consistently.

## v2.17.646 · 2026-11-05

### 2026-11-05 · 165bf9da7c30 · #9767 · @nestorcolt

**The controller keeps a hive reported master live by its heartbeat**

This fixes a bug where a master reported by a different hive, but with no execution target of its own, was wrongly judged dead on this hive because its local pane and process checks failed, triggering a refused retire and then a blocked relaunch. The controller now recognizes such masters via `held_elsewhere.keep`: if another hive lists them and their heartbeat is still fresh, they're kept live without local binding judgement, and any stuck `retiring` state reverts to `working`, avoiding false retirement loops.

## v2.17.645 · 2026-11-05

### 2026-11-05 · ef6820fceeb2 · #9766 · @nestorcolt

**Profile choice falls back to the lane profile when no decision backend answers**

This change fixes a bug where tasks would stall indefinitely with "profile classification is unavailable" whenever no decision backend responded, requiring manual intervention to assign a profile before work could proceed. Now, when the classifier is unreachable, the claim automatically falls back to the lane's default profile, recording the source as "lane default" with the unavailability noted as the reason—mirroring the existing below-floor fallback behavior. Refused or unresolved classifier responses still block as before, preserving the safeguard against silent engineer fallbacks. Note that the controller's actual connectivity to the decision backend remains a separate deployment concern.

## v2.17.644 · 2026-11-05

### 2026-11-05 · dbe60ff9538d · #9763 · @nestorcolt

**Hive placement matches account slots in either separator form**

This fix resolves a placement bug where hive routing rejected valid matches because account identifiers used underscores while hive-advertised slugs used hyphens (e.g. `nestorcolt-smatgmail-com` vs `nestorcolt_smatgmail_com`), causing every task to fail with "no hive" errors. The fix centralizes normalization in `scripts.hive.placement.account_key`, now shared across slot, interactive login, and master login checks, and updates `scripts.hive.launch` to use this same key rather than its own private implementation, ensuring consistent matching regardless of separator style.

### 2026-11-05 · 336672cf7e12 · #9762 · @nestorcolt

**Ledger readiness accepts a master launched after staging without an execution identity**

This fix resolves a false negative in ledger cutover readiness: previously, a master launched after the tick move to the Anton controller had no execution_id and generation 0, which made the freshness check fail and blocked the hosted ledger activation even though the launch was valid. Now readiness logic accepts a master lacking execution identity if its launch timestamp is positive and no earlier than the ledger staging time, using that timestamp instead of generation for the receipt, and acknowledge additionally checks the agents hash and re-verifies the row's launch timestamp before confirming, preventing stale or mismatched receipts.

### 2026-11-04 · cb75dc987a3d · #9731 · @nestorcolt

**Run the whole tree tick pattern comparison at promotion**

This change closes a gap where the tick pattern gate only checked files touched by a pull request, letting two individually clean merges combine into undetected drift on dev. A new `scripts.tick_pattern --tree` mode now compares the entire tree against the allowlist and names any differing files; a `tick-pattern-tree` job runs it on every dev push, feeding `promotion-ready` and `promote` so drift turns promotion red and triggers the usual fixer task automatically, rather than silently accumulating. It runs only on dev pushes, not pull requests or the queue, with a 60s budget and light runner.

### 2026-11-04 · c75424f66e2e · #9749 · @nestorcolt

**Document the tick closing lane for heartbeat, last tick and status publish**

This change documents the closing lane already shipped in #9720, which runs the heartbeat, last tick, and status publish from the pass finally rather than inline in the tick. The new section in tick-engine-steps.md covers the CLOSING_LANE, the 30-second CLOSING_S deadline, the closing group, how ended values (full, early, none) are stamped, sequential mode, and the hang-proof test, while swarm.md and tick-adapters.md are updated to match. This gives operators a clear, accurate reference for how tick closure now behaves so they can troubleshoot heartbeat and status issues confidently.

## v2.17.643 · 2026-11-04

### 2026-11-04 · 216a351fdbee · #9751 · @nestorcolt

**Close every operator workflow viewer and redraw before keyboard walks**

This change fixes a flaky test that was ejecting unrelated pull requests from the merge queue. The operator workflow package test suite could leak a browser context when an exception occurred before cleanup, poisoning later attempts and the module-scoped fixture; it also raced a cooperative render against a synchronous keyboard walk, occasionally losing focus. The fix closes every viewer context regardless of outcome and forces a synchronous redraw before the keyboard walk, making case a and c deterministic so the tick pattern's scaffold, gate, registry rule, and sweeps can merge without being ejected by this unrelated flake.

## v2.17.642 · 2026-11-04

### 2026-11-04 · 73dddfcc294b · #9742 · @nestorcolt

**Document the tick pattern gate without its bootstrap path**

This change updates the Tick pattern gate reference to document that the gate no longer has a bootstrap fallback: the Hold the tick step pattern step script now fails outright if the protected grader copy is missing `scripts/tick_pattern.py`, and a base missing `tests/TICK_PATTERN_ALLOWLIST.json` fails grading with a specific error and summary line, both now captured with exact output. It also records the removed bootstrap rule as history, ties it to the same pattern used when the whole ledger state gate dropped its bootstrap, and adds two new Troubleshooting rows so operators can recognize and resolve these failures quickly.

## v2.17.641 · 2026-11-04

### 2026-11-04 · 07eda110e3b5 · #9734 · @nestorcolt

**Time box the shared mutation stats download and plan probed shards for the lowest stage wall**

This change fixes two sources of wasted CI time in test sharding. Each retried "Download the shared mutation stats" step now times out after one minute instead of three, so a stall triggers the existing retry logic quickly rather than stalling a shard. Separately, the shard planner now picks the shard count with the lowest overall stage wall time, choosing one wave of longer shards over two waves of shorter ones when a remainder can't fit a single six-minute wave, cutting replan times from 80 shards and two waves down to a much faster single-wave plan.

### 2026-11-04 · 79d12f0579b7 · #9720 · @nestorcolt

**Publish heartbeat, last tick and status from the pass finally on a closing lane**

This change moves heartbeat, last tick, and status publishing out of the critical lane's deadline and into a dedicated closing lane that the pool's Pass starts on first use, governed by its own `closing_s` deadline. Previously an expired critical lane could drop these writes, risking the stale watcher restarting a live scheduler; now they publish reliably from `run_tick`'s finally block on full or early-ended passes, while stopped, halted, probe-failed, or raised passes stamp nothing, matching current behavior.

### 2026-11-04 · dcb7e6071fe0 · #9686 · @nestorcolt

**One ledger read per pass**

This change cuts redundant ledger reads during each pass by making `view.document` return the already-held view instead of re-reading the ledger unless it's stale, so every full-document read within a pass shares one read. Phase change detection now works off that held pass document via a new projection module, and `promotion_watch.refresh` and `cli._phase_steps` were updated to stop issuing extra reads. Capacity keeps a separate call since threading the document through it pushed test selection over budget. Net effect: fewer ledger reads per pass, verified by mutation tests and live tick timing, with no behavior change.

### 2026-11-04 · b2a4d06848d8 · #9729 · @nestorcolt

**Mutation probe names the mutant status it refused instead of a budget message**

When the early mutation probe shard refused a mutant result, it always blamed a budget timeout, which was misleading when the real cause was something like a mutant returning "no tests." Now the probe tallies each refused status by name and counts owned mutants still missing a result, only mentioning budget when elapsed time actually exceeded it. This makes failures self-explanatory, so an engineer seeing "no tests" in the message knows immediately what to fix instead of chasing a nonexistent timing problem.

## v2.17.637 · 2026-11-04

### 2026-11-04 · 2da5164ff443 · #9701 · @nestorcolt

**Document the spawn order timed rows for the running agent filter and focus placement**

This change adds documentation covering the timed rows that pull request #9673 introduced inside spawn order, specifically naming each timed segment (`_running`, `ready_in_order`, `focus_budget.place`, `profile_first.placeable`, `_ranked`) and explaining how to read them via the tick report command. It updates tick-adapters.md with a new section, links it from tick-report.md and index.md, and includes a changelog fragment. This matters because it gives operators a clear, verified reference for diagnosing where spawn order time is spent, backed by a live reading from the workstation tick, supporting the goal of keeping every pass under sixty seconds.

## v2.17.634 · 2026-11-04

### 2026-11-04 · 00bf6cd33454 · #9673 · @nestorcolt

**Time focus budget placement and the running agent filter in spawn order**

Spawn order now emits a per-lane timed step row for focus budget placement (scripts.swarm.focus_budget.place) and for the running agent filter (scripts.swarm.tick._running, pulled out of the inline list in _spawn_order), nested under the existing _spawn_order row. This closes a visibility gap where a 21-second spawn order pass had no timed row above two seconds, so the tick report can now show exactly where those seconds go, supporting the push to keep every tick pass under sixty seconds. Verified with full mutation coverage and a live tick report showing both new rows with medians.

## v2.17.633 · 2026-11-04

### 2026-11-04 · 8c40a11dc829 · #9608 · @nestorcolt

**Tick report columns for late, abandoned, held, skipped and dropped writes**

This change adds new columns to the tick report so each pass now surfaces counts of writes that were late, abandoned, held, skipped, or dropped, giving the operator visibility into pass health beyond simple pass/fail timing. Tests were written first to pin down the expected report shape, then the reporting logic was extended to populate these fields from existing pass data, closing out task tc-g15 under the under-a-minute tick phase without altering the core scheduling loop.

### 2026-11-04 · 6fd9969e2d82 · #9652 · @nestorcolt

**Name the caller and holder in the tick lock stale error and journal each take and release**

When the tick lock goes stale, you'll now see exactly who tried to take it and who's actually holding it, instead of a bare error — and every take or release of that lock writes a one-line journal entry to stderr naming the caller and what happened. This makes future stale-lock incidents diagnosable from logs alone rather than requiring guesswork. Under the hood, lock reads now go through a typed helper instead of a cast, which also trims a few type errors from the codebase. This closes out task tc-g14 toward the under-a-minute tick phase.

## v2.17.631 · 2026-11-04

### 2026-11-04 · 80bb6f4290ea · #9663 · @nestorcolt

**Return the master stall pass as stall writes the engine applies while the master stays live**

The master stall step now returns its decisions as writes for the engine to apply, instead of mutating state directly: `master_stall.stalls` decides forget, stop, or retire for the live master without writing, `apply_stall` applies one decision, and `stall_writes`/`stall_writer` turn decisions into guarded compare-and-set writes keyed on the master name read. If the master changes before the write lands, the engine drops it with a `RevisionConflict` and recomputes it on the next pass, rather than corrupting state. This closes out the pure-steps task for the parallel engine, leaving the stall step safe to run concurrently with other steps.

## v2.17.629 · 2026-11-04

### 2026-11-04 · 42c1e6400516 · #9650 · @nestorcolt

**Rebind the hive daemon hive_id before every test**

This fix closes a flaky, test-order-dependent failure where `tests/hive/test_refresh.py` picked up a stale `hive_id` stub left over from earlier tick characterisation tests that monkeypatched `scripts.swarm.commands.hive_id`, because `scripts.hive.daemon` had bound its own module level reference to that function at import time. A new autouse fixture in `tests/conftest.py` rebinds `daemon.hive_id` back to the real `commands.hive_id` before every test, so each test starts clean regardless of execution order, eliminating the shard 17 failures seen on Python 3.12.

## v2.17.627 · 2026-11-04

### 2026-11-04 · 2bf6333b3e29 · #9622 · @nestorcolt

**Document the saved fleet red listing, the notice step budget and the status publish findings reuse**

This change updates the documentation to reflect behavior already shipped in the scale proof work: the tick claim filter now reads the saved fleet red listing via `fleet_red.saved_reader`, the notice step keeps its listing save within a measured thirty second budget, late steps drop their returned writes and cache saves, and status publish reuses health findings the pass already computed. It adds usage, limits, troubleshooting, and test coverage notes, plus reference index entries, so operators have an accurate record of what fifteen-agent scale proof actually does and why it stays fast.

## v2.17.626 · 2026-11-04

### 2026-11-04 · d073f36132e1 · #9628 · @nestorcolt

**Run the Tests mutation plan on the standard runner pool**

The Tests mutation-plan job now runs on the standard runner pool instead of the light pool, keeping its five minute timeout unchanged. Timing data showed the job intermittently timed out on light pods because their 250m CPU request meant it could get throttled to roughly 6.5% of a core under node contention, while the actual CPU work only needs about 14 seconds; standard pods request 2000m, avoiding that starvation. This should eliminate the sporadic timeouts seen on PR 9547 without changing shard or stats sizing behavior.

## v2.17.625 · 2026-11-04

### 2026-11-04 · cda4cddf2d55 · #9609 · @nestorcolt

**Raise a health finding for tick passes that keep ending early on the same missing value**

Swarm status now surfaces a new information-only health finding, kind `early end`, when several consecutive tick passes stop early on the same missing value, with the streak length controlled by `AGENTIHOOKS_HEALTH_EARLY_ENDS` (default 3). The heartbeat still gets stamped and nothing is blocked; the finding is purely visible so the stale watcher can notice a tick that keeps doing no work. A single full pass resets the streak. Includes tests, feature map entry, and docs updates for the new threshold.

## v2.17.623 · 2026-11-04

### 2026-11-04 · d03515863099 · #9610 · @nestorcolt

**Let the task update guard match size and group members**

This change extends the ledger's task_update guard so a write can require that a task's difficulty (size) and group_members are unchanged on the server, not just ownership fields as before. It adds defaults for tasks missing these fields, validates malformed guards with clear errors, and checks each matched field in `_guarded`. This matters because it lays the groundwork for atomic tick size and group release writes, letting future appliers safely compare-and-set these fields without race conditions, though wiring that in is deferred to a follow-up task while issue #9598 stays open.

## v2.17.620 · 2026-11-04

### 2026-11-04 · 792810298be5 · #9606 · @nestorcolt

**Document the source control Semgrep rule over scripts/tick**

This change updates `docs/reference/tick-adapters.md` to document that the `swarm-source-control-port` Semgrep rule now scans `scripts/tick` in addition to `scripts/swarm`, closing out the documentation debt left by PR #9597. It adds concrete failing and passing tick examples using the real `SourceControl.change` port call, explains the finding the scan reports, clarifies that the fixture grades pattern shapes while the repository scan grades the path include list, and updates the troubleshooting table to name both trees, so engineers working in `scripts/tick` understand how the gate applies to their code.

## v2.17.619 · 2026-11-04

### 2026-11-04 · c74d183069f4 · #9590 · @nestorcolt

**Reuse the pass's health findings in the tick status publish**

This change stops the tick status publish from recomputing health findings that the pass already gathered a moment earlier: the report step now passes along the stored findings, and status_report only calls findings() itself when none are supplied, with the same fallback preserved for callers that didn't capture them. It shaves roughly nine seconds off each pass by removing a duplicate computation of status.findings, directly advancing the goal of getting a full tick pass under one minute at scale.

## v2.17.617 · 2026-11-03

### 2026-11-03 · 1977278c62f6 · #9557 · @nestorcolt

**Release the claim of an agent its beating hive no longer lists**

This change fixes a gap where an agent assigned to a hive before that hive reported its host target could get stuck if the hive later started beating but stopped listing the agent among its members. The tick now detects this case via a new `Reports.vanished` check, treats it the same as a hive going silent, and reaps the agent: releasing its seat and reopening its claimed task while preserving its branch and work folder, so work isn't silently lost or left stuck in limbo.

## v2.17.614 · 2026-11-03

### 2026-11-03 · a6506ccdc9b2 · #9555 · @nestorcolt

**Move the size and group release tests onto the tick write path**

This change finishes the cleanup started in #9475 by moving the difficulty size and grouping release tests off the deleted `size_pass`/`release_pass` functions and onto the current tick write path, exercising `tasks.size_writes`/`tasks.task_writer` and `tasks.release_writes`/`tasks.group_writer` instead. The tick engine steps reference is updated to name the actual write functions and appliers rather than the removed passes. This keeps the test suite aligned with the real code path, removes dead reimplementations, and closes out the follow-up task left after the legacy passes were deleted, with mutation testing and review confirming no regressions.

## v2.17.610 · 2026-11-03

### 2026-11-03 · 0b3ad4ae3ff7 · #9528 · @nestorcolt

**Fleet red listing cache write moves behind an engine applier**

The fleet red notice step no longer writes its open pull request listing directly to the store; instead, `fleet_red.cached` returns the listing alongside any needed cache save, which flows through holds and task writes to a new `fleet_red.saver` applier that saves it under an NX guard with a ten minute expiry, raising a conflict if another pass already saved it. This completes the tick core plan's move toward engine-run steps that share no state, removing the last direct write from this step so parallel passes can never race on the listing cache.

### 2026-11-03 · 3790670b1530 · #9539 · @nestorcolt

**Let the protected grader accept the early mutation probe and replan stages as non gates (Refs #9536)**

This change extends `gate_protected.py`'s `MUTATION_STAGES` list to recognize `mutation-probe` and `mutation-replan` as valid non-gate stages, matching existing handling for `mutation-refine` and `mutation-shard`. It allows a branch head to declare these stages under `not_gates` while still requiring them in the `mutation` job's needs, with corresponding test coverage added for both new stages. This unblocks the upcoming Tests workflow update that introduces the probe and replan jobs themselves, which will land in a separate pull request.

## v2.17.608 · 2026-11-03

### 2026-11-03 · 861f05a17069 · #9537 · @nestorcolt

**Announce the protected grader blob that accepts the early mutation stages (Refs #9536)**

This change registers a new allow-listed hash for `scripts/gate_protected.py` in the protected-blob manifest, specifically the version whose `MUTATION_STAGES` tuple includes the four early mutation phases (refine, probe, replan, shard). It's a prerequisite step: the actual grader logic update in the next pull request can't land unless its exact blob hash is pre-approved here, keeping the frozen grader file tamper-resistant while still allowing the planned probe-and-shard mutation workflow improvements to proceed.

### 2026-11-03 · baf07ad8e644 · #9533 · @nestorcolt

**Size mutation preflight shards after the early tests from what they left**

This change fixes mutation preflight on branch pushes to size its shard jobs from what the early test stage actually leaves alive, rather than the worst-case estimate used before any tests ran. A new lightweight `replan` job merges early results, marks killed mutants as done with zero cost, and prices survivors from measured slice costs, fitting shards into the budget the slowest early job left. This collapses a two-wave, 49+49 job run into a single 40+21 job wave, cutting planned shard worker time from about 130,000 to 44,000 seconds with no mutants lost and more kills caught early.

## v2.17.606 · 2026-11-03

### 2026-11-03 · 052ed283086e · #9518 · @nestorcolt

**Test runtime observe on a remote worker its hive reports gone**

This change adds a direct test covering `HerdrRuntime.observe`/`status` for a remote worker whose hive report marks it gone, confirming it correctly returns `exited`, and also checks that with no hive reports at all `observe` returns `unknown`. It's needed because an earlier fix (#9506) changed the tick logic to check `Reports.gone` before calling `runtime.observe`, which meant the existing tick tests no longer exercised that gone-branch code path, tripping the coverage ratchet. This is a test-only addition with no production code changes, restoring coverage so the pipeline passes.

### 2026-11-03 · 6bcc4f6c151a · #9517 · @nestorcolt

**Run the post spawn ledger, GitHub, findings and metrics tick steps in the slow lane**

This change moves twelve post-spawn report steps (ledger events, done rechecks, progress checks, merge pass, doc report, findings, metrics, bottleneck status, snapshot job, etc.) into the slow lane, since none of them block spawn or are needed by a critical step. Command runner and quota notice stay critical because they run before spawn. The tick registry now also enforces ordering across lanes for readers and writers. This directly informs whether lane processes get built, since the decision hinges on critical lane CPU share staying above forty percent, measured in the next tick report.

### 2026-11-03 · 4be3124c8bbf · #9475 · @nestorcolt

**Tick steps return writes the engine applies with compare and set**

This change moves sizing and grouping steps away from writing state directly, having them instead return writes that the engine applies through per-domain compare-and-set appliers; if a write conflicts with what another step changed, it's dropped, logged on the step record with its reason, surfaced in step actions, and recomputed on the next pass rather than silently corrupting data. This is the first slice of the broader move to pure steps under the parallel engine, proving the pattern with a concrete conflict test before extending it to rank, phase, planning, fleet red, and stall steps in a follow-up pull request.

### 2026-11-03 · cffcc1094275 · #9506 · @nestorcolt

**Settle a worker its restarted hive reports gone instead of retrying retire**

This fix corrects a bug where a worker on a hive that restarted or dropped its pane would get stuck being retried for retirement indefinitely, since the tick's runtime lacked hive reports and so never detected the worker was gone. Now `_remote_gone_reason` checks `Reports(store.redis).gone(agent)` directly, so the tick correctly ends such records, releases claims, reopens unfinished tasks, and clears the retire-failure finding, rather than endlessly retrying a retire that always fails.

## v2.17.605 · 2026-11-03

### 2026-11-03 · 635b434ef675 · #9509 · @nestorcolt

**Grade oversized mutation plans in two runner waves instead of refusing them**

The mutation gate planner now runs oversized changes across two waves of the 40-runner pool (up to 80 shards, 216000 worker seconds) instead of refusing them outright, since queued shards don't count against timeout-minutes. Pull request counts in any remaining refusal now reflect this real 80-shard capacity, and a refusal only names a specific mutant when it genuinely can't be split to fit a shard. Kill-file collection and preflight seed copying were fixed to handle same-named files across shards. Verified against e2: 49 shards graded all 4618 mutants with no budget exhaustion, with CI node count unchanged at seven.

## v2.17.603 · 2026-11-03

### 2026-11-03 · d2ca9320bdc6 · #9492 · @nestorcolt

**Accept the focus only flag in the ledger API schema**

This fixes a schema bug where the ledger server rejected `focus --only` requests with a 400 error because `operation_schema` defaulted untyped fields to strings, while clients send `only` as a JSON boolean. The ledger schema now explicitly types `freeze_set`'s `only` as a boolean and `position` as the `top`/`bottom` enum matching `ledger_freezes.POSITIONS`, so valid requests pass validation while invalid values (non-boolean `only`, bad `position`) are still correctly refused. This unblocks the `agentihooks swarm focus --only` CLI workflow.

## v2.17.600 · 2026-11-03

### 2026-11-03 · 950d4c0f5424 · #9484 · @nestorcolt

**Seed the first Tests mutation plan of a pull request from the branch preflight kill memory**

When a pull request's first Tests run can't restore its own cached kill memory, it used to price every mutation split unit blind, pushing eleven of thirty-six e2 shards over budget. This change has mutation-refine mint a scoped token, locate the newest Mutation preflight run for the head commit (falling back to the branch) with live killer artifacts, download them before planning, and save them under the pull request cache with a distinct preflight suffix. The first run now plans from real evidence instead of guessing, and the final mutation job's cache save stays separate.

## v2.17.599 · 2026-11-03

### 2026-11-03 · c92aed7e72a6 · #9485 · @nestorcolt

**Document the default on hive host projection read and its failed projection cleanup**

This change documents the real behavior shipped in PR 9474 so operators know exactly what to expect when rolling out the host projection read. It records that the projection read now defaults on (only `false`/`0`/`no`/`off` disable it), explains how a failed write after fenced commit gets cleaned up via `forget_hosts` with fallback to the registry, lists the gaps cleanup can't cover, spells out the safe rollout order, and gives a concrete acceptance command with a measured passing result, so future operators can trust and verify the rollout without re-deriving behavior from code.

### 2026-11-03 · f7bf73ebb7a7 · #9482 · @nestorcolt

**Carry mutation kill memory between Tests runs**

This change makes the Tests workflow cache and reuse mutation kill-memory across runs, the same way mutation-preflight already does. Previously every shard was priced using a generic head-kill estimate instead of observed timings, causing 11 of 36 shards to blow their 1080s budget despite accurate per-unit costs existing. Now shards restore prior kill memory, upload their own observations even on failure or timeout, and the mutation job merges and saves them, so later runs plan from real data. First runs on a new PR still plan blind, but subsequent runs should stop exhausting budget.

## v2.17.598 · 2026-11-03

### 2026-11-03 · 761b429e81c4 · #9479 · @nestorcolt

**Reserve shard setup time when sizing mutation shards**

This change fixes the mutation shard planner, which was packing shards to the full `workers * budget` figure without leaving room for mutant generation, clean/forced-fail runs, reporting, and tail effects, causing five shards to run out of time and leave mutants ungraded. It introduces a 180-second per-shard setup reserve so capacity is computed as `workers * (budget - 180)`, applied consistently in shard counting, the fit loop, and overrun messaging. This raises the plan from 32 to at least 38 shards, still under the 40-runner ceiling, and should eliminate budget_exhausted failures on rerun.

## v2.17.596 · 2026-11-03

### 2026-11-03 · 01e7409aacec · #9473 · @nestorcolt

**Document the source control port tools, its swarm callers and the gh command gate**

This change adds documentation to the tick adapters reference covering the full source control port delivered under task tc-d4: all twenty `SourceControl` operations and their underlying GitHub calls and records, the enqueue-before-merge rule for branches with a merge queue, every swarm caller of the port with its builder, deadline and refusal, and the `swarm-source-control-port` Semgrep gate with its now-minimal allowlist. It matters because it gives operators a single, accurate reference for how GitHub access is gated, declared, and tested, closing the loop on the port work and making the gate's intent auditable going forward.

## v2.17.595 · 2026-11-02

### 2026-11-02 · 93312a65d28c · #9292 · @agenticore-app

**Run tick guards on every tick pull request through the required gate**

Tick equivalence now runs as a required check on every pull request touching the tick engine, its steps, the registry, swarm tick modules, tick scenarios, the equivalence script, or the load test, instead of only on manual dispatch. It executes inside the writer lock job, so a failure there automatically turns the existing required gate red without touching the protected grader itself. This closes the gap that let a tick-breaking pull request merge with no equivalence run at all, as proven by a planted fault that now correctly fails the gate while a clean head stays green.

### 2026-11-02 · fc16740309a9 · #9388 · @nestorcolt

**Read the merge queue through the GitHub adapter and shrink the source control allowlist to the adapter**

This change finishes moving the merge queue's GitHub calls behind the source control adapter: graphql queries now run through the adapter's `graph` tool and REST calls through its `download` tool, both billed to the reader via `github_reads.calling`, with the old `_RunGraph` helper removed in favor of passing the adapter directly. The Semgrep allowlist for direct GitHub access now covers only the adapter and its cached reader, locking in the boundary so no other code can call GitHub directly, with a test pinning that allowlist.

## v2.17.594 · 2026-11-02

### 2026-11-02 · 970ac9011870 · #9463 · @nestorcolt

**Pin old layout registry writes, cold host read commands and retry jitter**

This change adds three regression tests covering behavior already shipped in the execution registry, with no production code added. It pins that archive and lease fenced rows are still written to the old layout document and readable by both `fencing.read` and the record port, locks the exact Redis call sequence (and count of 5) for a cold host scope read, and verifies the real retry jitter varies within `uniform(0.001, 0.032)` with no trailing sleep. It also tightens the member scope type baseline from 2 to 1 to match measured behavior, closing three spec gaps left from the registry's original review.

### 2026-11-02 · 73cf77da8d59 · #9464 · @nestorcolt

**Document tick steps reading the ledger view and the whole ledger state gate**

This change adds reference documentation for the tick-engine's ledger view and whole-ledger-state gate, covering the eight view parts, `view.holding()`, the `pass_state` read table, the widened `parts` export, and the full gate mechanics: what counts as a whole-ledger read, the shrink-only allowlist, grading, output, threshold pins, and how to remove a read. It also adds troubleshooting guidance and test references, plus an index entry and changelog fragment, so operators and contributors have an accurate, verified record of the already-merged behavior from PR #9345, reducing confusion when auditing or extending which steps touch the ledger.

## v2.17.593 · 2026-11-02

### 2026-11-02 · 2a6326b9f65f · #9465 · @nestorcolt

**Document execution registry and hive scope on records**

This PR documents the execution registry and hive scope work already shipped in #9279: it explains how execution now reads through the records port on both layouts, how hive placement reads use a hashed `hosts` projection gated by `SWARM_HOST_PROJECTION_ENABLED`, and how a jittered retry handles fencing conflicts. New pages cover testing and troubleshooting, the fencing-records reference lists the records port callers and the retry row, and the reference index gains three entries, giving operators a clear record of the behavior that cut hive scope read latency from 20ms to under 5ms median.

## v2.17.591 · 2026-11-02

### 2026-11-02 · dff7ec2ddcd3 · #9416 · @agenticore-app

**Clear comments in the install target adapters**

This change sweeps all whole-line comments out of the five install target adapter modules (`scripts/targets`), relocating their content into the docstrings of the units they describe, dropping section banners, and trimming stray history like dates and ticket numbers; the two cold-path import comments are kept per prior precedent. No executable lines changed, confirmed by syntax-tree comparison. One test was strengthened to pin the copilot hook's `timeoutSeconds` value at 30, and the comment-free-paths registry now covers these modules, keeping the codebase's comment-free standard enforced going forward.

## v2.17.589 · 2026-11-02

### 2026-11-02 · 68b6af7aa7c1 · #9412 · @agenticore-app

**Fail the whole ledger read gate when the base lacks its allowlist**

This change closes a self-grading loophole in the whole ledger read gate: previously, if the protected base branch lacked its own allowlist, the gate would fall back to trusting whatever the head branch claimed, letting a head script effectively grade itself. Now a missing base allowlist simply fails the gate outright. This matters because that fallback was only ever meant to bootstrap the gate's own rollout, and leaving it in place after that point let an attacker-controlled head branch bypass the allowlist check entirely by targeting a base without one.

## v2.17.586 · 2026-11-02

### 2026-11-02 · 699dbac59add · #9393 · @agenticore-app

**Clamp negative job durations and read runner minutes with the job token**

This change fixes the CI runner minutes measurement used to track efficiency against the 100 euro per day Oracle budget. A job whose recorded completion time preceded its start was producing a negative duration, which had thrown off a pull request tier reading by over 1188 minutes; the fix clamps such cases to zero seconds so the metric stays accurate. Additionally, the ci-minutes workflow now accepts a job token input, letting a measurement window be read using the job's own token rather than hitting the App installation's rate limit, keeping the ongoing measurement runs reliable.

### 2026-11-02 · bd97fca24b43 · #9389 · @agenticore-app

**Record the cause when a worker probe container misses readiness**

When a worker probe container fails to become ready, `supervision_proof.ready` previously just raised a bare timeout error with no context, making it hard to diagnose the failure. Now it captures the last readiness snapshot (or the snapshot error), the container's process list, and the last 40 log lines, each bounded by a time limit, and includes that evidence in the raised exception. This doesn't change pass/fail behavior, but the next time a worker probe times out, the uploaded evidence will show the actual cause instead of requiring guesswork.

## v2.17.585 · 2026-11-02

### 2026-11-02 · 2374b33699f7 · #9370 · @agenticore-app

**Let the protected grader accept the merged mutation stages as non gates**

This change updates `gate_protected.grade` so the protected grader no longer blocks pull requests introducing the new `mutation-refine` and `mutation-shard` jobs added in PR 9365. Previously, any non-gate entry had to already exist on the base branch, which broke lint since these jobs run outside Gate — Required by design. Now the grader permits them specifically when Gate — Required needs `mutation` and the head `mutation` job needs that stage, keeping the exception narrow while unblocking the mutation-shard budget work. All other new non-gate entries still require an existing base entry, and new tests cover gated, merged, and non-mutation cases.

### 2026-11-02 · 3c3ab8a7bf65 · #9383 · @agenticore-app

**Announce the protected grader and reuse blobs that admit the split unit steps**

This registry-only update records the blob hashes for `scripts/gate_protected.py` and `scripts/ci_reuse.py` from branch `ci-323133-3649` into `tests/registry/FROZEN_GATES.json`, formally announcing the protected grader and letting later steps reuse these blobs. It exists purely to satisfy the frozen gate protocol, allowing the actual gate change admitting split mutation unit steps in Gate — Required to be submitted as its own follow-up pull request, which then feeds into activation in #9382.

## v2.17.584 · 2026-11-02

### 2026-11-02 · b502e850eedc · #9345 · @nestorcolt

**Steps read the pass ledger view; gate whole ledger state reads**

This change makes tick steps and swarm helpers read a held per-pass snapshot of the ledger (freezes, priorities, follow ups, alerts, overview) instead of repeatedly pulling the whole ledger state, with `pass_state` falling back to a full read only when no view is held, so behavior outside a pass stays the same. A new `no_whole_ledger` check enforces this going forward, counting remaining whole-ledger reads against a shrink-only allowlist (currently 11 reads in 5 files, none in tick), reducing load and catching regressions early. One known gap remains: `freeze.watched` still reads a watched peer ledger whole, tracked as a follow-up.

## v2.17.582 · 2026-11-02

### 2026-11-02 · 5d07c35e2013 · #9385 · @agenticore-app

**Announce the protected grader blob that keeps base mutation stage entries (Refs #9324)**

This change registers blob f009486c of scripts/gate_protected.py in tests/registry/FROZEN_GATES.json, marking it as a trusted frozen gate grader. That blob implements the reviewed fix ensuring a base mutation stage entry keeps its original expiry via setdefault, preventing a pull request from silently extending it. Since the frozen gate check only trusts blobs already announced on dev, this entry must land first so the dependent grader PR #9370 can pass its own gate check cleanly.

## v2.17.579 · 2026-11-02

### 2026-11-02 · a8c2935df89c · #9361 · @agenticore-app

**Read the dead stream until the over-cap redelivery is fetched**

This fixes a flaky NATS contract test that intermittently failed because the dead-letter stream was read only once, before the server's over-cap redelivery had actually arrived; under load or after an expired pull, the letter could land late and the single read would find nothing. The test now retries the take-and-read-dead-stream cycle for up to six ack-wait intervals (3.6s on NATS), so a late redelivery is caught without weakening any assertions. The memory backend is unaffected since it uses a fake clock and finds the letter immediately.

### 2026-11-02 · e5fbeec85aa0 · #9352 · @agenticore-app

**Turn automatic snapshots off in the swarm cli test fixture**

This change disables automatic snapshotting in the swarm cli test fixture by setting `AGENTIHOOKS_SWARM_SNAPSHOT_MINUTES=0`, fixing a flaky test where the tick's snapshot step, run inline on a cold worker, exceeded its time budget and caused the engine to abandon the step, dropping the tick's actions and triggering a `KeyError`. Since no swarm cli test actually exercises automatic snapshots, disabling it removes an unnecessary source of timing-dependent failures and lets the late handoff redirect test pass reliably on first run.

## v2.17.578 · 2026-11-02

### 2026-11-02 · 167efdbe8118 · #9343 · @agenticore-app

**Fail the mutation shard merge on a short or unmeasured shard**

This change hardens the mutation shard merge step so it no longer silently accepts a shard whose report claims success but actually graded fewer mutants than it owns, or carries no measurement at all. `merge.main` now fails and lists every such shard via `short_shards`, catching previously undetected incomplete coverage before it masks real mutants surviving undetected. `merge_reports` itself is untouched, so downstream consumers like `mutation_wait` and replay keep their existing completeness checks. Verified with planted short and unmeasured shard tests going red, and a complete run staying green.

## v2.17.574 · 2026-11-02

### 2026-11-02 · 1d6375b61a05 · #9322 · @nestorcolt

**Hold a tick step whose earlier run is still alive**

Previously, if a tick step ran past its time budget, the next pass could start that same step again while the old run was still going, which for a step like placement meant it could spawn twice. The engine now tracks each step's running thread by name under a lock; if an earlier run is still alive when the next pass starts, it returns a new `held` outcome instead of starting a second thread, and resumes normal runs once the earlier one finishes. This closes a real race that let a slow step overlap itself across passes.

## v2.17.572 · 2026-11-02

### 2026-11-02 · e8753f43f9f9 · #9321 · @agenticore-app

**Refresh the hive ready marker from the headroom thread during long beats**

This change fixes a readiness bug where a long agent launch inside the daemon beat could leave the hive ready marker stale past its sixty-second freshness window, causing rollout readiness checks to fail even while the hive was actually serving. Now the headroom publish path calls a new refresh_ready function after each successful publish, touching the marker independently of the launch step, so it stays fresh regardless of how long dispatch.poll takes. It only touches the file once it already exists, preserving the original behavior that readiness starts on this process's first successful beat.

## v2.17.570 · 2026-11-02

### 2026-11-02 · 085fdfa8e901 · #9316 · @agenticore-app

**Document measured tick budgets and spawn prerequisites (Refs #9285)**

This change adds reference documentation for the tick engine's step budgets and intervals delivered in #9278: a new `docs/reference/tick-engine-steps.md` explains how each step's budget is derived from measured p95 latency plus headroom, lists the declared intervals (60, 120, 600 seconds), gives configuration examples, describes late-worker and overlap behavior, and documents spawn's prerequisites and the forty-second bound, plus troubleshooting guidance. The reference index and swarm pillar doc are updated to link to it. This matters because it makes the engine's timing guarantees—especially that spawn is never starved by slower steps—visible and verifiable to operators and future contributors.

## v2.17.569 · 2026-11-02

### 2026-11-02 · 6644444ff98a · #9278 · @nestorcolt

**Bound each tick step by its measured budget so spawn starts by forty seconds**

This change gives every registered tick step a measured time budget (derived from real p95 run times, with intervals from the plan table) and runs each step in an isolated daemon thread capped at that budget, so a slow or hung step gets recorded as late or abandoned instead of blocking the pass. Spawn's needs are trimmed to just judge, orphans, and preempt, guaranteeing it starts within forty seconds even if an unrelated step runs long. This closes issue 9220 and ensures spawn can no longer be starved by slow steps, unblocking stage two of the rollout.

## v2.17.566 · 2026-11-01

### 2026-11-01 · 67fb3d888da3 · #9299 · @agenticore-app

**Remind seated sessions to read seat notes on every user prompt**

This change adds an automatic reminder on every user prompt in a session bound to a swarm seat, so lane agents (eng, ci, master, planner, dispatcher) see a one-line nudge naming their swarm's learned notes command before making any tool calls. It runs as a UserPromptSubmit step right after operator steps, routed through the shared context-injection path so it works consistently across Claude, Codex and Copilot. This prevents wasted tool calls re-deriving context already captured in seat notes, and new tests confirm seated sessions get the line while unseated ones don't, across all three targets.

## v2.17.563 · 2026-11-01

### 2026-11-01 · 6cd258b2c2ec · #9273 · @nestorcolt

**Hold spawning when tick pass p95 exceeds the stage budget**

This change pauses new worker spawning whenever the p95 of the last ten completed tick passes exceeds the rollout stage's time budget (S1 384s, S2 240s, S3 120s, S4 60s), keeping this hold separate from transient host holds so each is tracked and cleared independently. It notifies the master once per breach with retry-safe delivery and automatically lifts the hold once tick performance recovers, preventing runaway spawning during slow rollout stages while avoiding noisy or duplicate alerts.

## v2.17.562 · 2026-11-01

### 2026-11-01 · 55f90c828b8a · #9277 · @agenticore-app

**Document per record fencing keys and the fenced batch**

This change adds a new reference page documenting how per-record fencing keys and the atomic fenced batch work, covering the Redis key layout, the Lua script's lease/epoch/revision checks, how fencing.write translates changes into puts, deletes, and reads with retries, the one-time migration path, inspection commands, measured bytes per change, and known limitations. It links this page from the reference index and the swarm-v2 execution-identity doc. This matters because it gives operators a verified, checked-against-source explanation of behavior already shipped in PR #9249, closing the documentation gap tracked in issue #9276.

## v2.17.561 · 2026-11-01

### 2026-11-01 · bc83baf076c5 · #9271 · @nestorcolt

**List open pull requests through the source control port in fleet red**

Fleet red's open-pull-request check now goes through the source control port instead of shelling out to `gh pr list`. The port adds a new read tool, `open_changes`, which the GitHub adapter answers with a single GraphQL query (capped at 200 PRs, matching the old limit) covering each PR's newest-commit rollup checks, and which the fake answers for tests. Fleet red uses this with the same 60 second deadline and unchanged red-check rule, surfacing stack port errors instead of the old `ReadError`, and drops off the Semgrep source control allowlist, leaving six callers still pending the same migration.

## v2.17.560 · 2026-11-01

### 2026-11-01 · e6d19c945d9c · #9272 · @agenticore-app

**Restore coverage of competing fencing document creation**

This change adds a test case to fencing.read that re-establishes coverage lost during a previous push: it verifies that when the initial document creation loses a race to a competing writer, the code correctly recovers from the resulting StateConflict and returns the competing document unchanged rather than overwriting it. No production logic changed, only test coverage was restored to satisfy the coverage-ratchet check that previously flagged two untested lines, confirmed via mutation testing showing the touched lines are properly exercised.

## v2.17.556 · 2026-11-01

### 2026-11-01 · f6d70a31a48b · #9211 · @nestorcolt

**Run the tick's start and observation steps as registered engine steps**

This change refactors the tick's start and observation logic out of hand-written inline calls into formally registered engine steps, moving each step body verbatim into dedicated `step_*` functions organized under `scripts.tick.steps.observe`, with an adapter that passes parameters by name and preserves legacy execution order and exception propagation. It matters because it makes these steps consistent with the engine's step framework, improving testability and traceability while keeping behavior identical to the previous implementation, as verified by tick equivalence tests and a planted-fault regression test that confirms failures are still caught correctly.

## v2.17.554 · 2026-11-01

### 2026-11-01 · 0a840aea3e0d · #9246 · @nestorcolt

**Hand the tick wrapper steps after fencing the fenced runtime**

This fix closes a regression from pull request 9225: after the fencing step ran, the wrapper's pass state was still carrying the pre-fencing runtime, so later steps—ledger watchdog, ledger probe, reboot pass, and the event/metrics steps—were operating on the unfenced runtime and ledger instead of the fenced one. The change updates the pass state to take the fenced runtime with the fenced ledger, ensuring all downstream steps see consistent, properly fenced state rather than silently reverting to pre-fence data.

## v2.17.550 · 2026-11-01

### 2026-11-01 · 6b86ac25c522 · #9236 · @agenticore-app

**Document the tick ledger view and export parts**

This change adds the documentation for the Tick ledger view feature delivered in #9224, giving operators a clear reference for how the system avoids rereading the whole ledger on every pass. It covers the stored view file location under the scheduler runtime folder, the refresh table and its full-read fallback conditions, the HTTP 400 skip behavior, and the export parts request/reply shapes including the 256 generation change window. It also adds a check script, troubleshooting guidance, tests, a reference index entry, and a changelog fragment, so the delivered behavior is now fully documented and verifiable against merged dev.

### 2026-11-01 · c5f6822cae33 · #9231 · @nestorcolt

**Add run listing and run cancel port tools and move the panic cancel onto the port**

This change extends the source control port with `latest_runs` and `cancel_run` tools, letting the GitHub adapter fetch and cancel workflow runs through audited, deadline-bound calls rather than raw `gh run list`/`gh run cancel` commands. Panic's fleet-wide cancel logic (`cancel_runs`, `tick_pass`, and the panic button) now routes through this single port per repository under a sixty-second deadline, with matching fake support and effect-registry entries. This shrinks the shell-call allowlist further, keeps GitHub access auditable and testable, and moves the project closer to fully eliminating hardcoded GitHub CLI usage outside the adapter.

## v2.17.549 · 2026-11-01

### 2026-11-01 · 866f11b0e334 · #9213 · @nestorcolt

**Run the tick's master and dispatcher seat steps as registered engine steps**

This change converts the tick's master and dispatcher seat logic—eleven hand written calls spanning masters, master seats, dispatch seat, and master watch—into registered engine steps defined in a new `scripts/tick/steps/masters.py` module, with each tick site reduced to a single extend call that runs the group and surfaces exceptions properly. This shrinks the tick module's complexity (type baseline drops from 3496 to 3490), keeps execution order and behavior identical as verified by tick equivalence and planted-fault tests, and sets up a clean path for a follow up to remove the temporary untyped `SEAT` namespace once the seat modules are fully typed.

### 2026-11-01 · cb15ee2bea3d · #9224 · @nestorcolt

**Patch a stored tick ledger view from one export read of named parts**

Each tick pass now patches a cached ledger view (tasks, metadata, phases) with a single delta export since its last generation, instead of re-reading the whole ledger every time. The export route gains an optional `parts` filter so callers fetch only what they need, and the view logic falls back to a full read only when the server's change window no longer covers the gap. This cuts CPU and contention on large ledgers, letting parallel waves run without colliding, and unblocks the next stage of the rollout; servers predating this change degrade gracefully with a stderr warning rather than failing.

### 2026-11-01 · e9a97ee92519 · #9232 · @agenticore-app

**Document the empty registry tick integration contract**

This change updates the tick adapter documentation to fully describe the empty registry engine contract delivered in #9201: the seven ordered group call sites (start, observe, agents, masters, tasks, place, close), the wrapper-before-and-completion-only-after pattern, fresh empty views per group, and the independent 600 second deadline each group gets. It also adds internal usage notes, configuration limits, early-return behavior, troubleshooting guidance, and the golden equivalence evidence proving byte-identical output with no steps registered, while explicitly scoping that empty-registry guarantee to this delivery so future work registering real steps isn't mistakenly assumed to preserve it.

## v2.17.548 · 2026-11-01

### 2026-11-01 · 2ee703361052 · #9221 · @nestorcolt

**Read renewed Codex tokens in balance like the Claude rows**

This change fixes `agentihooks balance` so Codex account rows pick up freshly renewed login tokens instead of a stale shell environment, matching how Claude rows already work; previously, logging into a Codex account in a new shell wouldn't show up until the old shell was closed, now it correctly reports status like OPEN, Pro, 83 percent of the week immediately. It also resolves a type-checking regression in the tick module caused by a prior refactor, suppressing now-irrelevant unused-function and unused-import warnings and lowering the strict type count baseline so pull requests from dev stop failing the type gate.

## v2.17.547 · 2026-11-01

### 2026-11-01 · fcfc09affd1d · #9217 · @agenticore-app

**Confirm the frozen controller termination from the container runtime in kind smoke**

This fixes an intermittent kind smoke failure where, after the liveness probe restarted a frozen controller, Kubernetes sometimes reports a new container with an empty `lastState`, so the script couldn't confirm the old container's termination. `kind-smoke.sh` now polls for up to a minute, falling back to the kind node's container runtime (`crictl ps -a`) to check if the old container exited or was removed, before releasing the tick lock. The failure message also now reports the current id, last terminated id, and runtime state, making future diagnosis faster and reducing flaky red runs on the helm-kind smoke job.

## v2.17.544 · 2026-11-01

### 2026-11-01 · e79ef3b830de · #9196 · @nestorcolt

**Build every swarm gh command through the GitHub adapter and ban the builder outside it**

This change routes every swarm gh command through the GitHub adapter's command builder, and adds a Semgrep rule that forbids using that builder anywhere outside the adapter, with a shrinking allowlist covering thirteen caller files plus the adapter itself. Publish's merged-branch read and the panic fix's branch read now go through the source control port, so an unreadable fix branch correctly reports a failed panic cancel. Health checks still use gh pr checks pending commit-status support. This moves the project closer to eliminating hardcoded GitHub calls, per the adapter gate task.

## v2.17.542 · 2026-11-01

### 2026-11-01 · ea63bbb18ad8 · #9194 · @agenticore-app

**Document the tick source control port and the GitHub adapter**

This change adds documentation for the source control port and its GitHub adapter introduced in PR #9166, covering the provider-neutral records, the nine operations split by read/write effects, and how GitHubSourceControl handles audited gh calls, timeouts, paging, merge queue behavior, tag listing via git ls-remote, and error handling. It also documents the first consumer (swarm stack change used by park and publish), the FakeSourceControl test defaults, and adds troubleshooting entries, a reference index row, and a changelog fragment, giving operators a clear map of how source control effects are declared and swapped between providers.

## v2.17.540 · 2026-11-01

### 2026-11-01 · ef39ec8cea30 · #9188 · @agenticore-app

**Document the tick runtime and processes adapters**

This change updates the tick adapters reference documentation to cover behavior already shipped in PR #9161, describing how RouterRuntime handles arguments, operations, unknown handles, launch request keys, outcome passthrough, and single deadline checks, plus how PassProcesses performs scan and rescan so each tick does exactly one process scan shared across steps rather than a scan per step. It adds the declared effects rows, fakes, troubleshooting entries, index entry, and a changelog fragment, giving operators and contributors an accurate, trustworthy reference for how these adapters actually behave rather than leaving the delivered seam undocumented.

## v2.17.539 · 2026-11-01

### 2026-11-01 · dc405c20089b · #9183 · @agenticore-app

**Retry worker image tool downloads on a cut response body**

This change fixes a worker-image build failure where tool downloads would die on a cut response body, raising http.client.IncompleteRead, which the fetch retry loop didn't treat as transient. Now fetch also retries IncompleteRead and ConnectionResetError using the same five-attempt backoff already applied to 5xx responses and timeouts, while permanent HTTP errors, DNS failures, and checksum mismatches still fail immediately. This should prevent flaky build breaks caused by truncated downloads over unreliable connections.

## v2.17.538 · 2026-11-01

### 2026-11-01 · 34a2f997caf3 · #9181 · @agenticore-app

**Document the inbox, llm, metrics and host tick adapters**

This change adds a new reference page documenting the tick adapters introduced earlier (StoreInbox, ApiLlm, SpoolMetrics, JobHost, and local_host), covering their operations, return values, errors, declared effects via Tools.for_effects, their fakes, and the import check that keeps tick steps from reaching into the adapted modules directly, plus troubleshooting guidance. It also adds an index row and a changelog fragment. It's documentation only, verified by running the page's own examples against current code, so it carries no runtime risk but gives operators and contributors a clear, tested reference for how these adapters behave and fail.

## v2.17.535 · 2026-11-01

### 2026-11-01 · 0242bb8836e7 · #9153 · @nestorcolt

**Record bounded tick costs and report pass gaps**

This change adds durable visibility into tick performance by recording one finished entry per tick step plus one pass summary per run, each written into capped per-swarm Redis streams (bounded to 50,000 records) so history doesn't grow unbounded. A new CLI command, agentihooks swarm SLUG tick-report, surfaces this data as per-step counts, median and p95 latency, CPU share, and pass-level p95 and gaps, making it possible to see exactly where time and CPU go on the live tick and catch regressions before they spread, which is the foundation the rest of the measured-and-guarded phase depends on.

### 2026-11-01 · 11e4d6f52763 · #9150 · @agenticore-app

**Replay tick golden scenarios in an advisory equivalence job**

This adds a new workflow-dispatch-only CI job that replays all twenty four canonical tick golden scenarios concurrently on a chosen base and head, comparing their outputs to prove equivalence before any phase B move pull request merges, and verifies the check actually catches regressions by failing outward when a reap action fault is planted. It runs independently of the existing frozen ledger equivalence workflow and required gates, so nothing already protecting merges is touched, while giving every future move PR a concrete, attachable proof run id showing base and head behave identically.

## v2.17.534 · 2026-11-01

### 2026-11-01 · 0343ad72fa2c · #9165 · @nestorcolt

**Read externalized archive history in fenced execution lookups**

This change fixes a reader gap left by the fencing archive migration: `TaskAuthority._execution` and `OperationJournal._current` were still looking for execution rows only in the raw document, so archived executions wrongly fell into the "unknown identity" branch instead of being recognized as stale. A new shared helper, `fencing.execution_row`, now checks current rows, then the inline archive, then the external archive history, without writing back to the document, and both readers use it. This closes the last gap exposed by the archive migration, keeping task authority admission and the operation journal correct for archived executions while leaving the stored document shape unchanged.

## v2.17.532 · 2026-11-01

### 2026-11-01 · dfdddf09cd01 · #9163 · @nestorcolt

**Add inbox, llm, metrics and host tick adapters behind deadline ports**

This change wires the inbox store, LLM classifier, metrics spool, and host jobs behind the tick system's deadline-aware ports, so tick steps call through uniform adapters instead of touching those implementations directly. Each adapter rejects calls once its deadline has passed, and only the classifier call passes along remaining time to its underlying HTTP timeout. An AST-based test enforces that no tick step module bypasses these adapters with direct or relative imports, with a planted fault proving the check works. Wiring the adapters into the actual tick composition root is left as follow-up work.

## v2.17.531 · 2026-11-01

### 2026-11-01 · 83dbf1e5bd46 · #9157 · @nestorcolt

**fix: make agentienv's multiline check shell-neutral so zsh can source the block**

This fix replaces the multiline-value check in the agentienv rc block, which previously relied on bash-only features (`compgen` and `${!name}`), with a portable awk one-liner that scans exported environment variables for embedded newlines. The old check broke on macOS's default zsh shell, causing a "command not found" error when sourcing the block, as seen in the Smith matrix run against v2.17.527. The new approach works identically under bash and zsh, keeps the existing filtering and warning logic intact, and never exposes value text outside awk. New tests confirm the check is shell-neutral and that zsh can source it without errors.

## v2.17.530 · 2026-11-01

### 2026-11-01 · 6f5b56f55f75 · #9149 · @nestorcolt

**Open Claude seats from the week when no five hour window is reported**

This change fixes a seat-allocation bug introduced after Claude dropped the five-hour window from Max account reporting: previously, accounts lacking that window were treated as unknown and skipped, silently starving the fleet of seats and leaving tonight's fifteen-agent tick plan far short of capacity. Now, session cap, routing, account state, and the session quota policy fall back to opening seats from the current week when no five-hour window is reported, while still honoring explicit rejections and respecting real reported bands when present, restoring full seat availability.

## v2.17.529 · 2026-11-01

### 2026-11-01 · 07e0adfc1e53 · #9155 · @agenticore-app

**Define tick ports and enforce declared tool effects**

This change establishes nine typed external ports for tick steps, each with mandatory deadlines, so every outside system interaction goes through a well-defined, swappable contract rather than ad hoc calls. Unsupported capabilities now return explicit answers instead of silent failures, and Tools enforce declared effects by refusing writes outside their stated scope while still allowing reads. Recording fakes cover all ports for deterministic testing, with fake_tools able to mirror the same effect restrictions. This locks in the contract that dependent engine and adapter work will build on, catching undeclared-effect misuse early through tests rather than at runtime in production.

## v2.17.528 · 2026-11-01

### 2026-10-31 · 1e6ecd36ab8a · #9135 · @agenticore-app

**Define tick step contracts and validate dependency waves**

This change introduces typed tick step contracts (Step, Domain, Lane, Context, Write, Result) plus a Registry that validates steps before they can run: it rejects duplicate names, unknown or cyclic dependencies, and same-lane steps writing a shared domain without an ordered dependency path, then computes safe execution waves respecting order and registration sequence. A Deadline type with clamped remaining time and non-extending child deadlines is also added. This establishes a guarded boundary for scheduling future tick execution safely; wiring the actual engine execution is deferred to a later change.

### 2026-10-31 · 518902c206e5 · #9142 · @agenticore-app

**Characterise swarm tick passes with canonical goldens**

This change adds a safety net ahead of upcoming structural changes to the swarm tick logic: it introduces twelve scenarios run through both tick entry points, using fake Redis, ledger, runtime and Herdr, and commits canonical JSON goldens of the resulting actions and state writes. A new feature entry registers baseline and dropped-reap-action checks, and an exported output lets other tests compare against the same goldens. This ensures any future refactor that silently drops an action, like removing the first reap, is caught immediately rather than discovered later.

## v2.17.526 · 2026-10-31

### 2026-10-31 · 56a7b44d795b · #9116 · @agenticore-app

**Keep workstation and hive pinned tasks off events hive offers**

This change fixes a bug in the events engine where tasks marked as workstation-only, hive-pinned, or needs-pinned could be offered to, claimed by, or admitted onto hives that could never run them, leaving orphaned claims with no agent. Now offer generation, spoke takes, and hub admission all honor these three flags consistently, and the workstation tick itself handles launching workstation tasks under events. This prevents stuck claims and ensures tasks land only where they're actually runnable, matching the legacy engine's behavior.

### 2026-10-31 · db667f7fb6ed · #9130 · @agenticore-app

**Bound Redis state history and trim old document copies**

This change trims Redis document history retention from 64 rows down to just the current document and one prior revision, reducing memory usage significantly. A maintenance routine now safely removes excess old entries using atomic single-row deletions and a resumable cursor, processing up to 16 entries per run by default (max 128), so operators can repeatedly invoke it until both cursor and removed count reach zero. This matters because unchecked history growth was straining Redis memory, and the fix is irreversible for discarded revisions, so verify backups or acceptable data loss before running cleanup broadly.

### 2026-10-31 · bc66b2def2d2 · #9123 · @nestorcolt

**Reap process timers abandoned for an hour so scheduler documents stop growing**

This change fixes scheduler documents growing unbounded because failed `process_timer` starts or retires under write contention left orphaned entries behind, which bloated the shared entries document, slowed every write under the bucket-wide WATCH, and caused the observed scheduler delivery lag. Now a busy `Scheduler.run_due` pass also reaps `process`-owned timers that are finished or never started once they're an hour or more past due, using the same removal path as normal retirement, so the document stops accumulating leaked entries and write latency stays bounded.

### 2026-10-31 · d96138f5b397 · #9118 · @agenticore-app

**Open the inbox seat bus off the ledger request path behind one lock**

This change fixes a ledger stall: the inbox seat publisher used to open its NATS connection inside the ledger request thread with no lock, so a down bus made every write wait out the full connect timeout, and concurrent writers each started their own connect. Now a single background daemon thread opens the bus under one lock; only the triggering sender waits, capped at 0.25s, and others return immediately. If the bus isn't open, the event drops and relies on the existing Redis wake-outbox replay to deliver it later. Ledger writes now stay fast even when the bus is down.

## v2.17.525 · 2026-10-31

### 2026-10-31 · 37ee75d4c899 · #9033 · @nestorcolt

**Select pull request tests by the files they read as data**

This change closes a gap in pull request test selection by tracking not just files a test imports, but files it reads as plain data, since those reads previously went unnoticed and caused real failures to slip through unselected (29 merge groups, 35 failing tests over the week studied). The coverage recorder now distinguishes genuine imports from data opens using call-frame heuristics, `ci_select` pulls in every test whose recorded run touched a changed file, and manifest-based reuse checks now consult these recorded reads before falling back to source-unit comparisons, tightening selection accuracy without over-selecting on shared files everyone reads.

## v2.17.524 · 2026-10-31

### 2026-10-31 · 4d0d1fc2e730 · #9049 · @nestorcolt

**Read coverage ratchet base sources through one git cat-file batch**

This change replaces the coverage ratchet's per-path `git show` reads with a single long-lived `git cat-file --batch` process that serves every source lookup for a revision, and has `renamed` build one source per revision instead of one per path. Output stays byte-for-byte identical to the old reader, but it cuts process spawning dramatically: a full-source probe dropped from 11.98s to 1.38s, and the dev test shard's wall time fell from 26.5s to 22.6s, so CI's "hold every line the base ran" step should get noticeably faster without changing results.

## v2.17.523 · 2026-10-31

### 2026-10-31 · 5b9f5adc6266 · #9113 · @nestorcolt

**Index tasks and agents once per metrics row builder instead of scanning per row**

The metrics row builders in scripts.swarm.metrics_swarm were scanning the full agent history on every row to find a matching task or owning agent, which made them scale linearly with history size; on rig-grade-swarm this pushed metrics._collect to about 85-95 s and caused run_tick to brush against its 450 s deadline, missing it twice. This change adds one-time indexes (_Tasks, _Owners) built per builder instead of per row, cutting measured runtimes from seconds down to tens of milliseconds while producing byte-identical output, verified against live inputs and a new test bounding per-record reads.

## v2.17.518 · 2026-10-30

### 2026-10-30 · 03e07b5c3dca · #8990 · @agenticore-app

**Clear comments in the hook integrations**

This change strips all non-directive comments from the nine hooks/integrations modules, trimming module docstrings and folding constraint notes that were previously left as comments directly into docstrings for the affected classes and functions, including a stale, inaccurate comment about Postgres insert behavior that was simply dropped rather than preserved. It adds these modules to the comment-free allowlist and shrinks the size allowlist now that dynamodb.main fits the standard limit, and adds targeted tests covering lines whose comments were removed so coverage doesn't regress, with no behavioral or logic changes verified by AST comparison against the prior version.

## v2.17.517 · 2026-10-30

### 2026-10-30 · ae3aafaf487e · #8976 · @agenticore-app

**Watch hub admitted agents on another hive as remote in the tick**

This change fixes a false-positive loss detection in the tick's reap pass: agent records that name a remote hive but haven't yet received a runtime target (as written during hub admission) were wrongly treated as local, so any hive whose startup took longer than the six-minute grace period got marked lost and had its task reopened prematurely. Now such records are treated as remote and held as suspect until their runtime responds, deferring actual loss detection to the hub reconciler's presence check. Records with any runtime target, including local Codex launches, are unaffected and keep their prior behavior.

### 2026-10-30 · a730655d1f5c · #8961 · @agenticore-app

**Split heavy test files across the selected unit legs**

This change fixes uneven CI leg timing by letting `scripts.ci_select.legs_for` break up large test files into their top-level tests or classes before distributing work across unit legs, instead of assigning whole files as indivisible units. Previously, a single heavy file like `tests/test_gate_protected.py` could monopolize one leg (507 stored seconds, causing a 210s vs 138s runtime gap). Now such files split safely—skipping anything pytest can't collect as a standalone node—so the same selection spreads across six legs at roughly 150-152 seconds each, closing the timing gap and keeping CI runs more balanced and predictable.

### 2026-10-30 · 95ea2ea0c22f · #8975 · @agenticore-app

**Inventory the messaging and ledger features in the feature map**

This change adds 54 new inventory entries to feature-map.json covering messaging (inbox, masters channel, ledger reads/writes, artifacts, plans) and the page UI, then tags 161 existing tests with matching feature markers so they trace back to these entries, with no changes to test logic itself. This closes out another slice of the broader feature-mapping effort, improving traceability between code behavior and test coverage. Some tests using unconventional TestCase naming can't be mapped yet and are tracked separately; feature-map validation and lint checks pass cleanly on the final commit.

### 2026-10-30 · 227e1ae5af8e · #8973 · @agenticore-app

**Allow Stop after an accepted swarm handoff**

This change fixes a gap where a finished predecessor task could still be hit with Stop escalation even after its handoff to a successor had been accepted, letting repeated stops potentially block progress while the successor kept working. ClaimStop now checks whether a matching retained handoff transfer exists and, if so, clears the predecessor's refusal streak before any task reading or mutation occurs, so it no longer gets penalized. Normal workers and unrelated transfers are unaffected and continue to be enforced as before, keeping the fix narrowly scoped to the handoff-acceptance case.

### 2026-10-30 · 17d95ffc15ed · #8971 · @agenticore-app

**Allow blocker escalation when the critic has no session capacity**

Previously, if every eligible Claude account was at session capacity, the blind critic couldn't launch, and the blocker protocol would stall the engineer indefinitely since no critic verdict could be produced. This change treats that specific routing refusal as valid critic evidence, letting the engineer escalate with a blocker reason and proceed rather than getting stuck. All other critic failure modes still require the critic step as before, so this only loosens the rule for the capacity-exhaustion case, unblocking engineers without weakening the broader review requirement.

### 2026-10-30 · 887c0bba9345 · #8912 · @agenticore-app

**Issue and check swarm credentials in an auth service with signed tokens**

This change centralizes credential issuing and checking into a single auth service that signs short lived tokens, instead of letting every service inspect shared member maps directly. Agent, ledger, controller, tick, Cloudflare Access, and agent credentials now all route through this service, each backed by its own independent record, while the ledger page and other services simply verify tokens locally with a shared key. It also adds a Helm deployment for the auth service and tests covering agent authentication even when the ledger page is down, closing issue #8848 and tightening credential isolation across the swarm.

## v2.17.515 · 2026-10-30

### 2026-10-30 · 7aeca1153cb1 · #8958 · @agenticore-app

**Start the writer lock ledger Postgres from the already pulled service image**

This change makes the writer-lock ledger's Postgres container start from the pgvector/pgvector:0.8.0-pg16 image that the job's service container already pulls, using --pull never, instead of retrying a separate pull. It now starts before checkout so readiness checks overlap with setup rather than blocking afterward. This cuts the slow, flaky manifest-inspect-and-pull step (around 23–37 seconds) down to about 1 second, shaving roughly 25–37 seconds off each run and making the writer-lock scenario job noticeably faster and more reliable.

### 2026-10-30 · 4b6b5475b7b6 · #8922 · @agenticore-app

**Allow ledger joins past queued talk and refresh launch evidence**

This change lets a solo authorized ledger join go through to the server even while other ledger talk is still queued, including talk tied to a different ledger, instead of being blocked behind it; normal write ordering is unaffected. It also refreshes join evidence that was missing before the system evaluates pending launches, so launch decisions are made on current data rather than stale records. The existing launch window rules for rejecting late or absent joins are unchanged, and durable join acceptance during a genuine outage remains separate follow-up work, tracked under the still-open parent issue.

### 2026-10-30 · 52d97c022718 · #8952 · @agenticore-app

**Keep moved tasks on their pull request delivery branch**

This change fixes a bug where a task moved between hosts could get stuck because leftover branch records from its old host conflicted with its current one, causing publication to reject the delivery branch. Now, when such a conflict is detected, the system resolves it by checking the open pull request's head branch for the current repository and using that, only falling back to the existing selection if no matching open pull request is found. This prevents moved tasks from losing their correct delivery branch and avoids publication failures caused by stale branch mappings.

### 2026-10-30 · 6cf536b17568 · #8938 · @agenticore-app

**Comment gate for shell scripts, ledger JavaScript and style sheets**

The comment gate now covers shell scripts, the ledger's front-end JavaScript, and its style sheets, using pinned tree-sitter grammars instead of regex so constructs like `${x#y}` or `//` inside strings aren't mistaken for comments. Shell allows only shebang, one header block, and shellcheck directives; JavaScript allows a header, doc blocks before functions/classes, and eslint/ts directives; CSS allows one header. The ten stray comments inside ledger JavaScript catch blocks were removed and those blocks given real logic to satisfy SonarJS S108. New planted-fault tests per language confirm the parser isn't fooled, closing out this enforcement task.

## v2.17.514 · 2026-10-30

### 2026-10-30 · f5e69c4d3abf · #8953 · @agenticore-app

**Hold mapped feature tests against disabling and unrecorded removal**

This change strengthens the feature map validator so mapped tests can no longer be quietly disabled or deleted. It now reuses the shared disabling checks to reject skipped, xfailed, skipif'd, or early-returning mapped tests, and introduces a `retired` ledger so removing a feature or its tests requires an explicit, non-reusable retirement record checked against the base. The validator runs from the protected grader copy (with `--base`) so a pull request cannot loosen its own gate. Planted red and green CI runs confirm each rule triggers and passes correctly, closing a gap where feature coverage could silently erode.

## v2.17.513 · 2026-10-30

### 2026-10-30 · 1a9b9eb12705 · #8948 · @agenticore-app

**Publish the release chart only after kind installs the packaged archive**

This change fixes a release ordering bug where the chart was pushed to GHCR before its kind install was verified. The workflow now runs package, then kind (installing the exact packaged archive via a new chart-artifact input and CHART_DIR mode), then publish, which depends on kind and has no bypass condition. A contract test locks in these dependencies and artifact wiring so the bug can't silently return. This matters because it guarantees users only receive chart releases that have actually been proven to install successfully in kind, preventing broken charts from reaching production.

## v2.17.511 · 2026-10-30

### 2026-10-30 · 3fc9e4cd061c · #8937 · @agenticore-app

**Provision hive brain binding and credential references**

This change fixes hive engineers starting without brain tools by introducing optional `workers.brainBinding` chart settings that mount an existing binding Secret and pass through credential Secret key references. Worker startup now installs the projected binding document as a private, owned regular file before profile rendering, while the binding resolver keeps refusing to proceed when the document is missing. Validation confirms correct failure and success paths across startup, chart contracts, and kind integration. Full task completion still depends on the separate antoncore hive template mapping and the corresponding infrastructure provisioning and deployed proof, tracked under #8932 and #8936.

### 2026-10-30 · 9116367b0b63 · #8931 · @agenticore-app

**Read large inbox mailboxes in bounded Redis pages**

This change fixes an unbounded Redis range read that caused connection resets on oversized mailboxes by fetching message identifiers in bounded pages of 500 before the existing row hydration step, while preserving message ordering, seat aggregation, and cleanup of dangling records. It directly resolves the reproduced failure where large inboxes triggered a connection reset, and the fix is backed by focused proofs covering paging, row batches, dangling indexes, CLI behavior, and seat handling, plus a clean mutation report, so operators should see large mailbox reads succeed reliably without behavioral regressions.

### 2026-10-30 · 34c9ef125d5a · #8939 · @agenticore-app

**Prove in kind that a rolled hive waits for its agent's handoff**

This adds a kind smoke proof confirming that when a worker hive is rolled with `workers.rollout.enabled`, the old hive's container doesn't stop until its agent records its own handoff. A scripted agent and rollout lease checks verify the preStop hook actually waits for drain, the lease stays held by the release, and the replacement serves afterward, running alongside the existing controller lease proofs. It was validated green against the real preStop logic and confirmed red when a planted change made drain give up immediately, catching the bug via a mismatched handoff reason instead of the expected recycle-triggered one.

## v2.17.510 · 2026-10-30

### 2026-10-30 · d0df00db0692 · #8926 · @agenticore-app

**Inventory the swarm runtime features in the feature map**

This change adds 44 inventory entries to feature-map.json documenting the swarm runtime's internals: tick passes, locks, claim lifecycle, master seat behavior, handoff mechanics, recovery, health findings, and scaling controls. It tags 99 existing tests with feature markers linking them to these entries, without altering test logic. No live checks exist yet for these behaviors. Two files hit the project's line cap so their tests remain unmarked, and worker idle retirement is explicitly deferred to a follow-up. This keeps the feature map accurate as a reference for what's tested versus what still needs coverage.

## v2.17.508 · 2026-10-30

### 2026-10-30 · 695a52daf0cb · #8920 · @agenticore-app

**Feature suite harness on real services and scripted agents**

This adds a feature-suite harness that exercises real Redis, Postgres, and a live ledger server instead of the in-process fakes, launching a scripted agent process that joins the ledger, beats its canary, and hands off a document so the full launch-validate-handoff path is tested end to end. A new GitHub Actions workflow runs it with real service containers, gated behind an env flag so the existing unit suite and its Redis refusal stay untouched; this gives genuine confidence that the ledger, swarm, and agent lifecycle work together, not just against mocks.

## v2.17.507 · 2026-10-30

### 2026-10-30 · f616c3e518d4 · #8917 · @nestorcolt

**Scan account keys in pages of ten thousand in fleet_held**

This change fixes `accounts.fleet_held` to scan the `accounts:*` keyspace using a SCAN count of 10000, matching what `stored_accounts` already does, instead of redis-py's default page size of 10. The default was taking over 11,000 SCAN calls and 60+ seconds in the hosted controller pod, stalling the quota capacity read long enough that the liveness probe killed the controller before another proof swarm's lease expired. With count 10000, the same scan completes in about 0.58 seconds.

## v2.17.503 · 2026-10-30

### 2026-10-30 · 45ad94b4bfab · #8891 · @nestorcolt

**Use each master holder record in shared login home readers**

When a shared Claude login home is used by multiple masters, this change makes sure each joining master sees and validates their own rendered profile and overlays instead of whatever the creating master had, fixing a bug where launch checks, enforcement, status display, and hive profile/brain checks could use the wrong holder's data. It preserves each holder's own chain and overlay records, supports older overlay-only records, and falls back on render stamps when needed, so shared homes behave correctly regardless of which master set them up.

## v2.17.502 · 2026-10-30

### 2026-10-30 · 133ed086e8ea · #8879 · @agenticore-app

**Gate Python comments on changed lines with a growing comment free list**

This change adds a grader that blocks new Python comments and standalone string statements on changed lines, limits module docstrings to twenty lines, and tracks exempt files in a growing list at tests/COMMENT_FREE_PATHS.json; tool directives still work, and Semgrep annotations are only allowed in .semgrep* fixture folders. It runs in both the lint job and the prepush plan, with a `--all` option to check the whole tree, keeping comment hygiene enforced automatically as the codebase grows rather than relying on manual review.

## v2.17.500 · 2026-10-30

### 2026-10-30 · 8c756e0ae2d4 · #8847 · @agenticore-app

**Launch an admitted order on its admitted account without rereading the hive seat view**

This change fixes a routing bug where an admitted order could be bounced back for readmission if the hive's local seat view was stale, full, draining, or missing the account. Now `HerdrRuntime._route_account` sends an admitted order directly to its admitted account instead of rereading the hive's own quota view, so a stale local view can no longer override a controller decision. The controller remains the single source of truth for seat assignment, aggregating live hive reports and only placing work on hives that advertise the needed slot, which eliminates the readmission loop that was failing all ten cases in `test_admitted_quota_launch.py`.

### 2026-10-30 · 384eb72ebca1 · #8839 · @agenticore-app

**Reset request metrics journal state between swarm ledger tests**

This change fixes test isolation for request metrics by adding an autouse fixture in the swarm ledger conftest that resets journal state (path, held since, lost spans, pending rows, samples, evicted through) before each test, since the ledger server serve tests previously left this global state dirty for subsequent tests on the same worker. The ledger writer's serving fixture now stubs metrics_journal to match other serve tests, and a new ordered test pair verifies the fix, preventing flaky failures caused by leaked journal state between tests.

## v2.17.499 · 2026-10-30

### 2026-10-30 · 63db7370f00f · #8825 · @nestorcolt

**Recover telemetry exporters from scheduler failures**

This change fixes telemetry exporters that were being terminated by scheduler state conflicts, which could silently drop monitoring data. It now recovers from these conflicts instead of exiting, wraps the entire timer lifecycle within the polling timeout so setup and cleanup are properly bounded, preserves interruption state and prior alarms across restarts, and tags failure records with the exporting session for easier diagnosis. It also adds a Doctor rates report showing distinct telemetry stale incidents per hour, reporting unknown when collection history is missing, giving the operator visibility into recurrence before and after the fix is applied in production.

## v2.17.498 · 2026-10-30

### 2026-10-30 · d1b68b08aa13 · #8800 · @nestorcolt

**Keep a finished agent finished so a mid tick master handoff launches the successor**

This change fixes a race in `ExecutionRegistry._update` where a master agent's `finished` status could be overwritten back to `working` if a handoff landed mid-tick, between `_reap`'s read of the agent list and `_watch_idle`'s write-back. Because that stale write only compared identity fields, it silently reverted completed masters, preventing `_ended` from firing and blocking successor launches. Now a `finished` record stays finished unless the write itself ends the execution, guarded under the registry's lock so all tick paths are protected, with tests confirming successors spawn correctly on handoff.

### 2026-10-30 · fb2f01823752 · #8834 · @nestorcolt

**Read claims and clear fleet red hold marks in one Redis call each**

This change batches the fleet red notice pass so it reads claim state for every open unclaimed task with a single Redis MGET and clears all stale hold marks with one Redis DEL at the end of the pass, instead of issuing a separate GET and DEL per task. On rig-grade-swarm that per-task round-trip pattern was costing 76 to 80 seconds of every tick pass despite only 1.7 to 1.8 seconds of actual CPU work, so cutting it to two bulk calls removes most of that wall-clock time without altering which tasks get held, noticed, or cleared.

## v2.17.497 · 2026-10-30

### 2026-10-30 · a9db9ee548c2 · #8833 · @agenticore-app

**Add the service split design record**

This change adds docs/reference/service-split.md, the design record for task da1 of plan #8818, laying out how the ledger application splits into four services plus the agent WebSocket. It defines the contracts for the frontend, API, app server and auth service, maps every route, rule, side effect and auth path from the dev inventory to its target service, and lays out seven strangler steps with switches, rollbacks, resource budgets and the Compose developer build. This gives the team a verified, reviewed blueprint to safely execute the service split in phase one, closing #8827.

### 2026-10-30 · 5874938044ca · #8832 · @agenticore-app

**Accept allowlists registered in the head shrink only literal**

This change fixes a bug where newly added allowlist or baseline files registered in the same pull request were incorrectly flagged as unregistered by the protected threshold registry. Now, `registry_errors()` reads the head's `SHRINK_ONLY` literal via `constant()` to recognize fresh registrations, while still rejecting unregistered files, registered allowlists that grow, and registrations that only appear in `GRADED_BY`. This lets legitimate new allowlist additions land cleanly without fighting the gate, while preserving protection against unauthorized growth or bypass.

### 2026-10-30 · 610a3dde478b · #8829 · @agenticore-app

**Skip mutation lines in modules whose bare syntax tree did not change**

This change teaches `changes_since` to recognize when a file's edits only touched comments, docstrings, or plain function signature annotations without altering the actual logic, and skips generating mutation lines for those files in that case. It avoids wasted mutation-testing effort and noisy survivors on purely cosmetic diffs, while still fully grading any file that contains a real logic change. Exceptions remain for decorated functions, assignment annotations like dataclass fields, and functions invoked by reference at runtime, where annotations still matter and continue to be graded.

## v2.17.495 · 2026-10-30

### 2026-10-29 · 65249ec02f45 · #8816 · @nestorcolt

**Publish the hive worker once per release version**

This change fixes the hive worker release script so each version is published exactly once instead of being rebuilt and repushed on every rerun. If the version tag already exists in GHCR, the script now exits cleanly without pushing; if a prior run partially published (e.g. release tagged but not version), it retags from the already-built commit image instead of rebuilding. Any registry error other than "not found" still fails the run. This closes a gap where reruns or partial failures could cause wasted builds or inconsistent `:release`/version tags.

## v2.17.494 · 2026-10-29

### 2026-10-29 · 82bf4260e98f · #8810 · @nestorcolt

**Give hive launches the rest of an order-bound deadline for the profile canary**

This change fixes agent launches timing out before completing their startup canary: instead of a fixed 120s (Claude) or 90s (Codex) wait counted from route report, `init-agent` now waits for the rest of the actual launch deadline, bounded by a new `--launch-until` flag. `Consumer.launch` computes this from the order's expiry minus a 15s acknowledgement margin, converted to the hive's wall clock, so a late-starting launch no longer runs past its order's lifetime and gets wrongly marked expired. Docs were updated to match, and tests confirm queued orders under realistic timing no longer expire.

### 2026-10-29 · 71a1784e74ea · #8813 · @nestorcolt

**Write hive agent tokens without rewriting member maps**

This change stops hive agent token issuance from rewriting the entire member record, which previously meant touching tens of thousands of digests for a single new token. Instead, tokens now get their own small credential record carrying the current ledger digest plus exact scope, with a safety check that clears the new record if a revocation happens concurrently, so member contents and revision history stay intact. Revocation now writes tombstones for legacy-only records to keep old tokens properly invalidated under the new scheme. This reduces write amplification and risk during issuance while preserving compatibility with existing readers.

### 2026-10-29 · fb368cb4dac6 · #8806 · @nestorcolt

**Time the parts of agent record writes inside the tick**

This change adds fine-grained timing around agent record writes inside the tick, breaking down ExecutionRegistry.put and update into their transaction bodies and individual Redis pipeline commits, so we can see where the previously unexplained 4.3-second median write time actually goes. It's a diagnostic step toward the tick-pass speedup goal: since neither CPU contention nor Redis slowlog accounts for the slowness, these new step records should reveal whether the write-lock wait, retry commits, or something else is the real bottleneck. No behavior changes, just added visibility to guide the next optimization.

## v2.17.493 · 2026-10-29

### 2026-10-29 · 7551daf897c7 · #8787 · @nestorcolt

**Run a mutant's direct tests before the fallback cohort**

This change improves how mutation testing prioritizes which tests run first for each mutant: after the previously known killer, it now runs tests whose recorded coverage actually reaches the mutant's changed lines, before falling back to mutmut's default ordering. The planner records this mapping in a new direct-tests file, and the runner and KillMemory use it to rank tests accordingly. The payoff is substantial: a benchmark replay that previously finished only 217 of 225 shard units within the time budget now completes all 347 of 347, comfortably inside the limit, meaning more mutants get properly graded instead of timing out.

## v2.17.492 · 2026-10-29

### 2026-10-29 · c14df669b14a · #8807 · @nestorcolt

**Publish the release chart only after the hive worker image moved release**

This change fixes a race where the Helm chart was published to the registry before the hive worker image actually finished moving its `:release` tag, which could cause hives to roll onto a stale image. Now the chart release is triggered by the completion of the Swarm worker image workflow itself, only for successful release-triggered runs, and it builds from that run's exact tag and commit rather than the default branch head. The kind install step is also sequenced after the images it depends on exist, ensuring each release rolls hives exactly once onto the correct image.

## v2.17.488 · 2026-10-29

### 2026-10-29 · 09120043fa79 · #8796 · @nestorcolt

**Delete the coverage baseline's dead dependency manifest cache steps**

This change removes three now-dead steps in the coverage-baseline job that downloaded the `dep-manifest` artifact and saved it as a cache keyed by commit SHA, but only when a job it doesn't depend on succeeded — a condition that was never true on dev pushes, so the steps were always skipped and nothing ever restored that cache. Deleting them simplifies the workflow with no behavior change, and a test now pins that coverage-baseline carries no manifest step going forward.

## v2.17.487 · 2026-10-29

### 2026-10-29 · 1e512ef06b7b · #8790 · @nestorcolt

**Let the protected grader accept an announced dependency manifest gate need**

This change updates the protected grader so its base branch now accepts the dep-manifest entry in the Gate — Required needs inventory, mirroring the fix previously made for install-smoke in #7692. It exists specifically to unblock a follow-up pull request that adds the dep-manifest job to the gate, since that PR needs a base branch whose grader already recognizes this new need rather than flagging it as unexpected or missing.

## v2.17.486 · 2026-10-29

### 2026-10-29 · c0a1ee139fc7 · #8791 · @nestorcolt

**Document plan invalidation after task text rewrites**

This change updates the swarm guide to explain that rewriting a task's title or description invalidates any existing traced plan, forcing a fresh plan and a new intent plan judgment, while unchanged rewrites still reuse the prior verdict and incremental tracing continues to apply. It also documents gate configuration, upgrade recovery steps, and adds a home navigation link so operators can find this section quickly. Clarifying this behavior prevents confusion when task edits silently invalidate cached plans, and ensures operators know how to recover gracefully after upgrades without re-deriving context manually.

## v2.17.485 · 2026-10-29

### 2026-10-29 · b7f901945c65 · #8779 · @nestorcolt

**Select the locked tests manifest check for locked test and manifest changes**

This change updates the pull request test selector so that editing a locked test module or the locked tests manifest now triggers `tests/test_locked_tests.py` directly on that pull request, rather than waiting for the merge group to catch problems. Previously, a manifest that under-recorded assertions could slip through pull request checks since only the queue ran that test, even though the lint grader already caught over-recorded manifests. This closes the gap so stale or incomplete manifests fail fast on the originating pull request.

## v2.17.484 · 2026-10-29

### 2026-10-29 · b5afc17f17dd · #8764 · @agenticore-app

**Fail the ledger load gate at setup when a write preflight is refused**

This change adds a one-time write preflight check to the ledger load gate before it starts its timed measurement window. If that preflight request is refused, the gate stops immediately and reports the HTTP status and reason rather than proceeding into an invalid run. When the preflight succeeds, the timed window and its grading behave exactly as before, and the preflight write itself is excluded from the count. This closes a gap where the gate could start timing a load test even when writes were already being rejected, which would have produced misleading results.

## v2.17.483 · 2026-10-29

### 2026-10-29 · 8fe6ea0b6ec5 · #8775 · @nestorcolt

**Bound concurrent hive credential work in the ledger server**

Hosted ledger pods were OOM killed because every hive credential request spun up an unbounded thread decoding a member's full token map, each costing tens of megabytes; under swarm load dozens of concurrent requests exhausted the 4Gi pod limit. This change caps hive credential reads and token issues at four concurrent operations in the ledger server, with a five second wait before returning a 503 "busy" that clients already retry. It directly targets the memory spike measured on ledger-0 and leaves the underlying token-map storage work for later fixes.

### 2026-10-29 · ba6014fd4e71 · #8778 · @nestorcolt

**Skip consumed handoffs in Doctor reads**

This change fixes a race in the Doctor's handoff diagnostics where a successor consuming a pending handoff between separate reads could make the removed record look like an unattributed finding. It now fetches the document, seat, and envelope together in a single Redis MGET and skips any record whose document has disappeared, while still correctly flagging genuinely malformed records and preserving the successor-prompt reader. CI reproduced the original phantom finding and confirmed it's resolved, with reviews and regression tests passing.

## v2.17.480 · 2026-10-29

### 2026-10-29 · 0a112ea5e313 · #8738 · @agenticore-app

**Skip promotion red fixers for checks a newer dev run already passed**

This change makes the promotion step smarter about red-to-green test fixing by checking whether a more recent development run has already passed a given check; if so, the promotion red fixer skips re-fixing it, avoiding redundant work and wasted cycles chasing failures that newer runs have already resolved. It closes issue #8735 and ships as a tests-only commit, with the actual fix link to follow, reducing noise and saving compute by trusting fresher passing evidence over stale red results.

### 2026-10-29 · 4169a5862712 · #8707 · @agenticore-app

**Use the runner wheelhouse for selected shard setup**

Selected pull request shards now reuse the pinned uv installer and runner wheelhouse instead of restoring a 123 MB dependency cache, cutting a median 9-second step on self-hosted runners while keeping the same constraints, virtual environment, PyPI fallback, and test commands; hosted runners are unaffected and keep their existing cache path. A one-run pilot (40870061916) showed a 59-second test-to-job difference versus the 60-second target, but this doesn't replace the required ten-run after-merge validation, which is still pending alongside an unrelated, separately owned locked-manifest CI blocker.

## v2.17.479 · 2026-10-29

### 2026-10-29 · e00e4d101c45 · #8759 · @agenticore-app

**Index the threshold pins committed copy from HEAD's tree**

The `committed` module fixture was running `git add -A` over a 7168-file archive copy, which on busy dev shards blew past the 45s faulthandler and 60s thread timeouts and crashed worker gw3, forcing the rebound-pin test to pass only on retry. This change replaces that slow staging step with `git read-tree` pulling the index directly from HEAD's tree via an objects alternate on the source repo, with `threshold_pins.tracked` now relying solely on `git ls-files`. Verified runs show the suite completing in under 19s with zero reruns, eliminating the flaky retries entirely.

## v2.17.478 · 2026-10-29

### 2026-10-29 · 1959413b8276 · #8756 · @agenticore-app

**Upload each selected leg's coverage database once**

Each selected pull-request leg was uploading its coverage database to Sonar twice, once for branch coverage and again for shard coverage, wasting roughly five seconds per leg across 139 runs. The fix skips the shard upload whenever the branch upload already carries the same database, letting Sonar reuse the branch coverage download as that leg's shard coverage instead. Fork pull requests, other base branches, and legs lacking a branch record still upload shard coverage as before, so correctness is preserved while same-repo dev pull requests get faster, cheaper CI runs.

## v2.17.477 · 2026-10-29

### 2026-10-29 · 1a78d66d05b5 · #8708 · @agenticore-app

**Judge launched literals by command position in the local test guard**

This change fixes a false positive in the local test guard, which previously flagged any subprocess-launching script that contained a string literal matching a runner name, even in unrelated contexts like CI job labels such as "unit pytest shard." Now the guard only treats literals as runner invocations when they appear in an actual command position—at line start, after shell separators, wrappers, flags, assignments, or path prefixes like uv run or .venv/bin/—while still scanning non-literal code normally. This prevents legitimate scripts from being wrongly denied.

## v2.17.476 · 2026-10-29

### 2026-10-29 · ea708842345e · #8673 · @agenticore-app

**Invalidate cached plan verdicts after task text changes**

This change fixes a bug where editing a task's title or description could leave stale, now-invalid plan verdicts in place. Rewrites now properly invalidate cached intent plan checks and traced verdicts, while unchanged text and plans continue reusing their existing verdicts for efficiency. Incremental tracing re-checks the earlier prefix against the current task text, and the build gate reads from the same task-bound digest, closing five reproduced stale-verdict cases. Existing plans will need one fresh trace after upgrading to pick up this fix.

### 2026-10-29 · 1ff3c2be6de5 · #8749 · @nestorcolt

**Read independent hive token credentials before changing issuance**

This change updates `auth.agent_member` so it can read independent hive token records alongside the existing legacy two-field member scopes, checking exact ledger and agent scope and validating current ledger membership for revocation, while issuance logic itself remains untouched. This read-compatibility step must be deployed to every verifier before the upcoming writer change that actually migrates tokens to independent records, ensuring a safe rolling deployment without breaking verification mid-rollout. Tests confirm correctness, including mutant coverage and intended reader regression failures, with Standards and Spec readers both reconfirming pass status.

### 2026-10-29 · 03e16c0dcc8e · #8744 · @agenticore-app

**Map common ends of long repeated modules before diffing coverage lines**

The ratchet’s line_map was running Python’s difflib SequenceMatcher over entire files, and on files with thousands of identical lines its longest-match search scaled quadratically, pushing runs past the ten-second budget and causing a flaky test that only passed on retry. This change has line_map first identify and map the common prefix and suffix of the files directly, then run the matcher only on the remaining changed middle section, cutting measured runtime from several seconds (up to 11.58s under load) down to under a millisecond for the repeated-line case while keeping results correct and the budget intact.

## v2.17.473 · 2026-10-29

### 2026-10-29 · 1620412a540a · #8730 · @nestorcolt

**Time each read inside the tick idle watch**

This change wraps each individual read inside the tick's idle watch step with timing.timed, so instead of a single opaque 3.5-second span per agent, the tick step records separately name the refusal check, pane state write, outage handling, heartbeat waits, ledger probe hold, and similar reads, letting the slowest one be identified by its median duration. It matters because the reap step was burning roughly 3.5 seconds of wall time per agent on under 0.2 seconds of actual CPU work, and this instrumentation is the needed diagnostic groundwork for finding and fixing that bottleneck without changing any behavior itself.
