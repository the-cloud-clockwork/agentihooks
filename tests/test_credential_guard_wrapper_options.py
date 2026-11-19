import pytest

from hooks.context.credential_guard import decide

pytestmark = pytest.mark.unit


@pytest.mark.feature("credential-guard-wrapper-option-values")
@pytest.mark.parametrize(
    "command",
    [
        "sudo -u root cat .env",
        "sudo --user root cat .env",
        "sudo -g staff cat .env",
        "sudo -n -u root cat .env",
        "sudo -u root -g staff bash -c 'cat .env'",
        "env -u UNUSED cat .env",
        "env --unset UNUSED cat .env",
        "env -C /srv cat .env",
        "env --chdir /srv bash -c 'cat .env'",
        "sudo -u root env -u UNUSED cat .env",
    ],
)
def test_a_credential_read_after_wrapper_option_values_is_refused(command: str) -> None:
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    assert decide(payload) is not None


@pytest.mark.feature("credential-guard-wrapper-option-values")
@pytest.mark.parametrize(
    "prefix",
    [
        "sudo -u root",
        "sudo --user root",
        "sudo -g staff",
        "sudo --group staff",
        "sudo -h host",
        "sudo --host host",
        "sudo -p prompt",
        "sudo --prompt prompt",
        "sudo -C 3",
        "sudo --close-from 3",
        "sudo -D /srv",
        "sudo --chdir /srv",
        "sudo -R /srv",
        "sudo --chroot /srv",
        "sudo -T 10",
        "sudo --command-timeout 10",
        "sudo -r role",
        "sudo --role role",
        "sudo -t type",
        "sudo --type type",
        "sudo -U root",
        "sudo --other-user root",
        "env -u UNUSED",
        "env --unset UNUSED",
        "env -C /srv",
        "env --chdir /srv",
        "sudo -uroot",
        "sudo --user=root",
        "sudo -Eu root",
        "sudo -Euroot",
        "env -uUNUSED",
        "env --unset=UNUSED",
        "env -iu UNUSED",
        "sudo -u root --",
        "env -u UNUSED --",
        "sudo -u root env -u UNUSED",
        "env -u UNUSED sudo -u root",
        "MODE=readonly sudo -u root",
        "sudo -u root MODE=readonly",
        "command sudo -u root",
        "sudo -u root command env -u UNUSED",
    ],
)
def test_wrapper_value_flags_preserve_the_credential_verdict(prefix: str) -> None:
    payload = {"tool_name": "Bash", "tool_input": {"command": f"{prefix} cat .env"}}
    assert decide(payload) is not None


@pytest.mark.feature("credential-guard-wrapper-option-values")
@pytest.mark.parametrize(
    "command",
    [
        "sudo -u root cat README.md",
        "sudo -u root cat .env.example",
        "env -u UNUSED bash -c 'cat README.md'",
        "sudo -u cat printf .env",
        "sudo --user cat printf .env",
        "sudo --prompt .env printf message",
        "env -u cat printf .env",
        "env --unset cat printf .env",
        "sudo -ucat printf .env",
        "sudo --user=cat printf .env",
        "sudo -Eucat printf .env",
        "env -ucat printf .env",
        "env --unset=cat printf .env",
        "sudo -gcat printf .env",
        "sudo -u",
        "env --unset",
        "sudo --",
        "sudo -n --",
        "sudo --user",
        "env -iu",
        "printf sudo -u root cat .env",
        "printf env -u UNUSED cat .env",
        "command -v cat .env",
    ],
)
def test_wrapper_option_values_do_not_become_commands_or_operands(command: str) -> None:
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    assert decide(payload) is None
