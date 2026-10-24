import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")
OUTCOMES = ("success", "captcha", "failure")


def run(current: Callable[[], T | None], browser: Callable[[], str], token: Callable[[], T]) -> T:
    found = current()
    if found is not None:
        return found
    outcome = browser()
    if outcome not in OUTCOMES:
        raise ValueError("browser outcome must be success, captcha or failure")
    if outcome == "success":
        found = current()
        if found is not None:
            return found
    return token()


def notification(target: Path, step: str, outcome: str, transport: str = "none") -> None:
    path = target.parent / "login-notifications.jsonl"
    record = {"step": step, "outcome": outcome, "transport": transport}
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\n")


def _browser_reply(store, request, recipient: str, sender: str) -> str | None:
    current = store.get(request.id)
    prefix = "done: replied with message "
    if current.state != "done" or not current.reason.startswith(prefix):
        return None
    answer = store.get(current.reason.removeprefix(prefix))
    if answer.ref != request.id or not store.acts_for(answer.sender, recipient):
        return None
    outcome = answer.text.strip()
    if outcome not in OUTCOMES:
        return "failure"
    store.close(answer.id, sender, "done", "browser outcome consumed")
    return outcome


def _request(store, environ, target, output):
    link = re.search(r"https://(?:claude\.ai|claude\.com)/oauth/authorize\?[^\s\x1b]+", output.read_text())
    if link is None:
        return None
    text = (
        "Authorize this native master login using the already signed in claude.ai session in the operator browser connector; "
        "never sign in, sign out or switch accounts. Stop on a login form or CAPTCHA. "
        f"Open {link.group(0)} and reply exactly success, captcha or failure. "
        "Never send authorization codes or credentials."
    )
    recipient = f"master@{environ['AGENTIHOOKS_SWARM']}"
    request = store.send(
        environ["AGENTIHOOKS_AGENT_NAME"], recipient, text, task=environ.get("AGENTIHOOKS_SWARM_TASK", "")
    )
    notification(target, "browser", "requested", "inbox")
    return request


def _wait_for_browser(process, store, environ: Mapping[str, str], target: Path, output: Path, timeout: float) -> str:
    sender = environ["AGENTIHOOKS_AGENT_NAME"]
    recipient = f"master@{environ['AGENTIHOOKS_SWARM']}"
    deadline = time.monotonic() + timeout
    request, reported = None, None
    try:
        while time.monotonic() < deadline:
            if request is None:
                request = _request(store, environ, target, output)
            if request is not None and reported is None:
                reported = _browser_reply(store, request, recipient, sender)
            if reported in ("captcha", "failure"):
                return reported
            code = process.poll()
            if reported == "success" and code == 0:
                return "success"
            if code is not None and (code != 0 or request is None):
                return "failure"
            time.sleep(0.25)
        return "failure"
    finally:
        if request is not None and store.get(request.id).state not in ("done", "cancelled", "blocked"):
            store.close(request.id, sender, "cancel", "browser attempt ended")


def _stop_login(process) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def authorize(environ: Mapping[str, str], target: Path, timeout: float = 300) -> str:
    from scripts import master_claude_login as login
    from scripts.inbox import store as inbox

    outcome = "failure"
    if not environ.get("AGENTIHOOKS_SWARM"):
        notification(target, "browser", outcome, "inbox")
        return outcome
    output = target.parent / "browser-auth.log"
    try:
        store = inbox.connect(environ)
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            process = subprocess.Popen(
                [shutil.which("claude") or "claude", *login.SETTINGS, *login.LOGIN],
                env={**login.child(environ, target), "BROWSER": "true"},
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                outcome = _wait_for_browser(process, store, environ, target, output, timeout)
            finally:
                _stop_login(process)
    except (OSError, inbox.InboxError):
        outcome = "failure"
    notification(target, "browser", outcome, "inbox")
    return outcome
