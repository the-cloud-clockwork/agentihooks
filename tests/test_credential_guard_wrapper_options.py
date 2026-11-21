import signal
from collections.abc import Iterator
from types import FrameType

import pytest

from hooks.context.credential_guard import decide, verb_index

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _bound_wrapper_parsing() -> Iterator[None]:
    def fail_on_loop(signum: int, frame: FrameType | None) -> None:
        raise TimeoutError("wrapper command parsing did not finish")

    previous_handler = signal.signal(signal.SIGALRM, fail_on_loop)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 1)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        signal.signal(signal.SIGALRM, previous_handler)


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
        "sudo -u root bash -e .env ignored",
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
        "sudo -- -u root cat .env",
        "env -- -u UNUSED cat .env",
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


@pytest.mark.feature("credential-guard-wrapper-option-values")
@pytest.mark.parametrize(
    "command",
    [
        "sudo --prompt .env bash -c 'printf message'",
        "sudo -p .env bash -c 'cat README.md'",
        "env --unset .env bash -c 'printf message'",
        "env -u .env python -c 'print(1)'",
    ],
)
def test_wrapper_options_do_not_turn_safe_scripts_into_credential_reads(command: str) -> None:
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    assert decide(payload) is None


@pytest.mark.feature("credential-guard-wrapper-option-values")
@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        (["MODE=readonly"], 0),
        (["MODE=readonly", "cat"], 1),
        (["sudo", "--", "-u", "root", "cat"], 2),
        (["env", "--", "-u", "UNUSED", "cat"], 2),
        (["sudo", "-u"], 2),
        (["env", "--unset"], 2),
    ],
)
def test_wrapper_parser_preserves_command_boundaries(tokens: list[str], expected: int) -> None:
    assert verb_index(tokens) == expected
