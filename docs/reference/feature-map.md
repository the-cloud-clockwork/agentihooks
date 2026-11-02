---
title: Feature map
parent: Reference
nav_order: 63
---

# Feature map

`feature-map.json` at the repository root gives every user visible behaviour of
the swarm a stable id, the seam its tests observe, the suite tests that cover it
and, where one exists, a live check the Doctor runs against the running swarms.
Its `features` list holds one entry per behaviour.

## Origins

Every entry names where it came from in `origin`:

| Origin | Meaning | Extra keys it requires |
| --- | --- | --- |
| `operator` | A feature the operator ordered in his own words | `operator_words`, `ordered_at`, `ledger_item`, `semgrep_rule`, `live_check` |
| `regression` | A behaviour pinned after an incident | `ledger_item` of the incident task |
| `inventory` | An existing behaviour recorded with the tests that already cover it | none |

## Keys

| Key | Required | Value |
| --- | --- | --- |
| `id` | Every entry | Unique feature name, used by the test marker and the Doctor finding |
| `origin` | Every entry | `operator`, `regression` or `inventory` |
| `behaviour` | Every entry | One plain sentence describing the behaviour |
| `seam` | Every entry | The public boundary the tests observe |
| `tests` | Every entry, unless `behaviour_test` is given | List of suite node ids, for example `tests/test_login.py::test_shared` |
| `behaviour_test` | Optional | One suite node id, accepted in place of or beside `tests` |
| `operator_words` | `operator` | Verbatim operator quotation, including voice typing |
| `ordered_at` | `operator` | Order date as `YYYY-MM-DD` |
| `ledger_item` | `operator`, `regression`; optional for `inventory` | Source as `ledger:SLUG/tasks/ITEM`, or a `phases`, `comments` or `chat` item |
| `semgrep_rule` | `operator`; optional otherwise | Rule id from `.semgrep/features/` |
| `live_check` | `operator`; optional otherwise | Argument list run from the repository root, without a shell |

An `ordered_at` outside `operator` is checked the same way and needs a
`ledger_item` beside it.

An optional key that is present is checked the same way as a required one: a
rule id must exist, a ledger item must have the ledger form and a live check
must name a repository command.

An illustrative regression entry:

```json
{
  "features": [
    {
      "id": "front-door-seat",
      "origin": "regression",
      "ledger_item": "ledger:rig-grade-swarm/tasks/rpin1",
      "behaviour": "The front door seat is bound to a live session after every handoff.",
      "seam": "The swarm status handoff rows for the master seat",
      "tests": ["tests/test_handoff.py::test_a_successor_binds_the_master_seat"]
    }
  ]
}
```

## Test marker

Mark every listed test with `@pytest.mark.feature("FEATURE_ID")`. Class methods
use `tests/test_login.py::TestLogin::test_shared`. The validator parses the
suite source without importing it or running it, and fails when:

- a listed node does not exist as a collectable suite test;
- a listed node does not carry the marker with the entry's id;
- a marked test is listed by no entry, which includes a marker naming an id the
  map does not hold;
- a marker has no id, two entries share an id, or two feature rules share an id.

Every rule in `.semgrep/features/` must be named by an entry's `semgrep_rule`
or listed in the map's top level `unmapped_rules`. That list holds the rules not
yet mapped and may only shrink: a listed rule that an entry names, or that no
longer exists, fails the map until it is removed from the list. A test pins the list
to the rules it held when the map landed, so a grown list fails CI.

## Operator entries

Copy the operator's words from the recorded ledger source before adding an
entry. A paraphrase or an agent's requirement is not an operator order. Review
checks the quotation against its source; the validator requires the words and
source but does not authenticate an offline copy of a ledger record.

## Live checks

Live commands use a repository script with `bash`, a Python script or module
with `python` or `python3`, or an executable repository path. The validator
checks the target exists inside the repository; the Doctor runs only the entries
that carry a live check and judges the exit status, see
[Feature live checks](feature-live-checks.md). Commands must check the deployed
behaviour and return nonzero when the feature fails. They must not change
configuration or print credentials.

The seed command `bash scripts/feature_checks/semgrep-feature-tests.sh` checks
the feature fixture step in the latest completed pull request Tests run. Pass a
run id as its optional first argument to inspect a specific run. Both the job
and its fixture step must have succeeded; a missing job fails.

## The gate

The `feature-tests` job of the Semgrep workflow runs `python -m scripts.feature_map`
in the pull request tier and prints
`Feature map: N features, all rule, test and live check links exist` when the
map is valid. Any failure exits nonzero with the reason. When changing the gate,
plant a marked test no entry lists and record a red CI run, remove it and record
green. Tests run in CI only.
