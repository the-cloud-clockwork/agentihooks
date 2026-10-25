import os

from scripts import master_claude_login as login
from scripts import master_login_chain as chain
from scripts.claude_quota_balancer import select_credential


def _take(name, bao):
    for number in order:
        with _locked(root(number)) as folder:
            # ruleid: feature-shared-preferred-master-login
            epoch = _claim(folder, name, os.getpid(), _alive, int(offered[number].get("epoch", 0)), alone=True)[0]
        return number, epoch, offered[number]


def take_positional(name, folder):
    # ruleid: feature-shared-preferred-master-login
    return _claim(folder, name, os.getpid(), _alive, 0, True)[0]


def launch_master(name, pid):
    # ruleid: feature-shared-preferred-master-login
    return login.claim(name, pid, alone=True)


def resume_master(name, pid):
    # ruleid: feature-shared-preferred-master-login
    return login.claim(name, pid, _alive, 0, True)


def shared_take(name, bao):
    for number in order:
        with _locked(root(number)) as folder:
            # ok: feature-shared-preferred-master-login
            epoch = _claim(folder, name, os.getpid(), _alive, int(offered[number].get("epoch", 0)))[0]
        return number, epoch, offered[number]


def shared_master(name, pid):
    # ok: feature-shared-preferred-master-login
    return login.claim(name, pid)


def master(openbao, environ, deps, slot=None):
    # ok: feature-shared-preferred-master-login
    epoch = login.claim(PROG, os.getpid(), alone=True)
    return epoch


def browser_home(environ):
    for number in numbers():
        with _locked(root(number)) as folder:
            if _private(home(number)) or holders(folder):
                continue
            # ok: feature-shared-preferred-master-login
            _claim(folder, environ["AGENTIHOOKS_AGENT_NAME"], os.getpid(), _alive, 0, True)
            return _rebuild(folder, environ["CLAUDE_CONFIG_DIR"])


def browser_home_existing(environ):
    # ruleid: feature-shared-preferred-master-login
    _claim(folder, environ["AGENTIHOOKS_AGENT_NAME"], os.getpid(), _alive, 0, True)
    return folder


def launch_claude(binary, args, environ, report):
    # ruleid: feature-shared-preferred-master-login
    return launch_token(binary, args, environ, report)


def launch_master_token(binary, args, environ, report):
    # ruleid: feature-shared-preferred-master-login
    return login.launch_token(binary, args, environ, report)


def master_route(environ):
    # ruleid: feature-shared-preferred-master-login
    return select_credential(environ)


def master_before_login(binary, args, environ, report):
    # ruleid: feature-shared-preferred-master-login
    login.launch_token(binary, args, environ, report)
    prepared = login.prepare_launch(environ, cwd)
    if prepared is None:
        # ok: feature-shared-preferred-master-login
        return login.launch_token(binary, args, environ, report)
    return prepared


def master_between_login_and_guard(binary, args, environ, report):
    prepared = login.prepare_launch(environ, cwd)
    # ruleid: feature-shared-preferred-master-login
    login.launch_token(binary, args, environ, report)
    if prepared is None:
        # ok: feature-shared-preferred-master-login
        return login.launch_token(binary, args, environ, report)
    return prepared


def master_after_login(binary, args, environ, report):
    prepared = login.prepare_launch(environ, cwd)
    if prepared is None:
        # ok: feature-shared-preferred-master-login
        return login.launch_token(binary, args, environ, report)
    return prepared


def launch_claude(binary, args, environ, report):
    try:
        prepared = prepare_launch(environ, cwd)
    except LoginRefused:
        raise
    if prepared is None:
        # ok: feature-shared-preferred-master-login
        return launch_token(binary, args, environ, report)
    return prepared


# ruleid: feature-shared-preferred-master-login
def _route_kind(self, lane, harness, task, slug):
    explicit = task.get("route_kind")
    if explicit:
        return explicit
    return "subscription"


# ruleid: feature-shared-preferred-master-login
def _route_kind(self, lane, harness, task, slug):
    if task.get("route_kind"):
        return "subscription"
    if login.enabled(slug, task.get("seat", ""), lane, harness):
        return "interactive"
    return "subscription"


# ok: feature-shared-preferred-master-login
def _route_kind(self, lane, harness, task, slug):
    if login.enabled(slug, task.get("seat", ""), lane, harness or "claude"):
        return "interactive"
    explicit = task.get("route_kind")
    return explicit or "subscription"


def prepare_launch(environ, cwd):
    # ruleid: feature-shared-preferred-master-login
    return chain.run(token, browser, current)


def prepare_launch(environ, cwd):
    # ok: feature-shared-preferred-master-login
    return chain.run(current, browser, token)
