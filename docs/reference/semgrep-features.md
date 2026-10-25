---
title: Semgrep feature tests
parent: Reference
nav_order: 60
---

# Semgrep feature tests

An operator ordered feature gets one rule in `.semgrep/features/`. Its id starts
with `feature-`, its severity is `ERROR`, and its message names the feature in
plain words and gives the date the operator ordered it as `YYYY-MM-DD`. Match a
specific regression of that feature. Semgrep checks static structure; use
behavioral tests for requirements that static patterns cannot establish.

Add `<feature>.yml` and a same-basename fixture such as `<feature>.py` or
`<feature>.test.yaml`. Put `# ruleid: feature-<name>` immediately before code
that regresses the feature, and `# ok: feature-<name>` before code that preserves
it. YAML fixtures use separate documents for independent cases. Keep both cases
in every fixture pair; use the exact rule id in the annotations.

The Semgrep workflow runs `semgrep --metrics off --test .semgrep/features` in
its parallel `feature-tests` job. Test mode fails when the regression stops
matching or the passing case starts matching. Fixtures stay under `.semgrep/`,
which `.semgrepignore` excludes from the production scan. The existing local
scan recursively loads `.semgrep`, including feature rules, with `--error
--strict`; a feature finding fails the required pull request Semgrep check and
prints the dated feature message. Fleet packs and chart checks run alongside it.

Push the rule and fixtures and read the test result in CI; tests do not run on
the workstation. A focused run is available with:

```bash
gh workflow run proofs.yml --ref <branch> -f proof=semgrep -f base=origin/dev
```

Before declaring a new rule proved, plant the feature regression in the pull
request and observe the named finding turn Semgrep red. Restore the feature and
observe green. Record both run ids in the pull request. Also break the rule's
match once and confirm test mode fails, then restore it. The initial
`feature-semgrep-feature-tests` rule protects the feature fixture invocation in
the local rules workflow itself, ordered on 2026-10-25.
