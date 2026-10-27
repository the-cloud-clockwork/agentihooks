import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WRAPPER = Path(__file__).resolve().parents[2] / "deploy" / "worker" / "gh"
BODY = "x" * 20
PRINTED = "ghs_" + BODY
CLASSIC = "ab" * 20


def wrapper_with_fake_gh(tmp_path, status):
    fake = tmp_path / "gh-real"
    fake.write_text(
        f'#!/usr/bin/env bash\necho "  - Token: {PRINTED}***" >&2\necho "  - Token: {CLASSIC}" >&2\n'
        f'echo "args: $*"\nexit {status}\n'
    )
    fake.chmod(0o755)
    wrapper = tmp_path / "gh"
    wrapper.write_text(WRAPPER.read_text().replace("/usr/bin/gh", str(fake)))
    wrapper.chmod(0o755)
    return wrapper


def run(wrapper, tmp_path, *args):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "AGENTIHOOKS_HOME": str(tmp_path)}
    return subprocess.run([str(wrapper), *args], env=env, capture_output=True, text=True)


@pytest.mark.parametrize("status", [0, 1])
@pytest.mark.parametrize("args", [("auth", "status"), ("--hostname", "github.com", "auth", "status", "-t")])
def test_auth_status_hides_the_token_and_keeps_the_exit_status(tmp_path, status, args):
    result = run(wrapper_with_fake_gh(tmp_path, status), tmp_path, *args)

    assert BODY not in result.stdout + result.stderr
    assert CLASSIC not in result.stdout + result.stderr
    assert "Token: " in result.stdout + result.stderr
    assert "args: " + " ".join(args) in result.stdout
    assert result.returncode == status


def test_other_commands_pass_through_untouched(tmp_path):
    result = run(wrapper_with_fake_gh(tmp_path, 0), tmp_path, "pr", "view")

    assert result.stdout == "args: pr view\n"
    assert PRINTED in result.stderr
