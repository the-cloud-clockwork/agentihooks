# Releases

One entry per change merged into dev, newest release first. Each entry names its date, merge commit, pull request and author, so a change can be found by date and hash.

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
