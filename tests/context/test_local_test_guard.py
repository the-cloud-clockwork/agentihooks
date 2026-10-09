import pytest

from hooks.context.shell_commands import commands
from hooks.hook_manager import BlockAction

pytestmark = pytest.mark.unit

RUNNERS = [
    "pytest -q",
    "/venv/bin/pytest",
    "$V/pytest",
    "python -m pytest",
    "python -mpytest",
    "python -m coverage run -m pytest",
    "python -c'import pytest; pytest.main()'",
    "bash <<'EOF'\npytest\nEOF",
    "python - <<'PY'\nimport pytest\npytest.main()\nPY",
    "python3 -I -m unittest discover",
    "uv run pytest",
    "uv run --project app python -m pytest",
    "uvx pytest",
    "poetry run pytest",
    "pipenv run pytest",
    "hatch run test",
    "pdm run pytest",
    "tox",
    "nox",
    "mutmut run",
    "cosmic-ray exec config.toml",
    "mutation",
    "stryker run",
    "npm test",
    "npm run test:unit",
    "npm exec -- vitest run",
    "pnpm test",
    "pnpm -C app test",
    "yarn test",
    "yarn run test:ci",
    "npx jest",
    "pnpm exec vitest run",
    "bun test",
    "vitest run",
    "jest",
    "node --test",
    "node --test=tests/*.js",
    "playwright test",
    "npx playwright test",
    "go test ./...",
    "go -C app test ./...",
    "cargo test",
    "cargo +nightly test",
    "cargo nextest run",
    "make test",
    "make -C app test-unit",
    "ctest",
    "meson test",
    "dotnet test",
    "mvn test",
    "gradle test",
    "ruby -S rspec",
    "rspec",
    "phpunit",
    "prove",
    "python -c 'import pytest; pytest.main()'",
    "python -m scripts.ci_mutation",
]

WRAPPERS = [
    "{}",
    "timeout 30s {}",
    "timeout --signal TERM --kill-after=5s 30s {}",
    "timeout -s TERM -k 5s 30s {}",
    "sudo -u worker -- {}",
    "sudo --user=worker {}",
    "nice -n 10 {}",
    "nice --adjustment=10 {}",
    "env -i -u UNUSED FOO=bar {}",
    "FOO=bar {}",
    "env -S '{}'",
    "env --split-string='{}'",
    "command -- {}",
    "nohup {}",
    "timeout --foreground 30s sudo -n nice -n 5 env FOO=bar {}",
    "bash -lc '{}'",
]


@pytest.mark.parametrize("command", RUNNERS)
def test_test_and_mutation_commands_are_blocked_by_default(command, monkeypatch):
    from hooks.context.local_test_guard import check_local_tests

    monkeypatch.delenv("AGENTIHOOKS_ALLOW_LOCAL_TEST_RUN", raising=False)
    with pytest.raises(BlockAction, match="draft pull request.*CI"):
        check_local_tests({"tool_name": "Bash", "tool_input": {"command": command}})


@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_wrapped_test_commands_are_blocked(wrapper, monkeypatch):
    from hooks.context.local_test_guard import check_local_tests

    monkeypatch.delenv("AGENTIHOOKS_ALLOW_LOCAL_TEST_RUN", raising=False)
    assert ["pytest", "-q"] in commands(wrapper.format("pytest -q"))
    with pytest.raises(BlockAction):
        check_local_tests({"tool_name": "Bash", "tool_input": {"command": wrapper.format("pytest -q")}})


@pytest.mark.parametrize("value", ["true", "TRUE"])
def test_explicit_true_allows_tests(value, monkeypatch):
    from hooks.context.local_test_guard import check_local_tests

    monkeypatch.setenv("AGENTIHOOKS_ALLOW_LOCAL_TEST_RUN", value)
    check_local_tests({"tool_name": "Bash", "tool_input": {"command": "pytest -q"}})


@pytest.mark.parametrize(
    "command",
    [
        "echo pytest",
        "printf '%s' 'pytest -q'",
        "git show tests/test_a.py",
        "npm install",
        "npm run build",
        "go build",
        "cargo check",
        "make build",
        "cat <<'EOF'\npytest -q\nEOF",
        "# pytest\necho ok",
    ],
)
def test_non_test_commands_pass(command, monkeypatch):
    from hooks.context.local_test_guard import check_local_tests

    monkeypatch.delenv("AGENTIHOOKS_ALLOW_LOCAL_TEST_RUN", raising=False)
    check_local_tests({"tool_name": "Bash", "tool_input": {"command": command}})


@pytest.mark.parametrize(
    "target,name,args",
    [
        ("claude", "Bash", {"command": "pytest"}),
        ("codex", "exec", {"cmd": "pytest"}),
        ("copilot", "bash", {"command": "pytest"}),
    ],
)
def test_pre_tool_use_blocks_each_harness(target, name, args, monkeypatch):
    from hooks import config, hook_manager
    from hooks.targets.normalizer import normalize_payload

    monkeypatch.setenv("AGENTIHOOKS_TARGET", target)
    monkeypatch.setattr(config, "SECRETS_MODE", "off")
    monkeypatch.setattr(config, "QUOTA_POLICY_ENABLED", False)
    monkeypatch.delenv("AGENTIHOOKS_ALLOW_LOCAL_TEST_RUN", raising=False)
    payload = normalize_payload({"tool_name": name, "tool_input": args, "session_id": "local-test-guard"})
    with pytest.raises(BlockAction, match="draft pull request"):
        hook_manager.on_pre_tool_use(payload)


def test_later_commands_and_substitutions_are_checked(monkeypatch):
    from hooks.context.local_test_guard import check_local_tests

    monkeypatch.delenv("AGENTIHOOKS_ALLOW_LOCAL_TEST_RUN", raising=False)
    for command in [
        "echo ok && pytest",
        "echo $(pytest)",
        'echo "$(pytest)"',
        "echo `pytest`",
        "if true; then pytest; fi",
    ]:
        with pytest.raises(BlockAction):
            check_local_tests({"tool_name": "Bash", "tool_input": {"command": command}})
