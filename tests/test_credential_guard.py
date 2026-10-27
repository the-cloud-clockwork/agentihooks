"""Credential-read guard behaviour, ported with the guard itself.

These cases came from the bundle hook's own selftest, which ran only on Claude.
The guard now runs on every target, so they run in CI instead of by hand.
"""

import json

import pytest

from hooks.context.credential_guard import (
    ENV_NAMES_SAFE,
    GREP_EXCLUDES,
    KIND_ENVIRONMENT,
    RG_EXCLUDES,
    block,
    decide,
    evaluate,
)

pytestmark = pytest.mark.unit

CASES = [
    # (label, payload, should_block)
    ("read dotenv", {"tool_name": "Read", "tool_input": {"file_path": "/srv/app/.env"}}, True),
    ("read .env.production", {"tool_name": "Read", "tool_input": {"file_path": ".env.production"}}, True),
    ("read stack env", {"tool_name": "Read", "tool_input": {"file_path": "runtime/tracker.env"}}, True),
    ("read .env.example", {"tool_name": "Read", "tool_input": {"file_path": ".env.example"}}, False),
    ("read bashrc", {"tool_name": "Read", "tool_input": {"file_path": "~/.bashrc"}}, True),
    ("read netrc", {"tool_name": "Read", "tool_input": {"file_path": "/home/x/.netrc"}}, True),
    ("read aws creds", {"tool_name": "Read", "tool_input": {"file_path": "~/.aws/credentials"}}, True),
    (
        "read worker grant",
        {"tool_name": "Read", "tool_input": {"file_path": "/home/worker/attempts/exe-1/run/launch-grant"}},
        True,
    ),
    ("grep in worker grant", {"tool_name": "Grep", "tool_input": {"pattern": "v2", "path": "run/launch-grant"}}, True),
    ("cat worker grant", {"tool_name": "Bash", "tool_input": {"command": "cat run/launch-grant"}}, True),
    ("grep worker grant", {"tool_name": "Bash", "tool_input": {"command": "grep v2 ./launch-grant"}}, True),
    (
        "python reads worker grant",
        {"tool_name": "Bash", "tool_input": {"command": "python3 -c \"print(open('run/launch-grant').read())\""}},
        True,
    ),
    ("cat glob of worker grant", {"tool_name": "Bash", "tool_input": {"command": "cat run/launch-*"}}, True),
    ("ls worker grant", {"tool_name": "Bash", "tool_input": {"command": "ls -la run/launch-grant"}}, False),
    ("read launch module", {"tool_name": "Read", "tool_input": {"file_path": "scripts/swarm_v2/launch.py"}}, False),
    ("read source file", {"tool_name": "Read", "tool_input": {"file_path": "hooks/secrets.py"}}, False),
    ("grep in dotenv", {"tool_name": "Grep", "tool_input": {"pattern": "URL", "path": ".env"}}, True),
    ("grep in src", {"tool_name": "Grep", "tool_input": {"pattern": "URL", "path": "src/"}}, False),
    ("glob untouched", {"tool_name": "Glob", "tool_input": {"pattern": "**/.env"}}, False),
    ("write untouched", {"tool_name": "Write", "tool_input": {"file_path": ".env"}}, False),
    ("cat dotenv", {"tool_name": "Bash", "tool_input": {"command": "cat .env"}}, True),
    ("source bashrc", {"tool_name": "Bash", "tool_input": {"command": "source ~/.bashrc"}}, True),
    ("dot-source dotenv", {"tool_name": "Bash", "tool_input": {"command": ". ./.env"}}, True),
    ("cat example", {"tool_name": "Bash", "tool_input": {"command": "cat .env.example"}}, False),
    ("ls dotenv", {"tool_name": "Bash", "tool_input": {"command": "ls -la .env"}}, False),
    ("wc dotenv", {"tool_name": "Bash", "tool_input": {"command": "wc -l ~/.bashrc"}}, False),
    ("test -f dotenv", {"tool_name": "Bash", "tool_input": {"command": "test -f .env && echo yes"}}, False),
    ("grep -c bashrc", {"tool_name": "Bash", "tool_input": {"command": "grep -c export ~/.bashrc"}}, False),
    ("grep content bashrc", {"tool_name": "Bash", "tool_input": {"command": "grep export ~/.bashrc"}}, True),
    ("bare env", {"tool_name": "Bash", "tool_input": {"command": "env"}}, True),
    ("bare printenv", {"tool_name": "Bash", "tool_input": {"command": "printenv"}}, True),
    ("export -p", {"tool_name": "Bash", "tool_input": {"command": "export -p"}}, True),
    ("env names", {"tool_name": "Bash", "tool_input": {"command": "env | cut -d= -f1"}}, True),
    ("env count", {"tool_name": "Bash", "tool_input": {"command": "env | wc -l"}}, False),
    ("env grep value", {"tool_name": "Bash", "tool_input": {"command": "env | grep REDIS"}}, True),
    ("env grep count", {"tool_name": "Bash", "tool_input": {"command": "env | grep -c REDIS"}}, False),
    ("env grep color", {"tool_name": "Bash", "tool_input": {"command": "env | grep --color=never ."}}, True),
    ("env grep long option", {"tool_name": "Bash", "tool_input": {"command": "env | grep --line-buffered ."}}, True),
    ("env grep pattern c", {"tool_name": "Bash", "tool_input": {"command": "env | grep -ec ."}}, True),
    ("env grep list", {"tool_name": "Bash", "tool_input": {"command": "env | grep -l ."}}, True),
    ("env wc names from input", {"tool_name": "Bash", "tool_input": {"command": "env | wc --files0-from=-"}}, True),
    ("env names then head", {"tool_name": "Bash", "tool_input": {"command": "env | cut -d= -f1 | head -3"}}, True),
    ("env sorted names", {"tool_name": "Bash", "tool_input": {"command": "env | sort | cut -d= -f1"}}, True),
    ("env awk names", {"tool_name": "Bash", "tool_input": {"command": "env | awk -F= '{print $1}'"}}, True),
    ("printenv names", {"tool_name": "Bash", "tool_input": {"command": "printenv | sed 's/=.*//'"}}, True),
    ("set names", {"tool_name": "Bash", "tool_input": {"command": "set | cut -d= -f1"}}, True),
    ("export -p names", {"tool_name": "Bash", "tool_input": {"command": "export -p | cut -d= -f1"}}, True),
    ("declare -x names", {"tool_name": "Bash", "tool_input": {"command": "declare -x | cut -d= -f1"}}, True),
    (
        "env names filtered to identifiers",
        {"tool_name": "Bash", "tool_input": {"command": "env | cut -d= -f1 | grep -xE '[A-Za-z_][A-Za-z0-9_]*'"}},
        True,
    ),
    (
        "env names filtered anchored",
        {
            "tool_name": "Bash",
            "tool_input": {"command": "env | sort | cut -d= -f1 | grep -E '^[A-Za-z_][A-Za-z0-9_]*$' | head -3"},
        },
        True,
    ),
    (
        "env malformed names printed",
        {"tool_name": "Bash", "tool_input": {"command": "env | cut -d= -f1 | grep -vxE '[A-Za-z_][A-Za-z0-9_]*'"}},
        True,
    ),
    (
        "env malformed names counted",
        {"tool_name": "Bash", "tool_input": {"command": "env | cut -d= -f1 | grep -cvxE '[A-Za-z_][A-Za-z0-9_]*'"}},
        False,
    ),
    (
        "env identifier lines without name stage",
        {"tool_name": "Bash", "tool_input": {"command": "env | grep -xE '[A-Za-z_][A-Za-z0-9_]*'"}},
        True,
    ),
    (
        "env names loose filter",
        {"tool_name": "Bash", "tool_input": {"command": "env | cut -d= -f1 | grep -E '[A-Za-z_]+'"}},
        True,
    ),
    ("compgen exported names", {"tool_name": "Bash", "tool_input": {"command": "compgen -e"}}, False),
    ("safe env names form", {"tool_name": "Bash", "tool_input": {"command": ENV_NAMES_SAFE}}, False),
    ("env head raw", {"tool_name": "Bash", "tool_input": {"command": "env | head -3"}}, True),
    ("printenv PATH", {"tool_name": "Bash", "tool_input": {"command": "printenv PATH"}}, False),
    ("printenv secret", {"tool_name": "Bash", "tool_input": {"command": "printenv DB_PASSWORD"}}, True),
    ("echo secret var", {"tool_name": "Bash", "tool_input": {"command": 'echo "$KB_ROUTER_TOKEN"'}}, True),
    ("echo plain var", {"tool_name": "Bash", "tool_input": {"command": 'echo "$HOME"'}}, False),
    (
        "curl with token",
        {"tool_name": "Bash", "tool_input": {"command": 'curl -H "Authorization: Bearer $API_TOKEN" http://x'}},
        False,
    ),
    ("docker exec cat env", {"tool_name": "Bash", "tool_input": {"command": "docker exec app cat /app/.env"}}, True),
    (
        "kubectl exec cat env",
        {"tool_name": "Bash", "tool_input": {"command": "kubectl exec pod -- cat /app/.env"}},
        True,
    ),
    ("ssh cat bashrc", {"tool_name": "Bash", "tool_input": {"command": "ssh host cat ~/.bashrc"}}, True),
    ("compose config", {"tool_name": "Bash", "tool_input": {"command": "docker compose config"}}, True),
    (
        "compose no-interpolate",
        {"tool_name": "Bash", "tool_input": {"command": "docker compose config --no-interpolate"}},
        False,
    ),
    ("docker inspect bare", {"tool_name": "Bash", "tool_input": {"command": "docker inspect litellm"}}, True),
    (
        "docker inspect format",
        {"tool_name": "Bash", "tool_input": {"command": "docker inspect --format '{{.State.Status}}' litellm"}},
        False,
    ),
    ("kubectl secret yaml", {"tool_name": "Bash", "tool_input": {"command": "kubectl get secret db -o yaml"}}, True),
    ("kubectl secret list", {"tool_name": "Bash", "tool_input": {"command": "kubectl get secrets -n prod"}}, False),
    ("kubectl describe secret", {"tool_name": "Bash", "tool_input": {"command": "kubectl describe secret db"}}, False),
    ("access env printed", {"tool_name": "Bash", "tool_input": {"command": "agentihooks access env"}}, True),
    (
        "access env module printed",
        {"tool_name": "Bash", "tool_input": {"command": "/v/bin/python -m scripts.access_env env | cat"}},
        True,
    ),
    (
        "access env captured",
        {"tool_name": "Bash", "tool_input": {"command": 'x="$(agentihooks access env)"; echo "$x"'}},
        True,
    ),
    (
        "access env evaluated",
        {"tool_name": "Bash", "tool_input": {"command": 'eval "$(agentihooks access env)" && test -n "$A"'}},
        False,
    ),
    (
        "access env module evaluated",
        {"tool_name": "Bash", "tool_input": {"command": 'eval "$(/v/bin/python -m scripts.access_env env)"'}},
        False,
    ),
    (
        "access env evaluated then printed",
        {"tool_name": "Bash", "tool_input": {"command": 'eval "$(agentihooks access env)"; agentihooks access env'}},
        True,
    ),
    ("access help", {"tool_name": "Bash", "tool_input": {"command": "agentihooks access envelope"}}, False),
    (
        "access env called from python",
        {
            "tool_name": "Bash",
            "tool_input": {"command": "python3 -c 'from scripts import access_env; access_env.main([\"env\"])'"},
        },
        True,
    ),
    (
        "access env values from python",
        {"tool_name": "Bash", "tool_input": {"command": "python3 -c 'print(access_env.values (env))'"}},
        True,
    ),
    (
        "access env source linted",
        {"tool_name": "Bash", "tool_input": {"command": "ruff check scripts/access_env.py"}},
        False,
    ),
    (
        "python calls env helper",
        {"tool_name": "Bash", "tool_input": {"command": 'python3 -c "auth.env_text(record)"'}},
        False,
    ),
    (
        "python references env attribute",
        {"tool_name": "Bash", "tool_input": {"command": 'python3 -c "print(auth.env_name)"'}},
        False,
    ),
    (
        "python references credential attribute",
        {"tool_name": "Bash", "tool_input": {"command": 'python3 -c "print(auth.netrc_path)"'}},
        False,
    ),
    (
        "python reads member dotenv",
        {"tool_name": "Bash", "tool_input": {"command": "python3 -c \"print(open('auth.env').read())\""}},
        True,
    ),
    (
        "python reads dotenv beside helper call",
        {"tool_name": "Bash", "tool_input": {"command": "python3 -c \"auth.env_text(open('/app/auth.env').read())\""}},
        True,
    ),
    (
        "python reads named dotenv variant",
        {
            "tool_name": "Bash",
            "tool_input": {"command": "python3 -c \"print(open('/app/.env.production-local').read())\""},
        },
        True,
    ),
    (
        "python reads shell config",
        {"tool_name": "Bash", "tool_input": {"command": "python3 -c \"print(open('/home/user/.bashrc').read())\""}},
        True,
    ),
    (
        "python reads credential file",
        {"tool_name": "Bash", "tool_input": {"command": "python3 -c \"print(open('/home/user/.netrc').read())\""}},
        True,
    ),
    (
        "python opens dotenv",
        {"tool_name": "Bash", "tool_input": {"command": "python3 -c \"print(open('.env').read())\""}},
        True,
    ),
    ("chained cat dotenv", {"tool_name": "Bash", "tool_input": {"command": "cd /app && cat .env"}}, True),
    ("git status", {"tool_name": "Bash", "tool_input": {"command": "git status --short"}}, False),
    ("kubectl logs", {"tool_name": "Bash", "tool_input": {"command": "kubectl logs -f pod-0 -n ns"}}, False),
    ("empty command", {"tool_name": "Bash", "tool_input": {"command": ""}}, False),
    ("no tool_input", {"tool_name": "Bash"}, False),
    ("unknown tool", {"tool_name": "WebFetch", "tool_input": {"url": "http://x/.env"}}, False),
    ("malformed payload", {}, False),
    (
        "heredoc mentioning a renderer",
        {
            "tool_name": "Bash",
            "tool_input": {"command": "python3 - <<'PY'\nprint('" + "docker " + "compose config')\nPY"},
        },
        False,
    ),
    (
        "heredoc mentioning a dotenv",
        {"tool_name": "Bash", "tool_input": {"command": "python3 - <<'PY'\nprint('read the .env file')\nPY"}},
        False,
    ),
    # naming a credential file is not reading it
    (
        "find names dotenv",
        {"tool_name": "Bash", "tool_input": {"command": 'find . -name ".env" | grep -v example'}},
        False,
    ),
    (
        "grep regex in source",
        {"tool_name": "Bash", "tool_input": {"command": "grep -rn '\\.env' scripts/install.py"}},
        False,
    ),
    (
        "find split quotes",
        {"tool_name": "Bash", "tool_input": {"command": "find . -name '.en''v' -printf '%h\\n' | sort -u"}},
        False,
    ),
    ("rg absolute dir", {"tool_name": "Bash", "tool_input": {"command": "rg -n pat /abs/dir"}}, False),
    ("ls dot", {"tool_name": "Bash", "tool_input": {"command": "ls -la ."}}, False),
    (
        "grep secret name in source",
        {"tool_name": "Bash", "tool_input": {"command": "grep -rn 'API_KEY' src/main.py"}},
        False,
    ),
    ("find py xargs cat", {"tool_name": "Bash", "tool_input": {"command": "find . -name '*.py' | xargs cat"}}, False),
    ("echo dot", {"tool_name": "Bash", "tool_input": {"command": "echo . && ls"}}, False),
    ("grep exclude dotenv", {"tool_name": "Bash", "tool_input": {"command": "grep -rn --exclude=.env X ."}}, False),
    ("rg negative glob", {"tool_name": "Bash", "tool_input": {"command": "rg -g '!.env' X ."}}, False),
    ("git add dotenv", {"tool_name": "Bash", "tool_input": {"command": "git add .env"}}, False),
    ("docker env-file", {"tool_name": "Bash", "tool_input": {"command": "docker run --env-file .env img"}}, False),
    # reads that the operand-aware rules must still catch
    ("cat redirect dotenv", {"tool_name": "Bash", "tool_input": {"command": "cat < .env"}}, True),
    ("cat attached redirect", {"tool_name": "Bash", "tool_input": {"command": "cat <.env"}}, True),
    ("grep in dotenv file", {"tool_name": "Bash", "tool_input": {"command": "grep TOKEN .env"}}, True),
    ("git diff no-index", {"tool_name": "Bash", "tool_input": {"command": "git diff --no-index .env x"}}, True),
    ("git show rev path", {"tool_name": "Bash", "tool_input": {"command": "git show HEAD:.env"}}, True),
    ("git grep dotenv", {"tool_name": "Bash", "tool_input": {"command": "git grep X -- .env"}}, True),
    ("git -C diff dotenv", {"tool_name": "Bash", "tool_input": {"command": "git -C /repo diff .env"}}, True),
    (
        "git blame option value",
        {"tool_name": "Bash", "tool_input": {"command": "git blame --ignore-revs-file=.env f"}},
        True,
    ),
    ("bash -c cat", {"tool_name": "Bash", "tool_input": {"command": "bash -c 'cat .env'"}}, True),
    ("source zshrc", {"tool_name": "Bash", "tool_input": {"command": "source ~/.zshrc"}}, True),
    ("find dotenv xargs cat", {"tool_name": "Bash", "tool_input": {"command": "find . -name .env | xargs cat"}}, True),
    (
        "find dotenv exec cat",
        {"tool_name": "Bash", "tool_input": {"command": "find . -name '.env*' -exec cat {} \\;"}},
        True,
    ),
    ("process substitution", {"tool_name": "Bash", "tool_input": {"command": "awk 1 <(cat .env)"}}, True),
    ("dollar redirect", {"tool_name": "Bash", "tool_input": {"command": 'echo "$(<.env)"'}}, True),
    ("command substitution", {"tool_name": "Bash", "tool_input": {"command": 'echo "$(cat .env)"'}}, True),
    ("cat dotfile glob", {"tool_name": "Bash", "tool_input": {"command": "cat .*"}}, True),
    ("grep -r home", {"tool_name": "Bash", "tool_input": {"command": "grep -r X ~"}}, True),
    ("grep -f attached", {"tool_name": "Bash", "tool_input": {"command": "sed -n 1p -f.env x"}}, True),
    ("ssh sudo cat", {"tool_name": "Bash", "tool_input": {"command": "ssh host sudo cat /root/.netrc"}}, True),
]


@pytest.mark.parametrize("label,payload,should_block", CASES, ids=[c[0] for c in CASES])
def test_guard_decision(label, payload, should_block):
    verdict = decide(payload)
    assert (verdict is not None) is should_block


def test_guard_never_raises_on_garbage():
    for payload in ({}, {"tool_name": None}, {"tool_input": "notadict"}, {"tool_name": "Bash", "tool_input": None}):
        decide(payload)
        evaluate(payload, allow_rewrite=True)


def test_access_env_block_names_the_token_and_the_eval_form():
    reason = decide({"tool_name": "Bash", "tool_input": {"command": "agentihooks access env"}})

    assert reason == (
        block("printing the Cloudflare Access service token (agentihooks access env)", KIND_ENVIRONMENT)
        + '\nEvaluate it instead:  eval "$(agentihooks access env)", then test each variable by name.'
    )
    assert reason.startswith("BLOCKED: printing the Cloudflare Access service token (agentihooks access env).\n")
    assert reason != block("printing the Cloudflare Access service token (agentihooks access env)", None) + (
        '\nEvaluate it instead:  eval "$(agentihooks access env)", then test each variable by name.'
    )


GH_TOKEN_RENDERERS = [
    "gh auth status",
    "gh auth status --show-token",
    "gh auth status -t",
    "gh auth status -h github.com",
    "gh --hostname github.com auth status",
    "gh auth token",
    "/usr/bin/gh auth status",
    "sudo gh auth status",
    "GH_TOKEN=x gh auth status",
    "env GH_HOST=github.com gh auth token",
    "gh auth status 2>&1 | head -5",
    "cd /repo && gh auth status",
    'echo "$(gh auth token)"',
    "bash -c 'gh auth status'",
    "ssh hive-002 gh auth status",
    "docker exec worker gh auth status",
    "kubectl exec pod-0 -- gh auth status",
    "gh -R owner/repo auth token",
]

GH_LEAK_FREE = [
    "gh auth login --with-token < /dev/null",
    "gh auth setup-git",
    "gh pr view 7843",
    "gh pr list --search auth token",
    'gh issue comment 1 --body "auth status"',
    "gh api rate_limit --jq .rate.remaining",
    'test -n "${GH_TOKEN:-}" && echo set || echo unset',
    "grep -rn 'gh auth status' docs",
    "git log --grep 'gh auth token'",
]


@pytest.mark.parametrize("command", GH_TOKEN_RENDERERS)
def test_gh_commands_that_print_a_token_are_refused(command):
    reason = decide({"tool_name": "Bash", "tool_input": {"command": command}})

    assert reason is not None
    assert reason.startswith("BLOCKED: printing the GitHub token (gh auth status or gh auth token).\n")
    assert 'test -n "${GH_TOKEN:-}"' in reason
    assert "gh api rate_limit" in reason


@pytest.mark.parametrize("command", GH_LEAK_FREE)
def test_gh_commands_that_print_no_token_pass(command):
    assert decide({"tool_name": "Bash", "tool_input": {"command": command}}) is None


def _bash(command, cwd=""):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}


class TestEnvironmentNames:
    """A malformed variable name can hold a secret, so a name listing must drop it."""

    FAKE_NAME = "fake-leaked-name-0000"
    FAKE_VALUE = "fake-value-0000"

    def _run_safe_form(self):
        import os
        import subprocess

        env = {"PATH": os.environ.get("PATH", ""), "VALID_NAME": "x", self.FAKE_NAME: self.FAKE_VALUE}
        return subprocess.run(["bash", "-c", ENV_NAMES_SAFE], env=env, capture_output=True, text=True)

    def test_block_message_gives_the_safe_form(self):
        reason = decide(_bash("env | cut -d= -f1"))
        assert ENV_NAMES_SAFE in reason
        assert "env | cut -d= -f1  " not in reason

    def test_safe_form_lists_valid_names_and_counts_malformed(self):
        r = self._run_safe_form()
        lines = r.stdout.splitlines()
        assert r.returncode == 0
        assert "VALID_NAME" in lines
        assert "malformed names: 1" in lines

    def test_safe_form_never_prints_the_malformed_name_or_value(self):
        r = self._run_safe_form()
        assert self.FAKE_NAME not in r.stdout + r.stderr
        assert self.FAKE_VALUE not in r.stdout + r.stderr


class TestMultilineEnvValues:
    """A value spanning two lines turns its second line into a fake name in line based listings."""

    FAKE_FIRST = "fake-first-line-0000"
    FAKE_REST = "fake-rest-line-0000"
    LISTING = "env | cut -d= -f1"

    def _run(self, command):
        import os
        import subprocess

        env = {"PATH": os.environ.get("PATH", ""), "FAKE_MULTI_0001": self.FAKE_FIRST + "\n" + self.FAKE_REST}
        return subprocess.run(["bash", "-c", command], env=env, capture_output=True, text=True)

    def test_unguarded_listing_would_print_the_second_line(self):
        assert self.FAKE_REST in self._run(self.LISTING).stdout.splitlines()

    def test_guard_blocks_the_listing(self):
        assert decide(_bash(self.LISTING))

    def test_block_message_names_multiline_values(self):
        assert "several lines" in decide(_bash(self.LISTING))

    def test_safe_form_never_prints_either_line(self):
        r = self._run(ENV_NAMES_SAFE)
        assert "FAKE_MULTI_0001" in r.stdout.splitlines()
        assert self.FAKE_FIRST not in r.stdout + r.stderr
        assert self.FAKE_REST not in r.stdout + r.stderr


class TestIdentifierShapedSecondLine:
    """A value's later line can look like a name, so no identifier filter makes a listing safe."""

    FAKE_FIRST = "fake-first-line-0000"
    FAKE_REST = "FAKE_REST_LINE_0000"
    FILTERED = "env | cut -d= -f1 | grep -xE '[A-Za-z_][A-Za-z0-9_]*'"

    def _run(self, command):
        import os
        import subprocess

        env = {"PATH": os.environ.get("PATH", ""), "FAKE_MULTI_0002": self.FAKE_FIRST + "\n" + self.FAKE_REST}
        return subprocess.run(["bash", "-c", command], env=env, capture_output=True, text=True)

    def test_unguarded_filtered_listing_would_print_the_second_line(self):
        assert self.FAKE_REST in self._run(self.FILTERED).stdout.splitlines()

    def test_guard_blocks_the_filtered_listing(self):
        assert decide(_bash(self.FILTERED))

    def test_safe_form_never_prints_the_second_line(self):
        r = self._run(ENV_NAMES_SAFE)
        assert "FAKE_MULTI_0002" in r.stdout.splitlines()
        assert self.FAKE_REST not in r.stdout + r.stderr

    def test_block_message_offers_only_the_awk_form_and_counts(self):
        reason = decide(_bash(self.FILTERED))
        assert ENV_NAMES_SAFE in reason
        assert "grep -c" in reason
        assert "grep -x" not in reason


class TestGrepLongOptionIsNotACount:
    """A long grep option holding c, l or q once passed as a count and printed values."""

    FAKE_VALUE = "fake-value-long-option-0000"
    COMMAND = "env | grep --color=never ."

    def test_unguarded_command_would_print_the_value(self):
        import os
        import subprocess

        env = {"PATH": os.environ.get("PATH", ""), "FAKE_TOKEN_0003": self.FAKE_VALUE}
        r = subprocess.run(["bash", "-c", self.COMMAND], env=env, capture_output=True, text=True)
        assert "FAKE_TOKEN_0003=" + self.FAKE_VALUE in r.stdout.splitlines()

    def test_guard_blocks_it(self):
        assert decide(_bash(self.COMMAND))


class TestRecursiveRewrite:
    """Under bypass on claude the search keeps running; credential files leave it."""

    def test_grep_r_gains_excludes(self):
        v = evaluate(_bash("grep -rn X ."), allow_rewrite=True)
        assert v.block is None
        assert v.rewrite == {"command": "grep " + GREP_EXCLUDES + " -rn X ."}

    def test_rg_gains_negative_globs(self):
        v = evaluate(_bash("rg foo ."), allow_rewrite=True)
        assert v.rewrite == {"command": "rg " + RG_EXCLUDES + " foo ."}

    def test_compound_keeps_its_cd(self):
        v = evaluate(_bash("cd src && grep -r X . | head -5"), allow_rewrite=True)
        assert v.rewrite == {"command": "cd src && grep " + GREP_EXCLUDES + " -r X . | head -5"}

    def test_env_prefix_and_path_survive(self):
        v = evaluate(_bash("LC_ALL=C /usr/bin/grep -R X src"), allow_rewrite=True)
        assert v.rewrite == {"command": "LC_ALL=C /usr/bin/grep " + GREP_EXCLUDES + " -R X src"}

    def test_excludes_never_name_a_denied_home_path(self):
        # An option value is a file operand to the harness: --exclude=.bashrc
        # matches Read(~/.bashrc) and prompts. Keep those basenames out.
        from hooks.context.credential_guard import SHELL_RC

        for rc in SHELL_RC:
            assert rc not in GREP_EXCLUDES, rc
            assert rc not in RG_EXCLUDES, rc

    def test_the_grant_file_the_worker_home_writes_is_a_credential(self, tmp_path):
        from hooks.context.credential_guard import KIND_CREDENTIAL, sensitive_kind
        from scripts.swarm_v2.broadcast_bridge import grant_path

        assert sensitive_kind(str(grant_path(tmp_path / "exe-1"))) == KIND_CREDENTIAL

    def test_a_tool_input_naming_the_grant_file_is_near_a_credential(self):
        from hooks.context.credential_guard import near_credential
        from scripts.swarm_v2.broadcast_bridge import GRANT_NAME

        assert near_credential({"command": f"python3 -c \"open('run/{GRANT_NAME}')\""})
        assert not near_credential({"command": "python3 -c \"open('run/launch')\""})

    def test_recursive_search_leaves_the_worker_grant_out(self):
        from scripts.swarm_v2.broadcast_bridge import GRANT_NAME

        assert "--exclude=" + GRANT_NAME in GREP_EXCLUDES.split()
        assert "'!" + GRANT_NAME + "'" in RG_EXCLUDES.split()

    def test_quoted_delimiters_are_data(self):
        cmd = 'echo "a && grep -r x ." && git commit -m "b | rg y ."'
        assert evaluate(_bash(cmd), allow_rewrite=True).rewrite is None
        assert decide(_bash('echo "x; cat .env"')) is None

    def test_non_recursive_grep_untouched(self):
        assert evaluate(_bash("grep -n X file.py"), allow_rewrite=True) == (None, None, None)

    def test_filename_only_grep_untouched(self):
        assert evaluate(_bash("grep -rl X ."), allow_rewrite=True).rewrite is None

    def test_file_operands_untouched(self, tmp_path):
        (tmp_path / "a.py").write_text("x")
        assert evaluate(_bash("grep -rn X a.py", cwd=str(tmp_path)), allow_rewrite=True).rewrite is None

    def test_decide_never_rewrites(self):
        assert decide(_bash("grep -rn X .")) is None


class TestRecursiveTreeScan:
    """Without the rewrite channel, a tree holding a credential file is blocked."""

    def test_dotenv_in_tree_blocks(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / ".env").write_text("A=1\n")
        v = evaluate(_bash("grep -rn X .", cwd=str(tmp_path)))
        assert v.block and "sub/.env" in v.block and "--exclude=.env" in v.block

    def test_clean_tree_allows(self, tmp_path):
        (tmp_path / "a.py").write_text("x")
        assert evaluate(_bash("rg X .", cwd=str(tmp_path))) == (None, None, None)

    def test_pruned_dirs_are_skipped(self, tmp_path):
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / ".env").write_text("A=1\n")
        assert evaluate(_bash("grep -r X .", cwd=str(tmp_path))).block is None

    def test_cd_moves_the_root(self, tmp_path):
        (tmp_path / "app").mkdir()
        (tmp_path / "app" / ".env").write_text("A=1\n")
        assert evaluate(_bash("cd app && grep -r X .", cwd=str(tmp_path))).block
        assert evaluate(_bash("grep -r X docs", cwd=str(tmp_path))).block is None

    def test_find_without_name_filter_scans(self, tmp_path):
        (tmp_path / ".env").write_text("A=1\n")
        assert evaluate(_bash("find . -type f | xargs cat", cwd=str(tmp_path))).block
        assert evaluate(_bash("find . -name '*.py' | xargs cat", cwd=str(tmp_path))).block is None

    def test_decide_skips_the_walk(self, tmp_path):
        (tmp_path / ".env").write_text("A=1\n")
        assert decide(_bash("grep -rn X .", cwd=str(tmp_path))) is None


class TestStandaloneLauncher:
    """The bundle runs this file by path as a second process."""

    GUARD = str(__import__("pathlib").Path(__file__).resolve().parents[1] / "hooks" / "context" / "credential_guard.py")

    def _run(self, payload, tmp_path, env=None):
        import os
        import subprocess
        import sys

        return subprocess.run(
            [sys.executable, self.GUARD, "--deny-only"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
            env={**os.environ, "PYTHONPATH": "", **(env or {})},
        )

    def test_explicit_read_exits_2(self, tmp_path):
        r = self._run(_bash("cat .env"), tmp_path)
        assert r.returncode == 2 and "BLOCKED" in r.stderr and r.stdout == ""

    def test_search_exits_0(self, tmp_path):
        (tmp_path / ".env").write_text("A=1\n")
        assert self._run(_bash("grep -rn X .", cwd=str(tmp_path)), tmp_path).returncode == 0

    def test_switch_off(self, tmp_path):
        assert self._run(_bash("cat .env"), tmp_path, {"CREDENTIAL_GUARD_ENABLED": "false"}).returncode == 0

    def test_garbage_near_credential_exits_2(self, tmp_path):
        import subprocess
        import sys

        r = subprocess.run(
            [sys.executable, self.GUARD],
            input='{"tool_name": "Bash", "tool_input": {"command": "cat .env"',
            capture_output=True,
            text=True,
        )
        assert r.returncode == 2 and "internal error" in r.stderr

    def test_garbage_elsewhere_exits_0(self, tmp_path):
        import subprocess
        import sys

        r = subprocess.run([sys.executable, self.GUARD], input="{not json", capture_output=True, text=True)
        assert r.returncode == 0


class TestRunsOnEveryTarget:
    """The whole point of the port: this reached Claude only before."""

    def test_copilot_normalized_payload_blocks(self):
        # what hooks.targets.normalizer produces for copilot's `bash` tool
        assert decide({"tool_name": "Bash", "tool_input": {"command": "cat .env"}}) is not None

    def test_template_files_still_readable(self):
        assert decide({"tool_name": "Bash", "tool_input": {"command": "cat .env.example"}}) is None


PROC_ENVIRON_READS = [
    "tr '\\0' '\\n' < /proc/12345/environ | grep ^AGENTIHOOKS_SWARM",
    "cat /proc/self/environ",
    "cat /proc/$PID/environ",
    "strings /proc/1/environ",
    "grep -a REDIS /proc/1/environ",
    "xargs -0 -n1 -a /proc/1/environ echo",
    "ls /proc/*/environ | xargs cat",
    "python3 -c \"print(open('/proc/1/environ').read())\"",
    "python3 -c \"import os; print(open(os.path.join('/proc', '1', 'environ')).read())\"",
    "cd /proc/1 && cat environ",
    "while read -d '' l; do echo $l; done < /proc/thread-self/environ",
    "python3 <<'EOF'\nprint(open(f'/proc/{1}/environ').read())\nEOF",
    "cat /proc/1/envi*",
    "cat /proc/1/envir?n",
    "cat /proc/1/env''iron",
    "cat /proc/1/env\\iron",
    "f=environ; cat /proc/1/$f",
    "cat /proc/1/*",
    "cat /proc/1/env[i]ron",
    "a=envi; b=ron; cat /proc/1/$a$b",
    "cat /proc/1/$(printf envi)ron",
    "cat /proc/1/`printf envi`ron",
    "cd /proc/1 && cat e*",
    "cd /proc/1; cat *",
    "pushd /proc/self && cat env*",
    "cd -- /proc/1 && cat e*",
    "cd -L /proc/1 && cat e*",
    "cd /p*/1 && cat e*",
    "cd /p*c/1 && cat environ",
    "cat /p*c/1/environ|head",
    "cat /proc/1/envi*|tr '\\0' '\\n'",
    "cat /proc/1/env[i]ron;echo",
    "cat $(cat /pr?c/self/environ)",
    "cat /p*c/1/environ",
    "cat /pr?c/self/environ",
    "cat /pr[o]c/1/environ",
    "P=/pro; cat ${P}c/1/environ",
]

PROC_ALLOWED = [
    "cat /proc/meminfo",
    "python3 -c \"import os; print(os.listdir('/proc/1'))\"",
    'test -n "${REDIS_URL:-}" && echo set || echo unset',
    "ls /proc/1/status",
    "cat /proc/*/status",
    "test -d /proc/$pid && echo alive",
    "cd /home/x/procs && cat e*",
    "cd Xproc && cat environ",
    "cat build/environ",
]


class TestProcEnvironReads:
    @pytest.mark.parametrize("command", PROC_ENVIRON_READS)
    def test_read_is_refused_with_the_leak_free_alternative(self, command):
        verdict = decide(_bash(command))
        assert verdict is not None
        assert verdict.splitlines()[0] == "BLOCKED: reading a process environment file under /proc."
        assert 'test -n "${VAR:-}"' in verdict
        assert ENV_NAMES_SAFE in verdict

    @pytest.mark.parametrize("command", PROC_ALLOWED)
    def test_neighbouring_commands_still_pass(self, command):
        assert decide(_bash(command)) is None

    @pytest.mark.parametrize("command", ["cat environ", "cat envi*", "cat $f", "cat *"])
    def test_read_from_a_proc_working_directory_is_refused(self, command):
        verdict = decide(_bash(command, cwd="/proc/1"))
        assert verdict is not None
        assert ENV_NAMES_SAFE in verdict

    def test_a_proc_working_directory_still_allows_other_files(self):
        assert decide(_bash("cat status", cwd="/proc/1")) is None
        assert decide(_bash("cat environ", cwd="/srv/procfiles")) is None

    @pytest.mark.parametrize("path", ["/proc/1/environ", "//proc/1/environ"])
    def test_read_tool_on_proc_environ_is_refused(self, path):
        verdict = decide({"tool_name": "Read", "tool_input": {"file_path": path}})
        assert verdict is not None
        assert ENV_NAMES_SAFE in verdict

    @pytest.mark.parametrize("path", ["docs/environ", "proc/1/environ", "/X/proc/1/environ"])
    def test_read_tool_on_an_environ_file_elsewhere_passes(self, path):
        assert decide({"tool_name": "Read", "tool_input": {"file_path": path}}) is None

    def test_grep_tool_on_proc_environ_is_refused(self):
        verdict = decide({"tool_name": "Grep", "tool_input": {"pattern": "REDIS", "path": "/proc/1/environ"}})
        assert verdict is not None
        assert verdict.splitlines()[0] == "BLOCKED: searching the contents of /proc/1/environ."
        assert 'test -n "${VAR:-}"' in verdict
        assert ENV_NAMES_SAFE in verdict
