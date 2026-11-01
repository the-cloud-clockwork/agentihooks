# Releases

One entry per change merged into dev, newest release first. Each entry names its date, merge commit, pull request and author, so a change can be found by date and hash.

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
