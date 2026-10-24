import pytest

from scripts import master_login_chain as chain


@pytest.mark.parametrize(
    ("available", "outcome", "expected", "order"),
    [
        (True, "failure", "session", ["current"]),
        (False, "success", "session", ["current", "browser", "current"]),
        (False, "captcha", "token", ["current", "browser", "token"]),
        (False, "failure", "token", ["current", "browser", "token"]),
    ],
)
def test_login_chain_uses_session_then_browser_then_token(available, outcome, expected, order):
    observed = []

    def current():
        observed.append("current")
        return "session" if available or observed.count("current") == 2 else None

    def browser():
        observed.append("browser")
        return outcome

    def token():
        observed.append("token")
        return "token"

    assert chain.run(current, browser, token) == expected
    assert observed == order


def test_browser_success_requires_a_live_session_before_use():
    observed = []

    def current():
        observed.append("current")
        return None

    def browser():
        observed.append("browser")
        return "success"

    def token():
        observed.append("token")
        return "token"

    assert chain.run(current, browser, token) == "token"
    assert observed == ["current", "browser", "current", "token"]


def test_pending_browser_never_falls_through_to_a_token():
    def token():
        pytest.fail("an unresolved browser request must wait")

    with pytest.raises(ValueError, match="browser outcome"):
        chain.run(lambda: None, lambda: "pending", token)
