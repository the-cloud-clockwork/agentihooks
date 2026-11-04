"""A swarm git call refused over ssh is retried once over https with the gh credential helper."""

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts.swarm import cli, git_https, publish, stack

REFUSAL = "git@github.com: Permission denied (publickey).\nfatal: Could not read from remote repository.\n"
HTTPS_ENTRIES = {
    "GIT_CONFIG_KEY_0": "url.https://github.com/.insteadOf",
    "GIT_CONFIG_VALUE_0": "git@github.com:",
    "GIT_CONFIG_KEY_1": "url.https://github.com/.insteadOf",
    "GIT_CONFIG_VALUE_1": "ssh://git@github.com/",
    "GIT_CONFIG_KEY_2": "url.https://github.com/.insteadOf",
    "GIT_CONFIG_VALUE_2": "git+ssh://git@github.com/",
    "GIT_CONFIG_KEY_3": "credential.https://github.com.helper",
    "GIT_CONFIG_VALUE_3": "",
    "GIT_CONFIG_KEY_4": "credential.https://github.com.helper",
    "GIT_CONFIG_VALUE_4": "!gh auth git-credential",
    "GIT_CONFIG_COUNT": "5",
    "GIT_TERMINAL_PROMPT": "0",
}


def answers(
    *replies: tuple[int, str, str],
) -> tuple[list[tuple[list[str], dict]], Callable[..., subprocess.CompletedProcess[str]]]:
    calls: list[tuple[list[str], dict]] = []
    queue = list(replies)

    def fake(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        code, out, err = queue.pop(0)
        return subprocess.CompletedProcess(argv, code, out, err)

    return calls, fake


def test_a_success_runs_once() -> None:
    calls, fake = answers((0, "ok\n", ""))
    assert git_https.run(["git", "push"], runner=fake, cwd="/w").stdout == "ok\n"
    assert calls == [(["git", "push"], {"capture_output": True, "text": True, "cwd": "/w"})]


@pytest.mark.parametrize(
    "answer",
    [
        (1, "", "fatal: couldn't find remote ref nope\n"),
        (128, "", "git@github.com: Permission denied (password).\n"),
        (0, "ok\n", REFUSAL),
    ],
)
def test_only_a_failed_publickey_refusal_is_retried(answer: tuple[int, str, str]) -> None:
    calls, fake = answers(answer)
    done = git_https.run(["git", "fetch", "origin", "nope"], runner=fake)
    assert (done.returncode, done.stdout, done.stderr) == answer
    assert len(calls) == 1


def test_a_publickey_refusal_is_retried_once_over_https_for_that_call_only() -> None:
    base = {"PATH": "/bin", "GH_TOKEN": "tok"}
    calls, fake = answers((128, "", REFUSAL), (0, "pushed\n", ""))
    done = git_https.run(["git", "push", "-u", "origin", "HEAD:b"], runner=fake, env=base, timeout=60)
    assert done.stdout == "pushed\n"
    assert calls == [
        (["git", "push", "-u", "origin", "HEAD:b"], {"capture_output": True, "text": True, "env": base, "timeout": 60}),
        (
            ["git", "push", "-u", "origin", "HEAD:b"],
            {"capture_output": True, "text": True, "env": {**base, **HTTPS_ENTRIES}, "timeout": 60},
        ),
    ]
    assert base == {"PATH": "/bin", "GH_TOKEN": "tok"}


def test_a_refused_retry_is_returned_without_a_third_attempt() -> None:
    calls, fake = answers((128, "", REFUSAL), (128, "", "remote: Invalid username or token.\n"))
    done = git_https.run(["git", "fetch", "origin"], runner=fake, env={})
    assert (done.returncode, done.stderr) == (128, "remote: Invalid username or token.\n")
    assert len(calls) == 2
    assert calls[1][1]["env"] == HTTPS_ENTRIES


def test_the_retry_builds_on_the_process_environment_when_the_caller_passes_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    monkeypatch.setenv("SWARM_MARK", "kept")
    calls, fake = answers((128, "", REFUSAL), (0, "", ""))
    git_https.run(["git", "ls-remote", "origin"], runner=fake)
    retry = calls[1][1]["env"]
    assert retry["SWARM_MARK"] == "kept"
    assert {key: retry[key] for key in HTTPS_ENTRIES} == HTTPS_ENTRIES


def test_the_settings_follow_the_entries_the_caller_already_holds() -> None:
    held = {"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "a.b", "GIT_CONFIG_VALUE_0": "1"}
    assert git_https.environment(held) == {
        "GIT_CONFIG_KEY_0": "a.b",
        "GIT_CONFIG_VALUE_0": "1",
        "GIT_CONFIG_KEY_2": "url.https://github.com/.insteadOf",
        "GIT_CONFIG_VALUE_2": "git@github.com:",
        "GIT_CONFIG_KEY_3": "url.https://github.com/.insteadOf",
        "GIT_CONFIG_VALUE_3": "ssh://git@github.com/",
        "GIT_CONFIG_KEY_4": "url.https://github.com/.insteadOf",
        "GIT_CONFIG_VALUE_4": "git+ssh://git@github.com/",
        "GIT_CONFIG_KEY_5": "credential.https://github.com.helper",
        "GIT_CONFIG_VALUE_5": "",
        "GIT_CONFIG_KEY_6": "credential.https://github.com.helper",
        "GIT_CONFIG_VALUE_6": "!gh auth git-credential",
        "GIT_CONFIG_COUNT": "7",
        "GIT_TERMINAL_PROMPT": "0",
    }


def test_the_settings_carry_no_token_from_the_environment() -> None:
    env = git_https.environment({"GH_TOKEN": "ghs_secret", "GITHUB_TOKEN": "ghp_secret"})
    added = {key: value for key, value in env.items() if key not in ("GH_TOKEN", "GITHUB_TOKEN")}
    assert added == HTTPS_ENTRIES


def test_publish_retries_a_refused_push_over_https(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    calls, fake = answers((128, "", REFUSAL), (0, "", "To https://github.com/o/r.git\n"))
    monkeypatch.setattr(git_https.subprocess, "run", fake)
    assert publish.git(tmp_path, "push", "-u", "origin", "HEAD:b") == ""
    assert [kwargs.get("env", {}).get("GIT_CONFIG_COUNT") for _, kwargs in calls] == [None, "5"]
    assert calls[1][1]["cwd"] == tmp_path


def test_publish_names_a_push_refused_over_both_transports(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _, fake = answers((128, "", REFUSAL), (128, "", "fatal: Authentication failed\n"))
    monkeypatch.setattr(git_https.subprocess, "run", fake)
    with pytest.raises(publish.SwarmError, match=r"^git push failed: fatal: Authentication failed$"):
        publish.git(tmp_path, "push")


def test_the_stack_fetch_path_retries_a_refused_fetch_over_https(monkeypatch: pytest.MonkeyPatch) -> None:
    calls, fake = answers((128, "", REFUSAL), (0, "", ""))
    monkeypatch.setattr(git_https.subprocess, "run", fake)
    assert stack.shell(["git", "fetch", "origin", "b"]).returncode == 0
    assert calls[1][1]["timeout"] == 60
    assert calls[1][1]["env"]["GIT_CONFIG_COUNT"].isdigit()


def test_swarm_branch_reads_origin_over_https_after_a_refusal() -> None:
    calls, fake = answers((0, "engineer-a1b2c3-0001\n", ""), (128, "", REFUSAL), (0, "abc\trefs/heads/x\n", ""))
    assert cli.worktree_branch(None, run=fake) == "engineer-a1b2c3-0001"
    assert calls[2][0] == ["git", "ls-remote", "--exit-code", "--heads", "origin", "engineer-a1b2c3-0001"]
    assert calls[2][1]["timeout"] == 20
    assert "GIT_CONFIG_COUNT" in calls[2][1]["env"]
