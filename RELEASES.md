# Releases

One entry per change merged into dev, newest release first. Each entry names its date, merge commit, pull request and author, so a change can be found by date and hash.

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
