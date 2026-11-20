from collections.abc import Callable, Mapping

import pytest

from scripts.ci_lab import checks, scheduler
from scripts.ci_lab.checks import Output
from scripts.ci_lab.objects import upload_path
from scripts.ci_lab.store import Verdict
from scripts.ci_lab.units import Unit
from tests.ci_lab.test_scheduler import HEAD_A, Run, plan, unit
from tests.ci_lab.test_scheduler import lab as _lab

lab = _lab
GITHUB = "gh" + "p_" + "a" * 36
SLACK = "xo" + "xb-" + "1" * 12
PASSWORD = "PASS" + "WORD=" + "q" * 12
LOG = b"".join(
    [
        b"head of the log\n",
        b"x" * 5000,
        f"\ngithub {GITHUB}\nslack {SLACK}\n{PASSWORD}\n".encode(),
        b"bad byte \xff\n",
        b"I001 [*] Import block is un-sorted or un-formatted\n --> scripts/ci_lab/head_notice.py:1:1\n  |\n",
        b"1 | / import hmac\n2 | | import hashlib\n  | |______________^\n  |\nhelp: Organize imports\n\n",
        b"Found 1 error.\n[*] 1 fixable with the `--fix` option.\n",
    ]
)
TAIL = (
    LOG.decode(errors="replace")
    .replace(GITHUB, "[REDACTED:github_token]")
    .replace(SLACK, "[REDACTED:slack_token]")
    .replace(PASSWORD, "[REDACTED:generic_secret]")[-4096:]
)


def finish(run: Run, outcomes: Mapping[Unit, tuple[str, bytes | None]]) -> None:
    jobs = {scheduler.job_id(HEAD_A, item): result for item, result in outcomes.items()}
    for _ in outcomes:
        grant = run.lease()[0]
        outcome, log = jobs[grant.job]
        if log is None:
            run.finish(grant, outcome)
            continue
        run.lab.objects.write_once(upload_path(grant.job, "log"), log)
        run.finish(grant, outcome, {"seconds": 1.0}, log=False)


def output(run: Run, gate: str) -> Output:
    return [post[4] for post in run.lab.poster.posts if post[1] == gate][-1]


@pytest.mark.feature("lab-static-check-log-tail")
def test_a_failed_static_unit_s_gate_check_carries_the_tail_of_its_stored_log_with_secrets_redacted(
    lab: Callable[..., Run],
) -> None:
    failed, passed = unit(1), unit(2)
    run = lab({HEAD_A: plan({"lint": (failed, passed)})})
    run.push(HEAD_A)
    run.tick(1000)
    finish(run, {failed: (scheduler.FAILED, LOG), passed: (scheduler.PASSED, b"passed unit log\n")})
    run.tick(1100)
    assert run.lab.poster.moves(HEAD_A, "lint")[-1] == ("completed", "failure")
    assert [item for item, _ in output(run, "lint").logs] == [failed]
    text = checks.render(output(run, "lint"))["text"]
    listing = f"- static `{failed.key}`\n- static `{passed.key}`\n"
    assert text == f"Log tail of static `{failed.key}`:\n```\n{TAIL}\n```\n{listing}"
    assert GITHUB not in text
    assert SLACK not in text
    assert PASSWORD not in text
    assert "I001" not in checks.render(output(run, scheduler.REQUIRED))["text"]


@pytest.mark.feature("lab-static-check-log-tail")
def test_a_failed_static_unit_answered_from_the_store_carries_its_stored_log_tail(lab: Callable[..., Run]) -> None:
    item = unit(1)
    run = lab({HEAD_A: plan({"lint": (item,)})})
    run.store.put(Verdict(item.kind, item.key, False, False, 1.0), {}, b"stored failure\n")
    run.push(HEAD_A)
    run.tick(1000)
    assert run.lab.poster.moves(HEAD_A, "lint") == [("completed", "failure")]
    text = checks.render(output(run, "lint"))["text"]
    assert text == f"Log tail of static `{item.key}`:\n```\nstored failure\n\n```\n- static `{item.key}`\n"


@pytest.mark.feature("lab-static-check-log-tail")
def test_a_gate_whose_static_units_pass_or_keep_no_log_posts_only_its_unit_keys(lab: Callable[..., Run]) -> None:
    first, second, unlogged, empty, tested = unit(1), unit(2), unit(3), unit(4), unit(5, "test")
    gates = {"lint": (first, second), "type-gate": (unlogged,), "semgrep": (empty,), "unit": (tested,)}
    run = lab({HEAD_A: plan(gates)})
    run.cache(empty, passed=False)
    run.store.put(Verdict(tested.kind, tested.key, False, False, 1.0), {}, b"failed test unit log\n")
    run.push(HEAD_A)
    run.tick(1000)
    outcomes = {
        first: (scheduler.PASSED, b"first unit log\n"),
        second: (scheduler.PASSED, b"second unit log\n"),
        unlogged: (scheduler.FAILED, None),
    }
    finish(run, outcomes)
    run.tick(1100)
    assert run.lab.poster.moves(HEAD_A, "lint")[-1] == ("completed", "success")
    assert run.lab.poster.moves(HEAD_A, "type-gate")[-1] == ("completed", "failure")
    assert run.lab.poster.moves(HEAD_A, "semgrep") == [("completed", "failure")]
    assert run.lab.poster.moves(HEAD_A, "unit") == [("completed", "failure")]
    assert checks.render(output(run, "lint"))["text"] == f"- static `{first.key}`\n- static `{second.key}`\n"
    assert checks.render(output(run, "type-gate"))["text"] == f"- static `{unlogged.key}`\n"
    assert checks.render(output(run, "semgrep"))["text"] == f"- static `{empty.key}`\n"
    assert checks.render(output(run, "unit"))["text"] == f"- test `{tested.key}`\n"
