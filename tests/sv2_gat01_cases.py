import hashlib
import json
import sys
import tempfile
import traceback
from pathlib import Path

from hooks.context import brain_routes
from scripts.swarm import brain_binding

FIXTURE = Path(__file__).parent / "fixtures/swarm_v2/brain-routes.json"
EVIDENCE_CLASS = (
    "mocked: in-process environments and operator owned files in a temporary home stand in for a launched session, "
    "its hooks and the operator's routes and swarm binding files; no brain server, network call or Pod runs"
)
SESSION = "fixture-session"


class Home:
    def __init__(self, inputs, root):
        self.inputs, self.root = inputs, root
        self.agentihooks = root / ".agentihooks"
        self.write_routes(inputs["routes"])

    def write_routes(self, doc, mode=0o600):
        found = self.agentihooks / "brain-routes.json"
        found.parent.mkdir(parents=True, exist_ok=True)
        found.write_text(json.dumps(doc))
        found.chmod(mode)
        return found

    def environ(self, **extra):
        return {
            "HOME": str(self.root),
            "AGENTIHOOKS_HOME": str(self.agentihooks),
            **self.inputs["credentials"],
            **extra,
        }

    def swarm(self, **extra):
        found = self.agentihooks / "swarm" / "brain-binding.json"
        found.parent.mkdir(parents=True, exist_ok=True)
        found.write_text(json.dumps(self.inputs["swarm_binding"]))
        found.chmod(0o600)
        environ = self.environ(**extra)
        environ[brain_binding.VARIABLE] = json.dumps(brain_binding.decide(environ))
        return environ

    def snapshot(self):
        return {
            str(item.relative_to(self.root)): hashlib.sha256(item.read_bytes()).hexdigest()
            for item in sorted(self.root.rglob("*"))
            if item.is_file() and brain_routes.FAILURES not in item.name
        }


def _session(environ):
    receipt = brain_routes.apply(environ)
    return {
        "receipt": {key: receipt[key] for key in ("status", "route", "origin", "generation")},
        "brain_url": environ.get("BRAIN_URL"),
        "credential_from": next(
            (k for k, v in environ.items() if k != "BRAIN_HTTP_TOKEN" and v == environ.get("BRAIN_HTTP_TOKEN")), None
        ),
    }


def _sessions(inputs, root):
    home = Home(inputs, root)
    personal = home.environ()
    observed = {
        "swarm": _session(home.swarm(**{brain_routes.VARIABLE: inputs["explicit_route"]})),
        "personal": _session(personal),
        "explicit": _session(home.environ(**{brain_routes.VARIABLE: inputs["explicit_route"]})),
    }
    return home, observed, brain_routes.remember(personal, SESSION), json.loads(personal[brain_routes.RECEIPT])


def positive(inputs, root):
    home, first, remembered, receipt = _sessions(inputs, root / "first")
    _, second, _, _ = _sessions(inputs, root / "second")
    assert first == second
    assert first["swarm"]["receipt"]["route"] == brain_routes.SWARM
    assert first["swarm"]["receipt"]["origin"] == inputs["swarm_binding"]["origins"]["lan"]["api"]
    assert first["personal"]["brain_url"] == inputs["routes"]["routes"]["personal"]["api"]
    assert first["personal"]["credential_from"] == "BRAIN_PERSONAL_TOKEN"
    assert first["explicit"]["receipt"]["route"] == inputs["explicit_route"]
    assert first["explicit"]["credential_from"] == "BRAIN_RESEARCH_TOKEN"
    assert remembered == brain_routes.FRESH
    stored = json.loads((brain_routes.store(home.environ()) / "sessions" / f"{SESSION}.json").read_text())
    failures = brain_routes.failures(home.environ())
    assert stored == receipt and failures == 0
    return {
        "sessions": first,
        "second_fixture_identical": True,
        brain_routes.FAILURES: {"session": SESSION, "value": failures},
    }


def _refusal(environ):
    receipt = brain_routes.apply(environ)
    return receipt, {key: environ.get(key) for key in (*brain_routes.SESSION_KEYS, "BRAIN_ENABLED")}


def rejection(inputs, root):
    home = Home(inputs, root)
    before = home.snapshot()
    receipt, left = _refusal(home.environ(**inputs["repository_override"]))
    assert receipt == {"status": brain_routes.REFUSED, "reason": brain_routes.UNAUTHORIZED_URL}
    assert left == {"BRAIN_URL": None, "BRAIN_HTTP_TOKEN": None, "KB_ROUTER_TOKEN": None, "BRAIN_ENABLED": "false"}
    assert home.snapshot() == before
    secrets = [*inputs["credentials"].values(), *inputs["repository_override"].values()]
    assert not any(secret in json.dumps(receipt) for secret in secrets)
    unknown, _ = _refusal(home.environ(**{brain_routes.VARIABLE: "attacker"}))
    home.write_routes(inputs["routes"], mode=0o666)
    shared, _ = _refusal(home.environ())
    home.write_routes(inputs["routes"])
    corrected = brain_routes.apply(home.environ())
    assert corrected["route"] == "personal"
    failures = brain_routes.failures(home.environ())
    assert failures == 3
    return {
        "repository_override": {"receipt": receipt, "session_brain_variables": left},
        "unknown_route": unknown,
        "group_writable_routes_file": shared,
        "corrected_request": {key: corrected[key] for key in ("status", "route", "origin")},
        "protected_state_unchanged": True,
        brain_routes.FAILURES: {"session": SESSION, "value": failures},
    }


def recovery(inputs, root):
    home = Home(inputs, root)
    first = home.environ()
    brain_routes.apply(first)
    old = json.loads(first[brain_routes.RECEIPT])
    states = [brain_routes.remember(first, SESSION)]
    cached = brain_routes.cache(first, SESSION, old)
    cached.mkdir(parents=True)
    (cached / "context.md").write_text("old route context")
    states.append(brain_routes.remember(first, SESSION))
    home.write_routes(inputs["changed_routes"])
    restarted = home.environ()
    brain_routes.apply(restarted)
    new = json.loads(restarted[brain_routes.RECEIPT])
    states.append(brain_routes.remember(restarted, SESSION))
    states.append(brain_routes.remember(first, SESSION))
    stored = json.loads((brain_routes.store(first) / "sessions" / f"{SESSION}.json").read_text())
    assert states == [brain_routes.FRESH, brain_routes.SAME, brain_routes.INVALIDATED, brain_routes.STALE]
    assert new["revision"] != old["revision"] and stored == new
    assert not cached.exists()
    return {
        "states": states,
        "old_origin": old["origin"],
        "new_origin": new["origin"],
        "old_cache_removed": True,
        "stored_generation": stored["generation"],
        brain_routes.FAILURES: {"session": SESSION, "value": brain_routes.failures(first)},
    }


CASES = {"a": positive, "b": rejection, "c": recovery}


def run_case(case):
    inputs = json.loads(FIXTURE.read_text(encoding="utf-8"))
    record = {
        "case": f"T-SV2-GAT-01-{case.upper()}",
        "evidence_class": EVIDENCE_CLASS,
        "input_sha256": {FIXTURE.name: hashlib.sha256(FIXTURE.read_bytes()).hexdigest()},
    }
    with tempfile.TemporaryDirectory() as folder:
        try:
            observed = CASES[case](inputs, Path(folder))
        except AssertionError:
            failed = traceback.extract_tb(sys.exc_info()[2])[-1]
            reproduce = {"fixture": f"tests/fixtures/swarm_v2/{FIXTURE.name}", "case": case, "line": failed.lineno}
            return {**record, "state": "failed", "reproduce": {**reproduce, "assertion": failed.line}}
    return {**record, "state": "passed", "observed": observed}
