"""Property and oracle tests for the signal matcher, branch guard, secrets scan and credential guard.

Oracle expectations are worked out from the CI manifesto (§5, §8) and the
credential files rule, never read back from the code under test. Secret shaped
literals are assembled at runtime so this file matches no detection pattern.
"""

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import hooks.context.ci_manifesto as ci_manifesto
from hooks import secrets
from hooks.context.branch_guard import check_branch_guard
from hooks.context.credential_guard import (
    KIND_CREDENTIAL,
    KIND_DOTENV,
    KIND_ENVIRONMENT,
    KIND_SHELLRC,
    sensitive_kind,
)
from hooks.hook_manager import BlockAction

pytestmark = pytest.mark.unit

ALL_SIGNALS = (
    ci_manifesto._DEFAULT_RELEASE_SIGNALS
    + ci_manifesto._DEFAULT_HOTFIX_SIGNALS
    + ci_manifesto._DEFAULT_BRANCH_SIGNALS
    + ci_manifesto._DEFAULT_PR_SIGNALS
)
NEGATIONS = ["don't", "dont", "do not", "not", "never", "should not", "won't", "will not", "can't", "cannot"]
NEUTRAL_WORDS = ["please", "ok", "now", "sure", "then", "go", "yes"]
TEMPLATE_SUFFIXES = ["example", "sample", "template", "dist", "tpl"]

property_settings = settings(database=None, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])


def recased(text):
    return st.lists(st.booleans(), min_size=len(text), max_size=len(text)).map(
        lambda flags: "".join(ch.upper() if up else ch.lower() for ch, up in zip(text, flags))
    )


@st.composite
def recased_signal(draw):
    return draw(recased(draw(st.sampled_from(ALL_SIGNALS))))


neutral_words = st.lists(st.sampled_from(NEUTRAL_WORDS), max_size=4).map(" ".join)
ref_names = st.from_regex(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,15}", fullmatch=True).filter(
    lambda name: name not in {"main", "master"}
)
path_segments = st.lists(st.from_regex(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,9}", fullmatch=True), max_size=4)
alnum = st.from_regex(r"[A-Za-z0-9]+", fullmatch=True)


@pytest.fixture
def default_signals(monkeypatch):
    monkeypatch.setattr(ci_manifesto, "_manifesto_paths", lambda: [])


@pytest.fixture
def guard(outside_a_guarded_repository, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_DISABLE_BYPASS_LOOKUP", "1")

    def run(command):
        check_branch_guard({"tool_input": {"command": command}, "session_id": "guard-properties"})

    return run


class TestSignalMatchProperties:
    @property_settings
    @given(phrase=recased_signal(), before=neutral_words, after=neutral_words)
    def test_any_casing_of_a_signal_matches(self, phrase, before, after):
        assert ci_manifesto._signal_match(f"{before} {phrase} {after}", ALL_SIGNALS)

    @property_settings
    @given(signal=st.sampled_from(ALL_SIGNALS), data=st.data())
    def test_a_negated_signal_in_any_casing_never_matches(self, signal, data):
        negation = data.draw(recased(data.draw(st.sampled_from(NEGATIONS))))
        phrase = data.draw(recased(signal))
        assert not ci_manifesto._signal_match(f"{negation} {phrase}", [signal])

    @property_settings
    @given(text=st.text(alphabet="0123456789 .,;:!?-_/()"))
    def test_text_without_letters_never_matches(self, text):
        assert not ci_manifesto._signal_match(text, ALL_SIGNALS)


@pytest.mark.parametrize(
    ("text", "release", "hotfix", "branch", "pr"),
    [
        ("do the release dance", True, False, False, False),
        ("Do The Release Dance now", True, False, False, False),
        ("release it", False, False, False, False),
        ("promote dev to main", False, False, False, False),
        ("please open a PR", False, False, False, True),
        ("do NOT open a pr", False, False, False, False),
        ("never create a PR for this", False, False, False, False),
        ("make a feature branch", False, False, True, False),
        ("New Branch for the fix", False, False, True, False),
        ("don't branch it", False, False, False, False),
        ("prod is down, hotfix", False, True, False, False),
        ("PROD IS BROKEN", False, True, False, False),
    ],
)
def test_signal_oracle(default_signals, text, release, hotfix, branch, pr):
    assert ci_manifesto.contains_release_signal(text) is release
    assert ci_manifesto.contains_hotfix_signal(text) is hotfix
    assert ci_manifesto.contains_branch_signal(text) is branch
    assert ci_manifesto.contains_pr_signal(text) is pr


class TestBranchGuardProperties:
    @property_settings
    @given(remote=ref_names, target=st.sampled_from(["main", "master", "HEAD:main", "HEAD:master"]))
    def test_push_to_a_protected_branch_always_blocks(self, guard, remote, target):
        with pytest.raises(BlockAction):
            guard(f"git push {remote} {target}")

    @property_settings
    @given(gap=st.from_regex(r"[ \t]+", fullmatch=True), target=st.sampled_from(["main", "master"]))
    def test_whitespace_between_words_never_unblocks(self, guard, gap, target):
        with pytest.raises(BlockAction):
            guard(gap.join(["git", "push", "origin", target]))

    @property_settings
    @given(verb=st.sampled_from(["merge", "rebase", "reset --hard"]), name=ref_names)
    def test_history_commands_on_other_branches_pass(self, guard, verb, name):
        guard(f"git {verb} {name}")

    @property_settings
    @given(verb=st.sampled_from(["merge", "rebase", "reset --hard"]), target=st.sampled_from(["main", "master"]))
    def test_history_commands_on_a_protected_branch_block(self, guard, verb, target):
        with pytest.raises(BlockAction):
            guard(f"git {verb} {target}")


@pytest.mark.parametrize(
    ("command", "blocked"),
    [
        ("git push origin main", True),
        ("git push origin HEAD:master", True),
        ("git push --force origin dev", True),
        ("git push -f origin feature", True),
        ("git push --force-with-lease origin feature", True),
        ("git tag v1.2.3", True),
        ("git reset --hard master", True),
        ("git branch -D main", True),
        ("git merge origin/main", False),
        ("git push origin maintenance", False),
        ("git checkout main", False),
        ('git commit -m "push to main later"', False),
    ],
)
def test_branch_guard_oracle(guard, command, blocked):
    if blocked:
        with pytest.raises(BlockAction):
            guard(command)
    else:
        guard(command)


class TestSecretsProperties:
    @property_settings
    @given(
        key=st.sampled_from(["PASSWORD", "SECRET", "API_KEY", "ACCESS_TOKEN"]).flatmap(recased),
        value=st.from_regex(r"[A-Za-z0-9]{8,24}", fullmatch=True),
        sep=st.sampled_from(["=", ": ", " = "]),
    )
    def test_generic_secret_matches_any_key_casing(self, key, value, sep):
        assert "generic_secret" in secrets.scan(f"{key}{sep}{value}", mode="standard")

    @property_settings
    @given(
        key=st.sampled_from(["PASSWORD", "SECRET", "API_KEY"]).flatmap(recased),
        name=st.from_regex(r"[A-Z_][A-Z0-9_]{0,12}", fullmatch=True),
    )
    def test_env_references_are_never_secrets(self, key, name):
        assert secrets.scan(f"{key}=${name}", mode="standard") == []

    @property_settings
    @given(
        prefix=st.sampled_from(["AKIA", "ASIA", "AROA", "AIPA"]),
        body=st.from_regex(r"[A-Z0-9]{16}", fullmatch=True),
        marker=recased("nosecret"),
    )
    def test_aws_key_ids_match_and_nosecret_suppresses_them(self, prefix, body, marker):
        line = f"key {prefix}{body}"
        assert "aws_access_key" in secrets.scan(line, mode="standard")
        assert secrets.scan(f"{line}  # {marker}", mode="standard") == []

    @property_settings
    @given(kind=st.sampled_from(["ghp_", "ghs_"]), body=st.from_regex(r"[A-Za-z0-9]{36,48}", fullmatch=True))
    def test_github_tokens_match_and_redact(self, kind, body):
        token = kind + body
        assert "github_token" in secrets.scan(f"token {token}", mode="standard")
        assert token not in secrets.redact(f"token {token}", mode="standard")


def _oracle_secret_cases():
    aws = "AKIA" + "IOSFODNN7EXAMPLE"
    gh = "ghp_" + "a1" * 18
    return [
        (f"export AWS_ACCESS_KEY_ID={aws}", "standard", {"aws_access_key"}),
        (f"GITHUB_TOKEN={gh}", "standard", {"github_token"}),
        ("pass" + "word = hunter2hunter2", "standard", {"generic_secret"}),
        ("PASS" + "WORD=$DB_PASSWORD", "standard", set()),
        ("api_" + "key = settings.api_key", "standard", set()),
        ("pass" + "word = '10s'", "standard", set()),
        ("xox" + "b-" + "1234567890-abcdef", "standard", set()),
        ("xox" + "b-" + "1234567890-abcdef", "strict", {"slack_token"}),
        (f"key {aws}  # nosecret", "standard", set()),
        (f"key {aws}", "off", set()),
    ]


@pytest.mark.parametrize(("text", "mode", "expected"), _oracle_secret_cases())
def test_secrets_oracle(text, mode, expected):
    assert set(secrets.scan(text, mode=mode)) == expected


SHELL_RC_NAMES = [".bashrc", ".bash_profile", ".profile", ".zshrc", ".zshenv", ".bash_history", ".zsh_history"]
CREDENTIAL_NAMES = [".netrc", ".npmrc", ".pypirc", ".git-credentials", ".pgpass"]


class TestCredentialGuardProperties:
    @property_settings
    @given(dirs=path_segments, base=st.sampled_from(SHELL_RC_NAMES), quote=st.sampled_from(["", "'", '"']))
    def test_shell_rc_under_any_directory_is_sensitive(self, dirs, base, quote):
        assert sensitive_kind(quote + "/".join([*dirs, base]) + quote) == KIND_SHELLRC

    @property_settings
    @given(dirs=path_segments, base=st.sampled_from(CREDENTIAL_NAMES))
    def test_credential_stores_under_any_directory_are_sensitive(self, dirs, base):
        assert sensitive_kind("/".join(["~", *dirs, base])) == KIND_CREDENTIAL

    @property_settings
    @given(
        dirs=path_segments,
        base=st.one_of(
            st.just(".env"),
            alnum.filter(lambda s: s not in TEMPLATE_SUFFIXES).map(lambda s: f".env.{s}"),
            alnum.map(lambda s: f"{s}.env"),
        ),
    )
    def test_dotenvs_under_any_directory_are_sensitive(self, dirs, base):
        assert sensitive_kind("/".join([*dirs, base])) == KIND_DOTENV

    @property_settings
    @given(
        dirs=path_segments,
        base=st.sampled_from([".env", ".env.local", "stack.env", *SHELL_RC_NAMES, *CREDENTIAL_NAMES]),
        suffix=st.sampled_from(TEMPLATE_SUFFIXES),
    )
    def test_template_suffix_is_never_sensitive(self, dirs, base, suffix):
        assert sensitive_kind("/".join([*dirs, f"{base}.{suffix}"])) is None


@pytest.mark.parametrize(
    ("token", "kind"),
    [
        (".env", KIND_DOTENV),
        ("/srv/app/.env.production", KIND_DOTENV),
        ("runtime/tracker.env", KIND_DOTENV),
        ("~/.bashrc", KIND_SHELLRC),
        ("/home/x/.bash_history", KIND_SHELLRC),
        ("~/.netrc", KIND_CREDENTIAL),
        ("~/.git-credentials", KIND_CREDENTIAL),
        ("~/.aws/credentials", KIND_CREDENTIAL),
        ("/proc/self/environ", KIND_ENVIRONMENT),
        (".env.example", None),
        (".env.sample", None),
        (".env.template", None),
        (".env.dist", None),
        ("docs/credentials", None),
        ("environment.py", None),
        ("", None),
    ],
)
def test_credential_kind_oracle(token, kind):
    assert sensitive_kind(token) == kind
