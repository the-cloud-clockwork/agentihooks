import hashlib
import hmac
import io
import json

import pytest

from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.events.memory import open_bus
from scripts.hub import github_webhooks, pulls
from scripts.hub.github_webhooks import Ingress
from scripts.hub.pulls import Observation, Pulls

pytestmark = pytest.mark.unit

SECRET = b"webhook-secret"
REPO = "https://github.com/acme/widgets"
URL = f"{REPO}/pull/7"


def sign(body: bytes, secret: bytes = SECRET) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def pull_request(action="opened", state="open", merged=False, head="a1"):
    return {
        "action": action,
        "pull_request": {"html_url": URL, "state": state, "merged": merged, "head": {"sha": head}},
    }


def check_suite(action="completed", conclusion="success", head="a1", numbers=(7,)):
    return {
        "action": action,
        "repository": {"html_url": REPO},
        "check_suite": {
            "id": 9,
            "head_sha": head,
            "conclusion": conclusion,
            "app": {"slug": "github-actions"},
            "pull_requests": [{"number": number} for number in numbers],
        },
    }


def setup():
    bus = open_bus(BusConfig())
    pulls = Pulls(bus, fetch=lambda url: pytest.fail("unexpected fetch"), clock=lambda: 1000)
    pulls.track("crew", URL)
    return bus, Ingress(pulls, SECRET), bus.events.consumer("watch", "swarm.>")


def deliver(ingress, name, payload, signature=None):
    body = json.dumps(payload).encode()
    headers = {"X-GitHub-Event": name, "X-Hub-Signature-256": sign(body) if signature is None else signature}
    return ingress.receive(headers, body)


def drain(consumer):
    deliveries = consumer.fetch(100, 0)
    for delivery in deliveries:
        delivery.ack()
    return [delivery.event for delivery in deliveries]


def test_verify_accepts_only_the_hmac_sha256_signature_of_the_body():
    body = b'{"zen": "x"}'
    assert github_webhooks.verify(SECRET, body, sign(body)) is True
    assert github_webhooks.verify(SECRET, body + b" ", sign(body)) is False
    assert github_webhooks.verify(SECRET, body, sign(body, b"other")) is False
    assert github_webhooks.verify(SECRET, body, sign(body).removeprefix("sha256=")) is False
    assert github_webhooks.verify(SECRET, body, "sha1=" + sign(body).removeprefix("sha256=")) is False
    assert github_webhooks.verify(SECRET, body, None) is False
    assert github_webhooks.verify(SECRET, body, "") is False
    assert github_webhooks.verify(SECRET, body, "sha256=é") is False
    assert github_webhooks.verify(b"", body, sign(body, b"")) is False


def test_a_signed_pull_request_delivery_becomes_bus_events():
    _, ingress, consumer = setup()
    assert deliver(ingress, "pull_request", pull_request()) == 202
    events = drain(consumer)
    assert [(e.swarm, e.type, e.key) for e in events] == [("crew", "pull.opened", URL)]
    assert isinstance(events[0], Envelope)
    assert deliver(ingress, "pull_request", pull_request("synchronize", head="b2")) == 202
    assert deliver(ingress, "pull_request", pull_request("enqueued", head="b2")) == 202
    assert deliver(ingress, "pull_request", pull_request("closed", "closed", True, "b2")) == 202
    assert [e.type for e in drain(consumer)] == ["pull.pushed", "merge.queued", "pull.merged"]


def test_a_dequeue_and_a_close_without_merge_are_reported():
    _, ingress, consumer = setup()
    deliver(ingress, "pull_request", pull_request("enqueued"))
    deliver(ingress, "pull_request", pull_request("dequeued"))
    deliver(ingress, "pull_request", pull_request("closed", "closed"))
    assert [e.type for e in drain(consumer)] == ["pull.opened", "merge.queued", "merge.dequeued", "pull.closed"]


def test_a_signed_check_suite_completion_becomes_a_checks_event():
    _, ingress, consumer = setup()
    deliver(ingress, "pull_request", pull_request())
    drain(consumer)
    assert deliver(ingress, "check_suite", check_suite(numbers=(7, 8))) == 202
    events = drain(consumer)
    assert [(e.type, e.data["suite"], e.data["app"], e.data["conclusion"]) for e in events] == [
        ("checks.completed", "9", "github-actions", "success")
    ]
    assert deliver(ingress, "check_suite", check_suite("requested")) == 202
    assert drain(consumer) == []


@pytest.mark.parametrize("signature", ["", "sha256=" + "0" * 64, sign(b"other body"), sign(b"{}", b"wrong")])
def test_unsigned_or_wrongly_signed_requests_are_refused_and_publish_nothing(signature):
    bus, ingress, consumer = setup()
    assert deliver(ingress, "pull_request", pull_request(), signature) == 401
    assert drain(consumer) == []
    assert json.loads(bus.state(pulls.BUCKET).get(pulls.key(URL)).value)["version"] == 0


def test_a_request_without_a_signature_header_is_refused():
    _, ingress, consumer = setup()
    body = json.dumps(pull_request()).encode()
    assert ingress.receive({"X-GitHub-Event": "pull_request"}, body) == 401
    assert drain(consumer) == []


@pytest.mark.parametrize(
    "body", [b"not json", b"[]", json.dumps({"action": "opened"}).encode(), json.dumps({"pull_request": 3}).encode()]
)
def test_a_signed_but_malformed_payload_is_a_bad_request(body):
    _, ingress, consumer = setup()
    assert ingress.receive({"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sign(body)}, body) == 400
    assert drain(consumer) == []


def test_other_signed_events_are_accepted_without_publishing():
    _, ingress, consumer = setup()
    assert deliver(ingress, "ping", {"zen": "x"}) == 202
    body = b"{}"
    assert ingress.receive({"X-Hub-Signature-256": sign(body)}, body) == 202
    assert drain(consumer) == []


def test_observations_from_payloads():
    assert github_webhooks.observations("pull_request", pull_request("edited")) == [
        Observation(URL, "open", "a1", None)
    ]
    assert github_webhooks.observations("pull_request", pull_request("enqueued")) == [
        Observation(URL, "open", "a1", True)
    ]
    assert github_webhooks.observations("pull_request", pull_request("dequeued")) == [
        Observation(URL, "open", "a1", False)
    ]
    assert github_webhooks.observations("pull_request", pull_request("closed", "closed", True)) == [
        Observation(URL, "merged", "a1", None)
    ]
    suite = {"app": "github-actions", "conclusion": "failure"}
    assert github_webhooks.observations("check_suite", check_suite(conclusion="failure", numbers=(7, 8))) == [
        Observation(URL, checks_head="a1", suites={"9": suite}),
        Observation(f"{REPO}/pull/8", checks_head="a1", suites={"9": suite}),
    ]
    payload = check_suite()
    payload["check_suite"]["app"] = None
    assert github_webhooks.observations("check_suite", payload)[0].suites == {"9": {"app": "", "conclusion": "success"}}
    assert github_webhooks.observations("check_suite", check_suite(numbers=())) == []
    assert github_webhooks.observations("check_suite", check_suite("rerequested")) == []
    assert github_webhooks.observations("check_run", check_suite()) == []


def test_ingress_refuses_to_run_without_a_secret():
    bus = open_bus(BusConfig())
    with pytest.raises(ValueError, match=r"^AGENTIHOOKS_GITHUB_WEBHOOK_SECRET must be set to verify GitHub webhooks$"):
        Ingress(Pulls(bus), b"")


class Recorder:
    def __init__(self, status=202):
        self.status = status
        self.calls = []

    def receive(self, headers, body):
        self.calls.append((dict(headers), body))
        return self.status


def request(ingress, path="/github", body=b"{}", length=None, cls=None, headers=None):
    cls = cls or github_webhooks.handler(ingress)
    handler = cls.__new__(cls)
    handler.path = path
    handler.headers = headers or {"X-GitHub-Event": "ping"}
    if length is not False:
        handler.headers["Content-Length"] = str(len(body)) if length is None else length
    handler.rfile = io.BytesIO(body)
    sent = []
    handler.send_response = lambda status: sent.append(("status", status))
    handler.send_header = lambda name, value: sent.append((name, value))
    handler.end_headers = lambda: sent.append(("end",))
    handler.do_POST()
    return sent


def test_http_handler_passes_the_body_to_the_ingress_and_answers_its_status():
    ingress = Recorder(401)
    assert request(ingress, body=b'{"a": 1}') == [("status", 401), ("Content-Length", "0"), ("end",)]
    assert ingress.calls == [({"Content-Length": "8", "X-GitHub-Event": "ping"}, b'{"a": 1}')]


def test_http_handler_refuses_other_paths_and_bad_lengths():
    ingress = Recorder()
    limit = 25 * 1024 * 1024
    assert request(ingress, "/other")[0] == ("status", 404)
    assert request(ingress, length="")[0] == ("status", 411)
    assert request(ingress, length=False)[0] == ("status", 411)
    assert request(ingress, length="-1")[0] == ("status", 411)
    assert request(ingress, length="²")[0] == ("status", 411)
    assert request(ingress, length=str(limit + 1))[0] == ("status", 413)
    assert ingress.calls == []
    body = b"x" * 3
    assert request(ingress, body=body, length=str(limit))[0] == ("status", 202)
    assert ingress.calls == [({"Content-Length": str(limit), "X-GitHub-Event": "ping"}, body)]
    assert request(ingress, body=b"abcdef", length="3")[0] == ("status", 202)
    assert ingress.calls[-1][1] == b"abc"


def test_http_handler_times_out_slow_clients():
    assert github_webhooks.handler(Recorder()).timeout == 10


def test_a_bus_failure_answers_503_so_github_redelivers(capsys):
    class Down:
        def observe(self, seen):
            raise ConnectionError("bus down")

    ingress = Ingress(Down(), SECRET)
    assert deliver(ingress, "pull_request", pull_request()) == 503
    err = capsys.readouterr().err
    assert err.startswith("hub: GitHub webhook could not record the event\nTraceback")
    assert err.endswith("ConnectionError: bus down\n")
    assert deliver(ingress, "ping", {}) == 202


def test_http_handler_logs_nothing():
    handler = github_webhooks.handler(Recorder())
    assert handler.log_message(None, "%s", "x") is None


def test_main_refuses_to_start_without_a_secret(monkeypatch, capsys):
    def refuse(config):
        raise AssertionError("opened the bus without a secret")

    monkeypatch.delenv("AGENTIHOOKS_GITHUB_WEBHOOK_SECRET", raising=False)
    monkeypatch.setattr(github_webhooks, "open_bus", refuse)
    assert github_webhooks.main([]) == 2
    assert capsys.readouterr().err == (
        "agentihooks github-webhook: AGENTIHOOKS_GITHUB_WEBHOOK_SECRET must be set to verify GitHub webhooks\n"
    )


def test_main_names_its_command_in_usage(capsys):
    with pytest.raises(SystemExit):
        github_webhooks.main(["--port", "x"])
    assert capsys.readouterr().err.startswith("usage: agentihooks github-webhook [-h]")


def test_main_serves_the_ingress_on_the_given_address(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_GITHUB_WEBHOOK_SECRET", "s")
    served = []

    class Bus:
        def __init__(self, config):
            served.append(("bus", config.backend))
            self.inner = open_bus(BusConfig())
            self.events = self.inner.events
            Pulls(self.inner, clock=lambda: 1000).track("crew", URL)

        def state(self, bucket):
            return self.inner.state(bucket)

        def close(self):
            served.append(("bus closed",))

    class Server:
        def __init__(self, address, handler):
            served.append(("init", address, handler.__name__))
            served.append(("unsigned", request(None, cls=handler)[0]))
            body = json.dumps(pull_request()).encode()
            signed = {"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sign(body, b"s")}
            served.append(("signed", request(None, body=body, cls=handler, headers=signed)[0]))

        def serve_forever(self):
            served.append(("serve",))
            raise KeyboardInterrupt

        def server_close(self):
            served.append(("close",))

    monkeypatch.setenv("AGENTIHOOKS_BUS", "memory")
    monkeypatch.setattr(github_webhooks, "open_bus", Bus)
    monkeypatch.setattr(github_webhooks, "ThreadingHTTPServer", Server)
    with pytest.raises(KeyboardInterrupt):
        github_webhooks.main(["--host", "0.0.0.0", "--port", "9001"])
    assert served == [
        ("bus", "memory"),
        ("init", ("0.0.0.0", 9001), "Webhook"),
        ("unsigned", ("status", 401)),
        ("signed", ("status", 202)),
        ("serve",),
        ("close",),
        ("bus closed",),
    ]
    served.clear()
    with pytest.raises(KeyboardInterrupt):
        github_webhooks.main([])
    assert served[1] == ("init", ("127.0.0.1", 8790), "Webhook")


def test_the_cli_delegates_github_webhook_to_this_module():
    from scripts.cli_delegates import DELEGATED_CLIS

    assert DELEGATED_CLIS["github-webhook"] == "scripts.hub.github_webhooks"


def test_the_hub_registry_runs_the_pulls_reactor_on_the_hub_clock():
    from scripts.hub import registry

    bus = open_bus(BusConfig())
    reactors = {reactor.name: reactor for reactor in registry.configured(bus, lambda: 4242)}
    reactors["pulls"].poll(lambda: True)
    assert json.loads(bus.state("hub-github").get("polled").value) == 4242
