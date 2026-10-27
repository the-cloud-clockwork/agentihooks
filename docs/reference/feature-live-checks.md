---
title: Feature live checks
parent: Reference
nav_order: 61
---

# Feature live checks

An operator ordered feature can carry a live check: a command that judges the
feature against the running swarms, not against the code. The Doctor runs every
live check in the feature map on its timer and raises one `feature regressed`
finding for each check that does not exit zero. The finding carries the
operator's own words, so whoever judges it reads what was ordered, not a
paraphrase. [Semgrep feature tests](semgrep-features.md) guard the same features
statically in CI; a live check catches a regression that only shows at run time.

## The feature map

The map is `feature-map.json` at the root of the agentihooks checkout the
Doctor runs from: the detector reads the file beside the `scripts` folder of its
own code, not the watched swarm's repository. With no file there the detector
reports nothing. The map arrives with the feature map validation pull request
([issue 7271](https://github.com/the-cloud-clockwork/agentihooks/issues/7271));
until that file is on `dev`, every pass of the `feature` detector is empty.

The file holds one object with a `features` list. No map entry runs the
front door login check yet; it is added by its own task. An entry that wires it
looks like this:

```json
{
  "features": [
    {
      "id": "front-door-login",
      "operator_words": "All the main masters from all the swarms should run in the same tier unless the tier is exhausted.",
      "ordered_at": "2026-10-25",
      "ledger_item": "ledger:rig-grade-swarm-doctor/tasks/t853",
      "behaviour": "Every front door master runs on the preferred stored claude ai login.",
      "semgrep_rule": "feature-front-door-login",
      "behaviour_test": "tests/test_front_door_login_check.py::test_a_front_door_on_a_token_while_the_stored_login_has_room_fails",
      "live_check": ["python", "-m", "scripts.feature_checks.front_door_login"]
    }
  ]
}
```

| Field | Used by the detector | Meaning |
|---|---|---|
| `id` | Yes | The finding subject: the finding id is `feature-regressed/<id>` |
| `operator_words` | Yes | The operator's words as he ordered the feature, quoted in the evidence |
| `ordered_at` | Yes | The order date, `YYYY-MM-DD` |
| `ledger_item` | Yes | The ledger item that holds the order |
| `behaviour` | Yes | One sentence naming the behaviour the check protects, used in the summary |
| `live_check` | Yes | The command as an argument list, run without a shell |
| `semgrep_rule`, `behaviour_test` | No | The feature's Semgrep rule and behaviour test, read by the map's CI validation |

The map on the validation branch holds one entry, `semgrep-feature-tests`, whose
live check is `["bash", "scripts/feature_checks/semgrep-feature-tests.sh"]`.

Every field the detector uses is required. A file that is not valid JSON or has
no `features` key, or an entry without `live_check`, fails the whole detector
for that pass. An entry missing another field fails it only when its check
fails, because the finding is built from those fields. See
[Troubleshooting](#troubleshooting).

## How the Doctor runs a check

The `feature` detector is one of the detectors of the
[Doctor timer pass](../pillars/swarm.md#doctor-timer-pass). It runs once per
detection pass, in its own thread beside the others, and is timed as the
`doctor.feature` step. For every entry, in map order:

1. A first argument of `python` or `python3` is replaced with the interpreter
   the Doctor itself runs on, so a Python check imports the same installed code.
   Any other first argument runs as given, looked up on `PATH`.
2. The command runs with the map's folder as its working directory, with
   output captured and a 120 second deadline.
3. Exit 0 passes and raises nothing. A nonzero exit, a timeout or a command that
   cannot start raises a finding.

Checks run one after another, so the detector takes as long as all its checks
together. The detection pass waits for it like any other detector, inside the
pass limits of the Doctor tick; keep each check well under a minute.

Nothing here is configurable: the map path, the 120 second deadline and the
output bounds below are fixed in `scripts.doctor.features`.

## The finding

| Field | Value |
|---|---|
| id | `feature-regressed/<id>`, for example `feature-regressed/front-door-login` |
| summary | `feature <id> regressed: <behaviour>` |
| evidence, first line | `operator words: <operator_words>` |
| evidence, second line | `ordered <ordered_at> in <ledger_item>` |
| evidence, third line | `live check <command> <how it ended>`: `exited <code>`, `timed out after 120 seconds`, or `could not run: <error type>: <message>` |
| evidence, remaining lines | `output: <line>`: the last six non-empty lines the check printed, standard output then standard error. None for a timeout or a check that could not start |
| threshold | `a feature map live check that does not exit zero` |
| number | `1` |

Each output line passes through the strict secret redaction of `hooks.secrets`
before it is stored, has its whitespace collapsed to single spaces and is cut at
300 characters. A check should still print account names and states only, never
a credential.

Each failing entry raises its own finding. The Doctor judges it like any other:
verdict, then a fix task when the regression is real.

### Usage

Measure a feature before and after its fix, passing the watched swarm's id:

```bash
agentihooks doctor rig-grade-swarm measure feature-regressed/front-door-login
```

It runs the detectors once and prints `feature-regressed/front-door-login 1`
while the check fails. Once the check passes it prints `0`, provided the Doctor
saw the finding earlier; an id never seen is refused with
`no finding feature-regressed/<id> has been seen; measure the class feature-regressed to read every live target`.

Judge it and file the fix:

```bash
agentihooks doctor rig-grade-swarm verdict feature-regressed/front-door-login established \
  --note "Rig front door runs on a quota token while the stored login has room"
agentihooks doctor rig-grade-swarm task feature-regressed/front-door-login --fix code
```

Run one check by hand from the checkout the Doctor uses, to read its full
output and exit code:

```bash
cd ~/dev/tcc-ecosystem/agentihooks
~/dev/tcc-ecosystem/.venv/bin/python -m scripts.feature_checks.front_door_login; echo "exit $?"
```

## Front door login check

`scripts.feature_checks.front_door_login` is the first live check. It protects
the operator's order of 25 October that every swarm's front door master runs on
the same stored claude ai login unless that login is exhausted; see
[Developer account login](account-login.md) for how a front door takes a stored
login.

### What it reads

| Input | Source |
|---|---|
| Front doors | Every swarm in the swarm Redis whose state is not `stopped`, and in it every agent in lane `master`, state `working`, harness `claude`, whose name is a live agent session and whose seat is `master@<slug>` or empty (a master that predates seats) |
| Each front door's route | The holders recorded in each stored login slot's `owner.json`. A front door named there runs on that slot's login; one named in no slot runs on a quota token, shown with the agent's routed account |
| Stored logins and their order | Every stored login slot, ranked by `AGENTIHOOKS_MASTER_LOGIN_ORDER` (comma separated account names, earliest first), then by most room left, then by slot number. This is the order a front door launch tries them |
| Room left on a login | 100 minus the larger of its five hour and week usage, from its sessions' quota snapshots and the router cache |

It never reads a credential file or prints an email address: accounts appear in
their slug form, such as `oneatexample_com`. A login with 10 percent room or less
counts as spent, the same drain rule `agentihooks balance` uses.

### When it fails

| Rule | Failure line |
|---|---|
| A front door runs on a quota token while any stored login has room | `FAIL: <slug> front door <agent> runs on a quota token while the stored login <first login with room> has room` |
| Front doors sit on two or more different logins while the first login in order has room | `FAIL: front doors sit on different logins, <a> and <b>, while the preferred login <first login> has room` |

A token front door passes when every stored login is spent, and front doors may
spread across logins once the first login in order is spent. With no stored
login at all, a token front door passes. Pool masters (`master-2@<slug>` and up),
Codex masters, idle or stopped masters, and masters of stopped swarms are never
judged.

### Output

```text
stored logins in order: oneatexample_com 60% left, twoatexample_com 20% left
doctor front door master@doctor-1 runs on the claude ai login oneatexample_com
rig front door master@rig-1 runs on a quota token tokenatexample_com
FAIL: rig front door master@rig-1 runs on a quota token while the stored login oneatexample_com has room
```

The first line lists the stored logins in launch order, or `none`. One line per
front door names its route; a token front door with no routed account shows
`unknown`. Every violation adds a `FAIL:` line and the check exits 1; with none
it exits 0. Through the Doctor, the last six of these lines become the finding's
`output:` evidence.

## Troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| No `feature regressed` finding ever appears | No `feature-map.json` at the root of the Doctor's checkout | Check with `ls ~/dev/tcc-ecosystem/agentihooks/feature-map.json`. The detector is silent without the map |
| The Doctor tick prints `the feature detector failed: JSONDecodeError: ...` or `KeyError: ...` | The map is not valid JSON, has no `features` key, or an entry lacks a field the detector uses | Fix the map by pull request. No finding is raised for that pass, not only for the broken entry |
| `could not run: FileNotFoundError: ...` | The first argument is not on `PATH` and is not `python` or `python3`, or a script path is wrong relative to the map's folder | Paths resolve against the map's folder; run the command by hand from there |
| `timed out after 120 seconds` | The check waited on something slow, such as an unreachable Redis | Run it by hand and time it. A check that reads live state should fail fast on an unreachable service |
| The login check reports a master as a token front door though it runs on a login | The master's name is missing from every slot's `owner.json` holders | Read the slots with `agentihooks claude login --master --list`; the holder list is what the check trusts |
| The login check skips a master that is running | It is a pool master, a Codex master, not in state `working`, not a live session, or its swarm is stopped | Expected: only live working Claude front doors of running swarms are judged |

## Delivery

Delivered in [pull request 7319](https://github.com/the-cloud-clockwork/agentihooks/pull/7319)
for ledger task t857, ordered by the operator on 25 October. Its tests cover a
failing check raising a finding with the operator's words and the check output,
passing checks and a missing map raising nothing, a check that cannot start,
the timeout and start error texts, the working directory and deadline, output
redaction and bounds, and the detector running inside the Doctor pass; and for
the login check, a token front door failing while a login has room, front doors
sharing the preferred login passing, a token passing when every login is spent,
different logins failing while the preferred one has room and passing once it is
spent, and the launch order of stored logins.
[Pull request 7775](https://github.com/the-cloud-clockwork/agentihooks/pull/7775)
later limited the login check to live front doors of swarms that are not stopped.
