import jwt
import pytest

from scripts.auth import tokens

KEY = "a-signing-key-of-at-least-32-bytes"
OTHER_KEY = "another-signing-key-of-32-bytes!!"
SLUG = "auth-proof"
AGENT = "engineer@323133-0256"


def test_a_signed_token_names_its_principal_on_its_own_ledger_only():
    token = tokens.sign(KEY, AGENT, SLUG)

    assert tokens.verify(KEY, token, SLUG) == AGENT
    assert tokens.verify(KEY, token, "another-ledger") is None
    assert tokens.verify(OTHER_KEY, token, SLUG) is None
    assert tokens.verify(KEY, token + "x", SLUG) is None


def test_the_operator_principal_is_the_empty_name():
    assert tokens.verify(KEY, tokens.sign(KEY, "", SLUG), SLUG) == ""


def test_a_token_lives_fifteen_minutes():
    claims = jwt.decode(
        tokens.sign(KEY, AGENT, SLUG, now=1000.0), KEY, algorithms=["HS256"], options={"verify_exp": False}
    )

    assert tokens.TTL_S == 900
    assert claims == {"sub": AGENT, "slug": SLUG, "iat": 1000, "exp": 1900}


def test_an_expired_token_is_refused():
    token = tokens.sign(KEY, AGENT, SLUG, now=1000.0)

    assert tokens.verify(KEY, token, SLUG) is None


@pytest.mark.parametrize("token", ["", "not-a-token", "a.b.c"])
def test_a_malformed_token_is_refused(token):
    assert tokens.verify(KEY, token, SLUG) is None


def test_a_token_without_a_ledger_claim_is_refused():
    token = jwt.encode({"sub": AGENT, "exp": 4102444800}, KEY, algorithm="HS256")

    assert tokens.verify(KEY, token, SLUG) is None


def test_an_unsigned_token_is_refused():
    token = jwt.encode({"sub": AGENT, "slug": SLUG, "exp": 4102444800}, None, algorithm="none")

    assert tokens.verify(KEY, token, SLUG) is None


def test_no_key_verifies_nothing():
    assert tokens.verify("", tokens.sign(KEY, AGENT, SLUG), SLUG) is None


def test_an_empty_token_is_refused():
    assert tokens.verify(KEY, "", SLUG) is None


def test_a_token_is_signed_with_hs256():
    assert jwt.get_unverified_header(tokens.sign(KEY, AGENT, SLUG))["alg"] == "HS256"


def test_the_key_and_the_legacy_switch_come_from_the_environment():
    assert tokens.key({"AGENTIHOOKS_AUTH_KEY": KEY}) == KEY
    assert tokens.key({}) == ""
    assert tokens.legacy({}) is True
    assert tokens.legacy({"AGENTIHOOKS_AUTH_LEGACY": "on"}) is True
    assert tokens.legacy({"AGENTIHOOKS_AUTH_LEGACY": "off"}) is False
