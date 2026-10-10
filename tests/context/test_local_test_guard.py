import pytest

from hooks.context.shell_commands import commands
from hooks.hook_manager import BlockAction

pytestmark = pytest.mark.unit

RUNNERS = [
    "pytest -q",
    "pytest-3",
    "npx vitest@latest@extra",
    "python /venv/bin/pytest",
    "python tests/test_a.py",
    "python tests/test-a.py",
    "./test-suite.sh",
    "./test_suite.sh",
    "node -e jest.runCLI()",
    "node --eval vitest.start()",
    "/venv/bin/pytest",
    "$V/pytest",
    "python -m pytest",
    "python -m pytest -q",
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
    "python3 -c \"import subprocess; subprocess.run(['pytest', '-q'])\"",
    "python3 -c \"__import__('pytest').main()\"",
    "python3 -c \"import runpy; runpy.run_module('pytest')\"",
    "python3 - <<'EOF'\nimport os\nos.system('python -m pytest')\nEOF",
    "node -e \"require('child_process').execSync('npx jest')\"",
    "node -e \"require('node:test')\"",
    "python3 -c \"getattr(__builtins__, '__import__')('pytest').main()\"",
    "python3 -c \"vars(__builtins__)['__import__']('unittest').main()\"",
    "node -e \"const M = require('mocha'); new M().run()\"",
    "node -e \"require('jest-cli').run()\"",
    "node -e \"require('vitest/node').startVitest('unit')\"",
    "python3 -c \"import os; getattr(os, 'system')('python -m pytest')\"",
    "python3 -c \"eval(\\\"__import__('os').system('python -m pytest')\\\")\"",
    "node -e \"require('child_process')['exec']('npx jest')\"",
    'node -e "new Function(\'require(\\"child_process\\").execSync(\\"npx jest\\")\')()"',
    "python3 - $'\\' ' <<EOF\nimport pytest; pytest.main(); print(\"'\")  # \"\nEOF",
    "# don't\npython3 - <<'EOF'\nimport pytest; pytest.main(); print(\"'\")  # \"\nEOF",
    "cd tests # run them\npytest -q",
    "echo a#b\npytest -q",
    "echo \\ #x; pytest -q",
    "node -e \"import('vitest/node').then(v => v.startVitest('unit'))\"",
    "node --input-type=module -e \"import M from 'mocha'; await new M().run()\"",
    "python3 -c \"import asyncio; asyncio.run(asyncio.create_subprocess_shell('python -m pytest'))\"",
    "python3 -c \"eval('import pytest; pytest.main()')\"",
    "python3 -c \"exec(compile('import pytest; pytest.main()', 'x', 'exec'))\"",
    'node -e "eval(\'require(\\"mocha\\").run()\')"',
    "node -e \"const m = 'mocha'; new (require(m))().run()\"",
    "node -e \"require('vm').runInThisContext('require(\\\"jest\\\").run()')\"",
    "node --input-type=module -e \"import { startVitest } from 'vitest/node'; await startVitest('unit')\"",
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
    "env -S'{}'",
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
        "python -m json.tool",
        "python -m json.tool pytest",
        "python -m",
        "python -c",
        "python -c pass pytest",
        "python -mjson.tool",
        "python app.py",
        "python test_app.txt",
        "node -e console.log(1)",
        "node jest",
        "ruby app.rb",
        "./build.sh",
        "./test-app.txt",
        "echo '\\$(pytest)'",
        """echo '\\`pytest`'""",
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
    "command",
    [
        "grep -n pytest .github/workflows/test.yml",
        "grep -n '<<' .github/workflows/test.yml",
        'grep -n "cat <<EOF" .github/workflows/test.yml',
        "grep -c \\<\\< tests/test_a.py",
        "awk '/<<EOF/,/^EOF/' .github/workflows/test.yml",
        "sed -n '/pytest/,+3p' .github/workflows/test.yml",
        "cat tests/context/test_local_test_guard.py",
        "git show origin/dev:.github/workflows/test.yml | grep -n 'python -m pytest'",
        "python3 -c \"import pathlib; p = pathlib.Path('t.yml'); p.write_text(p.read_text().replace('pytest -x', 'pytest -q'))\"",
        "python3 - <<'EOF'\nfrom pathlib import Path\np = Path('.github/workflows/test.yml')\nold = '''run: python -m pytest -x'''\n"
        'p.write_text(p.read_text().replace(old, "run: python -m pytest -q"))  # pytest\nEOF',
        "node -e \"fs.writeFileSync('p.json', s.replace('jest', 'vitest')) // jest\"",
        "python3 -c \"print('pytest')\"",
        "python3 -c \"import pathlib; p = pathlib.Path('ecosystem.yml'); p.write_text(p.read_text().replace('pytest -x', 'pytest -q'))\"",
        "python3 -c \"print(open('pytest.ini').read())\"",
        "node -e \"console.log('jest')\"",
        "echo ok #c; pytest -q",
        " # ok; pytest -q",
        "# ok; pytest -q\necho done",
        "python3 -c'print(1)  # pytest'",
        "cat 'a\\' <<EOF\npytest\nEOF",
        "cat \\\\'x' <<EOF\npytest\nEOF",
        "cat $'a' <<EOF\npytest\nEOF",
        "python3 -c \"'" + "\\\\" * 80 + '"',
        "echo \"don't\" # it's\ncat <<EOF\npytest\nEOF",
        "grep -n '^<<<<<<< \\|^=======\\|^>>>>>>> ' .github/workflows/test.yml",
        'git show HEAD:tests/test_a.py | grep -c "<<<<<<< HEAD"',
    ],
)
def test_reads_and_quoted_runner_names_pass(command, monkeypatch):
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


CLEARANCE_WRITER = (
    "python3 - <<'PY'\n"
    "import json\n"
    "from pathlib import Path\n"
    "from scripts.ci_mutation.clearances import write_clearance\n"
    "root = Path('/home/iamroot/dev/worktrees/agentihooks/engineer-323133-0837')\n"
    "records = json.loads(Path('/home/iamroot/scratchpad/agentihooks/rig-grade-swarm-mt3/"
    "standards-equivalence-rulings.json').read_text())\n"
    "for record in records:\n"
    "    write_clearance(root, record['key'], {'reader': 'Standards reader', 'reason': record['reason']})\n"
    "print(len(records))\n"
    "PY"
)


@pytest.mark.parametrize(
    "args",
    [
        {"cmd": CLEARANCE_WRITER},
        {"command": CLEARANCE_WRITER},
        {"cmd": "python3 -m scripts.ci_mutation.clearances"},
    ],
)
def test_native_clearance_writer_passes(args, monkeypatch):
    from hooks.context.local_test_guard import check_local_tests
    from hooks.targets.normalizer import normalize_payload

    monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
    monkeypatch.delenv("AGENTIHOOKS_ALLOW_LOCAL_TEST_RUN", raising=False)
    check_local_tests(normalize_payload({"tool_name": "exec", "tool_input": args}))


@pytest.mark.parametrize(
    "command",
    [
        CLEARANCE_WRITER.removesuffix("PY") + "from scripts.ci_mutation.runner import main\nmain()\nPY",
        CLEARANCE_WRITER.removesuffix("PY") + "import scripts.ci_mutation.selection\nPY",
        CLEARANCE_WRITER.removesuffix("PY") + "import subprocess\nsubprocess.run(['python', '-m', 'pytest'])\nPY",
        CLEARANCE_WRITER.removesuffix("PY") + "import pytest\npytest.main()\nPY",
        CLEARANCE_WRITER + "\npython -m scripts.ci_mutation",
        "python3 -m scripts.ci_mutation",
    ],
)
def test_clearance_writer_mixed_with_a_runner_is_blocked(command, monkeypatch):
    from hooks.context.local_test_guard import check_local_tests
    from hooks.targets.normalizer import normalize_payload

    monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
    monkeypatch.delenv("AGENTIHOOKS_ALLOW_LOCAL_TEST_RUN", raising=False)
    with pytest.raises(BlockAction, match="draft pull request"):
        check_local_tests(normalize_payload({"tool_name": "exec", "tool_input": {"cmd": command}}))


MUTATION_GATE_PATHS = [
    "python scripts/ci_mutation/__main__.py --base origin/dev",
    "python3 scripts/ci_mutation/__main__.py --base origin/dev",
    "python ./scripts/ci_mutation/__main__.py --base origin/dev",
    "python /home/u/dev/agentihooks/scripts/ci_mutation/__main__.py --base origin/dev",
    "python scripts/ci_mutation --base origin/dev",
]


@pytest.mark.parametrize("key", ["command", "cmd"])
@pytest.mark.parametrize("command", MUTATION_GATE_PATHS)
def test_mutation_gate_run_by_its_file_path_is_blocked(command, key, monkeypatch):
    from hooks.context.local_test_guard import check_local_tests

    monkeypatch.delenv("AGENTIHOOKS_ALLOW_LOCAL_TEST_RUN", raising=False)
    with pytest.raises(BlockAction, match="draft pull request"):
        check_local_tests({"tool_name": "Bash", "tool_input": {key: command}})


@pytest.mark.parametrize(
    "command",
    ["python scripts/other.py", "python3 scripts/ci_mutation_notes.py", "python myscripts/ci_mutation/x.py"],
)
def test_unrelated_script_paths_pass(command, monkeypatch):
    from hooks.context.local_test_guard import check_local_tests

    monkeypatch.delenv("AGENTIHOOKS_ALLOW_LOCAL_TEST_RUN", raising=False)
    check_local_tests({"tool_name": "Bash", "tool_input": {"command": command}})


def test_later_commands_and_substitutions_are_checked(monkeypatch):
    from hooks.context.local_test_guard import check_local_tests

    monkeypatch.delenv("AGENTIHOOKS_ALLOW_LOCAL_TEST_RUN", raising=False)
    for command in [
        "echo ok && pytest",
        "echo $(pytest)",
        'echo "$(pytest)"',
        """echo "'$(pytest)'" """,
        """echo "$(env -S 'pytest -q')" """,
        "echo `pytest`",
        "if true; then pytest; fi",
    ]:
        with pytest.raises(BlockAction):
            check_local_tests({"tool_name": "Bash", "tool_input": {"command": command}})


def test_empty_shell_payloads_and_non_shell_tools_pass(monkeypatch):
    from hooks.context.local_test_guard import check_local_tests

    monkeypatch.delenv("AGENTIHOOKS_ALLOW_LOCAL_TEST_RUN", raising=False)
    for payload in [
        {"tool_name": "Bash"},
        {"tool_name": "Bash", "tool_input": {}},
        {"tool_name": "Read", "tool_input": {"command": "pytest"}},
    ]:
        check_local_tests(payload)


def test_invalid_shell_syntax_is_denied_with_ci_guidance(monkeypatch):
    from hooks.context.local_test_guard import check_local_tests

    monkeypatch.delenv("AGENTIHOOKS_ALLOW_LOCAL_TEST_RUN", raising=False)
    with pytest.raises(BlockAction, match="draft pull request.*CI"):
        check_local_tests({"tool_name": "Bash", "tool_input": {"command": 'echo "'}})


def test_pretool_preserves_the_commit_guard_payload(monkeypatch):
    from hooks import config, hook_manager
    from hooks.context import branch_guard

    monkeypatch.setattr(config, "SECRETS_MODE", "off")
    monkeypatch.setattr(config, "QUOTA_POLICY_ENABLED", False)
    monkeypatch.setattr(branch_guard, "check_branch_guard", lambda payload: None)
    payload = {"tool_name": "Bash", "tool_input": {"command": "git commit"}, "session_id": "commit-guard"}

    def commit_guard(received):
        assert received is payload
        raise BlockAction("protected commit")

    monkeypatch.setattr(branch_guard, "check_commit_on_main", commit_guard)
    with pytest.raises(BlockAction, match="protected commit"):
        hook_manager.on_pre_tool_use(payload)


def test_pretool_guard_failure_logs_and_flushes_its_warning(monkeypatch):
    import io
    from unittest.mock import Mock

    from hooks import config, hook_manager
    from hooks.context import branch_guard

    class Output(io.StringIO):
        def __init__(self):
            super().__init__()
            self.flushes = 0

        def flush(self):
            self.flushes += 1
            super().flush()

    for flag in (
        "QUOTA_POLICY_ENABLED",
        "BROADCAST_ENABLED",
        "ENFORCEMENT_INJECTION_ENABLED",
        "RETRY_BREAKER_ENABLED",
        "BRAIN_ENABLED",
    ):
        monkeypatch.setattr(config, flag, False)
    monkeypatch.setattr(config, "SECRETS_MODE", "off")
    log = Mock()
    monkeypatch.setattr(hook_manager, "log", log)
    stderr = Output()
    monkeypatch.setattr(hook_manager.sys, "stderr", stderr)

    def fail(payload):
        raise RuntimeError("offline")

    monkeypatch.setattr(branch_guard, "check_branch_guard", fail)
    hook_manager.on_pre_tool_use(
        {"tool_name": "Bash", "tool_input": {"command": "echo ready"}, "session_id": "guard-failure"}
    )
    log.assert_any_call("branch_guard check failed", {"error": "offline"})
    assert stderr.getvalue() == "WARNING: branch_guard check failed (offline) — guard bypassed\n"
    assert stderr.flushes == 1
