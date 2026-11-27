#!/usr/bin/env bash
set -euo pipefail

DRILL="$(cat <<'PY'
def take(count):
    outcomes, empty = [], 0
    while len(outcomes) < count and empty < 5:
        outcomes.append(spoke.poll(1_000))
        # ruleid: spoke-poll-idle-count-resets-on-held
        empty = empty + 1 if outcomes[-1] == "empty" else 0
    return outcomes


def quiet(count):
    outcomes, idle = [], 0
    while len(outcomes) < count and idle < 5:
        outcomes.append(spoke.poll(1_000))
        # ruleid: spoke-poll-idle-count-resets-on-held
        idle = idle + 1 if outcomes[-1] == 'empty' else 0
    return outcomes


def bounded(count):
    outcomes, idle = [], 0
    while len(outcomes) < count and idle < 5:
        outcomes.append(spoke.poll(1_000))
        # ok: spoke-poll-idle-count-resets-on-held
        idle = idle + 1 if outcomes[-1] in ("empty", "held") else 0
    return outcomes


def started(count):
    outcomes, launches = [], 0
    while launches < count:
        outcomes.append(spoke.poll(1_000))
        # ok: spoke-poll-idle-count-resets-on-held
        launches = launches + 1 if outcomes[-1] == "started" else launches
    return outcomes
PY
)"
printf '%s\n' "$DRILL"
