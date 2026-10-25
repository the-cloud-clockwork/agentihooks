"""The sign in gate: an agent never signs in, solves a CAPTCHA or opens an OAuth grant in a browser. It ships log only."""

import re

from scripts.gates.base import Decision

NAME = "sign-ins"
PLAYWRIGHT = "mcp__playwright"
PAGE = "a sign in page"
ELEMENT = "a password, sign in or CAPTCHA element"
SIGN_IN_URL = re.compile(
    r"accounts\.google\.com|login\.microsoftonline|/(?:log-?in|sign-?in|sign_in|oauth2?|authorize|sso)\b", re.I
)
SIGN_IN_ELEMENT = re.compile(
    r"password|passcode|sign[ -]?in|log[ -]?in|captcha|not a robot|one[ -]time code|verification code", re.I
)
INTERACTIONS = frozenset(
    {"browser_type", "browser_click", "browser_fill_form", "browser_press_key", "browser_select_option"}
)


def refusal(what):
    return (
        f"sign in: this browser call opens or fills {what}. Agents never sign in, solve a CAPTCHA or grant OAuth; "
        "use a page already signed in, or raise the step for the operator in Priorities."
    )


def _texts(tool_input):
    yield str(tool_input.get("element") or "")
    for field in tool_input.get("fields") or []:
        if isinstance(field, dict):
            yield f"{field.get('name') or ''} {field.get('type') or ''}"


def sign_in(tool, tool_input):
    action = tool.rpartition("__")[2]
    if action == "browser_navigate":
        return PAGE if SIGN_IN_URL.search(str(tool_input.get("url") or "")) else ""
    if action in INTERACTIONS and any(SIGN_IN_ELEMENT.search(text) for text in _texts(tool_input)):
        return ELEMENT
    return ""


class SignIns:
    name = NAME
    default_mode = "observe"

    def matches(self, call):
        return call.tool.lower().startswith(PLAYWRIGHT)

    def decide(self, call, who, state):
        found = sign_in(call.tool, call.tool_input)
        return Decision.deny(refusal(found)) if found else Decision()
