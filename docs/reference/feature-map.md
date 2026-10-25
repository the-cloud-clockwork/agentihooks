---
title: Operator feature map
parent: Reference
nav_order: 61
---

# Operator feature map

`feature-map.json` links operator orders to code, behaviour and live checks. Its
`features` list contains one entry per ordered feature:

| Key | Value |
| --- | --- |
| `id` | Unique feature name |
| `operator_words` | Verbatim operator quotation, including voice typing |
| `ordered_at` | Order date as `YYYY-MM-DD` |
| `ledger_item` | Source as `ledger:SLUG/tasks/ITEM` or another ledger item |
| `behaviour` | One plain sentence describing the behaviour |
| `semgrep_rule` | Rule id from `.semgrep/features/` |
| `behaviour_test` | Suite node id, for example `tests/test_login.py::test_shared` |
| `live_check` | Argument list run from the repository root, without a shell |

Copy the operator's words from the recorded ledger source before adding an
entry. A paraphrase or an agent's requirement is not an operator order. Review
checks the quotation against its source; the gate requires the words and source
but does not authenticate an offline copy of a ledger record.

Mark the behaviour test function with `@pytest.mark.feature("FEATURE_ID")`.
Class methods use `tests/test_login.py::TestLogin::test_shared`. The map gate
parses the source without importing it or running the suite. Each feature rule
and each marked test must belong to an entry. Missing rule ids, test nodes,
markers, source fields and live command targets fail the required Semgrep feature job.

Live commands use a repository script with `bash`, a Python script or module
with `python` or `python3`, or an executable repository path. The validator
checks the target exists; the Doctor executes it and judges its exit status.
Commands must check the deployed behaviour and return nonzero when the feature
fails. They must not change configuration or print credentials.

The seed command `bash scripts/feature_checks/semgrep-feature-tests.sh` checks
the feature fixture job in the latest completed pull request Tests run. Pass a
run id as its optional first argument to inspect a specific run. Both the job
and its fixture step must have succeeded; a missing job fails.

The Semgrep feature job runs `python -m scripts.feature_map` in the pull request tier. When changing
the gate, plant a missing test node in an entry and record a red CI run, restore
the exact reference and record green. Tests run in CI only.
