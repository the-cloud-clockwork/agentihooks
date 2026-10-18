import pytest

from scripts import account_names

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("email", "key", "env"),
    [
        ("nestor.colt@gmail.com", "nestor-coltatgmail-com", "AH_CC_TOKEN_nestor_coltatgmail_com"),
        ("NESTOR.COLT@GMAIL.COM", "nestor-coltatgmail-com", "AH_CC_TOKEN_nestor_coltatgmail_com"),
        ("A+b_c@x-y.io", "a-b-catx-y-io", "AH_CC_TOKEN_a_b_catx_y_io"),
        ("ops2026@tcc.dev", "ops2026attcc-dev", "AH_CC_TOKEN_ops2026attcc_dev"),
    ],
)
def test_an_email_names_its_secret_key_and_its_token_variable(email, key, env):
    assert account_names.secret_key(email) == key
    assert account_names.token_env(email) == env


def test_every_symbol_becomes_its_own_dash():
    assert account_names.secret_key("a..b@c") == "a--batc"


def test_the_token_variable_keeps_the_balancer_prefix():
    assert account_names.TOKEN_PREFIX == "AH_CC_TOKEN_"


@pytest.mark.parametrize(
    "environ",
    [
        {"AH_CC_TOKEN_aatb": "value-one", "PATH": "/bin"},
        {"AH_CC_TOKEN_aatb": "value-one", "AH_CX_TOKEN_aatb": "value-two"},
        {"AH_CC_TOKEN_a": "", "AH_CC_TOKEN_b": "value-one"},
    ],
)
def test_the_one_mounted_token_becomes_the_claude_oauth_token(environ):
    assert account_names.oauth_env(environ) == {"CLAUDE_CODE_OAUTH_TOKEN": "value-one"}


@pytest.mark.parametrize(
    "environ",
    [{}, {"PATH": "/bin"}, {"AH_CC_TOKEN_a": "value-one", "AH_CC_TOKEN_b": "value-two"}, {"AH_CC_TOKEN_a": ""}],
)
def test_no_token_several_tokens_or_an_empty_token_export_nothing(environ):
    assert account_names.oauth_env(environ) == {}
