---
priority: 2
description: Checks run in three delivery tiers; a red integration branch keeps merging, promotion owns the slow checks and a red promotion must produce fixer tasks
---

# Delivery Tiers

Every check in a repository's CI belongs to exactly one tier. A check runs where its answer is needed, never earlier.

| Tier | Runs on | Checks | Blocks |
|---|---|---|---|
| **Pull request** | each push to a pull request head | lint, format, size, static analysis, ratchets on changed lines, the tests the change touches | the pull request entering the queue |
| **Queue** | the merge queue's merge group | the unit suite, once per group | the merge into the integration branch |
| **Promotion** | each push to the integration branch, each in its own run, so a red run never holds a newer green head | everything slow: coverage and mutation across whole modules, images and their smoke tests, cluster and load tests, time budget reports | promotion from the integration branch to the release branch |

- **A red integration branch keeps merging; promotion does not advance.** A failed promotion run never closes the queue or holds other pull requests. The release branch stays on the last green promoted head until a later run passes.
- **Promotion owns the slow checks.** A check whose run time does not fit the pull request or queue budget moves to promotion, never off the books. A pull request is never held for a check that only promotion runs.
- **A red promotion must produce one fixer task per failing check.** Each must be a `ci` task naming the check, the run and the commit, raised with a ledger alert, and closed by the next green promotion run. Fix the cause in code through a normal pull request; never rerun a promotion until it happens to pass.
- A runner lost mid run is retried once; a retry is not a pass on a failed check.
