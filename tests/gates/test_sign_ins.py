import io
import json

import pytest

from scripts.gates import Call, Gate, Who, entry, modes
from scripts.gates.sign_ins import ELEMENT, SignIns, refusal, sign_in

ME = Who(name="engineer@1-1", swarm="demo", lane="eng", task="t1")
EXT = "mcp__playwright-ext-account__"
CMD = "mcp__playwright-cmd__"


def rows(tmp_path):
    path = tmp_path / "demo" / "gates" / "log.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_it_is_a_gate_that_ships_log_only():
    gate = SignIns()
    assert isinstance(gate, Gate)
    assert (gate.name, gate.default_mode) == ("sign-ins", "observe")
    assert isinstance(entry.HOST_GATES["sign-ins"], SignIns)


def test_it_matches_playwright_tools_only():
    gate = SignIns()
    tools = (
        EXT + "browser_click",
        CMD + "browser_navigate",
        "mcp__plugin_playwright_playwright__browser_navigate",
        "mcp__serena__playwright",
        "mcp__serena__playwright__browser_click",
        "playwright__browser_click",
        "Bash",
    )
    assert [gate.matches(Call(tool)) for tool in tools] == [True, True, True, False, False, False, False]


def test_the_refusal_names_what_it_saw_and_the_way_out():
    assert refusal("a sign in page") == (
        "sign in: this browser call opens or fills a sign in page. Agents never sign in, solve a CAPTCHA or grant "
        "OAuth; use a page already signed in, or raise the step for the operator in Priorities."
    )


def test_form_fields_are_read_by_name_and_by_type_and_non_fields_are_skipped():
    by_name = {"fields": ["x", {"name": "Password", "type": "textbox"}]}
    by_type = {"fields": [{"name": "pw", "type": "password"}]}
    plain = {"fields": [{"name": "Email", "type": "textbox"}]}
    assert [sign_in(EXT + "browser_fill_form", form) for form in (by_name, by_type, plain)] == [ELEMENT, ELEMENT, ""]


def test_sign_in_pages_are_named():
    assert sign_in(CMD + "browser_navigate", {"url": "https://accounts.google.com/v3/signin/identifier"}) == (
        "a sign in page"
    )
    assert (
        sign_in(EXT + "browser_navigate", {"url": "https://github.com/login/oauth/authorize?x=1"}) == "a sign in page"
    )
    assert sign_in(CMD + "browser_navigate", {"url": "http://127.0.0.1:8765/rig-grade-swarm"}) == ""


def test_password_sign_in_and_captcha_elements_are_named():
    assert sign_in(EXT + "browser_type", {"element": "Password field", "ref": "e3", "text": "x"}) == (
        "a password, sign in or CAPTCHA element"
    )
    assert sign_in(EXT + "browser_click", {"element": "Sign in button", "ref": "e9"}) == ELEMENT
    assert sign_in(EXT + "browser_click", {"element": "I'm not a robot checkbox", "ref": "e1"}) == ELEMENT
    form = {
        "fields": [{"name": "Email", "type": "textbox", "ref": "e1", "value": "a"}, {"name": "pw", "type": "password"}]
    }
    assert sign_in(EXT + "browser_fill_form", form) == ELEMENT
    assert sign_in(EXT + "browser_click", {"element": "Approve plan button", "ref": "e2"}) == ""
    assert sign_in(EXT + "browser_click", {"element": "Design in progress tab", "ref": "e5"}) == ""
    assert sign_in(EXT + "browser_click", {"element": "Catalog in sidebar", "ref": "e6"}) == ""
    assert sign_in(EXT + "browser_take_screenshot", {"element": "Sign in panel"}) == ""


def test_a_sign_in_is_a_would_be_deny():
    call = Call(EXT + "browser_click", {"element": "Log in", "ref": "e4"})
    decision = SignIns().decide(call, ME, None)
    assert (decision.allowed, decision.reason) == (False, refusal("a password, sign in or CAPTCHA element"))


@pytest.mark.parametrize(
    "tool,tool_input,what",
    [
        (CMD + "browser_navigate", {"url": "https://example.com/signin"}, "a sign in page"),
        (EXT + "browser_type", {"element": "Password", "ref": "e2", "text": "x"}, ELEMENT),
    ],
)
def test_a_sign_in_is_logged_and_let_through_in_log_only_mode(tmp_path, monkeypatch, tool, tool_input, what):
    monkeypatch.setattr(modes, "swarm_gates", lambda swarm, environ: {})
    environ = {"AGENTIHOOKS_SWARM": "demo", "AGENTIHOOKS_AGENT_NAME": ME.name, "AGENTIHOOKS_SWARM_TASK": "t1"}
    payload = json.dumps({"tool_name": tool, "tool_input": tool_input})
    assert entry.main(["sign-ins"], io.StringIO(payload), environ, tmp_path) == 0
    assert [(r["gate"], r["kind"], r["reason"]) for r in rows(tmp_path)] == [("sign-ins", "observe", refusal(what))]
