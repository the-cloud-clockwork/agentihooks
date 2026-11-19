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
