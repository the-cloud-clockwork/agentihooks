import hashlib
import hmac

import pytest
import scripts.swarm_v2.operator_auth as operator_auth

from scripts.gates.base import Who
from scripts.swarm_v2.runtime.commands import Principal, Role

SLUG = "rig"
PAGE = "page-credential-0123456789"
CREDENTIALS = {SLUG: PAGE}


def agent_token(admin, slug, name):
    return hmac.new(admin.encode(), f"{slug}\n{name}".encode(), hashlib.sha256).hexdigest()


def _authenticate(slug, credential):
    return operator_auth.authenticator(CREDENTIALS.get)(slug, credential)


def test_the_page_credential_authenticates_as_the_operator():
    assert _authenticate(SLUG, PAGE) == Principal("operator", Role.OPERATOR)


@pytest.mark.parametrize(
    ("slug", "credential"),
    [
        (SLUG, agent_token(PAGE, SLUG, "engineer@rig-1")),
        (SLUG, agent_token(PAGE, SLUG, "master@rig-1")),
        (SLUG, ""),
        (SLUG, PAGE + "x"),
        (SLUG, PAGE[:-1]),
        (SLUG, None),
        (SLUG, "pagé"),
        ("other", PAGE),
        ("other", ""),
    ],
)
def test_anything_but_the_page_credential_of_that_ledger_is_refused(slug, credential):
    assert _authenticate(slug, credential) is None


@pytest.mark.parametrize("stored", [None, "", 7])
def test_a_ledger_without_a_page_credential_authenticates_no_one(stored):
    authenticate = operator_auth.authenticator(lambda slug: stored)
    assert authenticate(SLUG, "") is None
    assert authenticate(SLUG, "7") is None


def test_the_operator_presents_the_page_credential():
    who = Who(name="", swarm="")
    assert operator_auth.credential(SLUG, CREDENTIALS.get, who) == PAGE


@pytest.mark.parametrize(
    "who",
    [
        Who(name="engineer@rig-1", swarm=SLUG),
        Who(name="master@rig-1", swarm="other"),
        Who(name="engineer@rig-1"),
        Who(swarm=SLUG),
    ],
)
def test_any_agent_identity_presents_no_credential(who):
    assert operator_auth.credential(SLUG, CREDENTIALS.get, who) == ""


def test_a_ledger_without_a_page_credential_gives_the_operator_none():
    assert operator_auth.credential("other", CREDENTIALS.get, Who()) == ""
