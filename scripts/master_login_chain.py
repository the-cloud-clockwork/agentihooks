import json
import os
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")
OUTCOMES = ("success", "captcha", "failure")


@dataclass(frozen=True)
class LoginBudget:
    browser: float = 300
    native_check: float = 30
    token: float = 60
    canary: float = 120
    start: float = 30
    grace: float = 30

    @property
    def route(self) -> float:
        return self.browser + 2 * self.native_check + self.token + self.grace

    @property
    def launch(self) -> float:
        return self.route + self.start + self.canary + self.grace

    @property
    def spawn(self) -> float:
        return self.launch + self.grace

    @property
    def order_ttl_ms(self) -> int:
        return int((self.spawn + self.grace) * 1000)


BUDGET = LoginBudget()


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


def _close(store, sender, item, kind):
    from scripts.inbox import outbox

    detail = "browser attempt ended" if kind == "cancel" else "browser outcome consumed"
    entry = outbox.record("close", sender, {"kind": kind, "detail": detail}, item=item)

    def apply(store, record):
        store.close(record["item"], record["by"], record["args"]["kind"], record["args"]["detail"])
        return {}

    outbox.submit(store, entry, apply)


def _browser_reply(store, request, recipient: str, sender: str) -> str | None:
    current = store.get(request.id)
    prefix = "done: replied with message "
    if current.state != "done" or not current.reason.startswith(prefix):
        return None
    answer = store.get(current.reason.removeprefix(prefix))
    if answer.ref != request.id or not store.acts_for(answer.sender, recipient):
        return None
    outcome = answer.text.strip()
    _close(store, sender, answer.id, "done")
    return outcome if outcome in OUTCOMES else "failure"


def _request(store, environ, target, output):
    from redis.exceptions import RedisError

    from scripts.inbox.store import new_item_id
    from scripts.master_login_native import authorization_url

    link = authorization_url(output.read_text())
    if link is None:
        return None
    text = (
        "Authorize this native master login using the already signed in claude.ai session in the operator browser connector; "
        "never sign in, sign out or switch accounts. Stop on a login form or CAPTCHA. "
        f"Open {link} and reply exactly success, captcha or failure. "
        "If shown a manual authorization code, click the page Copy button immediately before replying success; "
        "the matching native login will privately consume and clear the Windows clipboard. Do not snapshot the code. "
        "Never send authorization codes or credentials."
    )
    sender = environ["AGENTIHOOKS_AGENT_NAME"]
    recipient = f"master@{environ['AGENTIHOOKS_SWARM']}"
    item = new_item_id()
    try:
        request = store.send(sender, recipient, text, task=environ.get("AGENTIHOOKS_SWARM_TASK", ""), item_id=item)
    except RedisError:
        _close(store, sender, item, "cancel")
        raise
    notification(target, "browser", "requested", "inbox")
    return request


def _wait_for_browser(
    process, store, environ: Mapping[str, str], target: Path, output, timeout: float, complete=lambda: True
) -> str:
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
            if reported == "success" and code is None and not complete():
                return "failure"
            if reported == "success" and code == 0:
                return "success"
            if code is not None and (code != 0 or request is None):
                return "failure"
            time.sleep(0.25)
        return "failure"
    finally:
        if request is not None:
            _finish_request(store, sender, request.id)


def _finish_request(store, sender, item):
    from redis.exceptions import RedisError

    try:
        pending = store.get(item).state not in ("done", "cancelled", "blocked")
    except RedisError:
        pending = True
    if pending:
        _close(store, sender, item, "cancel")


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


def authorize(environ: Mapping[str, str], target: Path, timeout: float = BUDGET.browser) -> str:
    from redis.exceptions import RedisError

    from scripts import master_login_native as native
    from scripts.inbox import store as inbox

    outcome = "failure"
    if not environ.get("AGENTIHOOKS_SWARM"):
        notification(target, "browser", outcome, "inbox")
        return outcome
    try:
        store = inbox.connect(environ)
        with native.start(environ, target, _stop_login) as attempt:
            outcome = _wait_for_browser(
                attempt.process, store, environ, target, attempt.output, timeout, attempt.complete
            )
    except (OSError, inbox.InboxError, RedisError):
        outcome = "failure"
    notification(target, "browser", outcome, "inbox")
    return outcome
