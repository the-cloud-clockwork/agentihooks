import hashlib
import hmac
import io
import json
import signal
from typing import Any, cast

import pytest

from scripts.ci_lab.webhooks import LabIntake
from scripts.events.bus import Bus, StateStore
from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.events.memory import open_bus
from scripts.events.subjects import subject
from scripts.hub import github_webhooks, pulls
from scripts.hub.github_webhooks import Ingress
from scripts.hub.pulls import Observation, Pulls

pytestmark = pytest.mark.unit

SECRET = b"webhook-secret"
REPO = "https://github.com/acme/widgets"
URL = f"{REPO}/pull/7"


def sign(body: bytes, secret: bytes = SECRET) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def pull_request(action: str = "opened", state: str = "open", merged: bool = False, head: str = "a1") -> dict[str, Any]:
    return {
        "action": action,
        "pull_request": {
            "html_url": URL,
            "state": state,
            "merged": merged,
            "head": {"sha": head, "ref": "feature", "label": "acme:feature"},
        },
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


def setup(lab=None):
    bus = open_bus(BusConfig())
    pulls = Pulls(bus, fetch=lambda url: pytest.fail("unexpected fetch"), clock=lambda: 1000)
    pulls.track("crew", URL)
    return bus, Ingress(pulls, SECRET, lab and lab(bus)), bus.events.consumer("watch", "swarm.>")


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
    assert deliver(ingress, "pull_request", pull_request()) == 200
    events = drain(consumer)
    assert [(e.swarm, e.type, e.key) for e in events] == [("crew", "pull.opened", URL)]
    assert isinstance(events[0], Envelope)
    assert deliver(ingress, "pull_request", pull_request("synchronize", head="b2")) == 200
    assert deliver(ingress, "pull_request", pull_request("enqueued", head="b2")) == 200
    assert deliver(ingress, "pull_request", pull_request("closed", "closed", True, "b2")) == 200
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
    assert deliver(ingress, "check_suite", check_suite(numbers=(7, 8))) == 200
    events = drain(consumer)
    assert [(e.type, e.data["suite"], e.data["app"], e.data["conclusion"]) for e in events] == [
        ("checks.completed", "9", "github-actions", "success")
    ]
    assert deliver(ingress, "check_suite", check_suite("requested")) == 200
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
    assert deliver(ingress, "ping", {"zen": "x"}) == 200
    body = b"{}"
    assert ingress.receive({"X-Hub-Signature-256": sign(body)}, body) == 200
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
    def __init__(self, status=200):
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
    assert request(ingress, body=body, length=str(limit))[0] == ("status", 200)
    assert ingress.calls == [({"Content-Length": str(limit), "X-GitHub-Event": "ping"}, body)]
    assert request(ingress, body=b"abcdef", length="3")[0] == ("status", 200)
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
    assert deliver(ingress, "ping", {}) == 200


def test_the_receiver_binds_its_bucket_on_a_delivery_until_the_hub_creates_it(capsys):
    inner = open_bus(BusConfig())
    bound = []

    class Bus:
        events = inner.events

        def state(self, bucket):
            bound.append(bucket)
            if len(bound) == 1:
                raise LookupError("bucket not found")
            return inner.state(bucket)

    ingress = Ingress(github_webhooks.LazyPulls(Bus()), SECRET)
    assert deliver(ingress, "ping", {}) == 200
    assert deliver(ingress, "check_suite", check_suite(action="requested")) == 200
    assert bound == []
    assert deliver(ingress, "pull_request", pull_request()) == 503
    assert capsys.readouterr().err.endswith("LookupError: bucket not found\n")
    assert deliver(ingress, "pull_request", pull_request()) == 200
    assert deliver(ingress, "pull_request", pull_request()) == 200
    assert bound == [pulls.BUCKET, pulls.BUCKET]


def test_http_handler_logs_one_line_per_delivery(capsys):
    request(Recorder(), headers={"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "d-1"})
    request(Recorder(401), headers={"X-GitHub-Event": "check_suite", "X-GitHub-Delivery": "d-2"})
    request(Recorder(), "/other")
    request(Recorder(401), headers={"X-GitHub-Event": "e" * 65, "X-GitHub-Delivery": "d" * 64})
    assert capsys.readouterr().err == (
        "github-webhook: delivery d-1 event pull_request answered 200\n"
        "github-webhook: delivery d-2 event check_suite answered 401\n"
        "github-webhook: delivery None event ping answered 404\n"
        f"github-webhook: delivery {'d' * 64} event {'e' * 64} answered 401\n"
    )
    handler = github_webhooks.handler(Recorder())
    assert handler.log_message(None, "%s", "x") is None
    assert capsys.readouterr().err == ""


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
        ("signed", ("status", 200)),
        ("serve",),
        ("close",),
        ("bus closed",),
    ]
    served.clear()
    with pytest.raises(KeyboardInterrupt):
        github_webhooks.main([])
    assert served[1] == ("init", ("127.0.0.1", 8790), "Webhook")


@pytest.mark.feature("hub-and-webhook-stop-cleanly-on-interrupt")
def test_sigint_while_serving_ends_the_receiver_with_0_after_closing_server_and_bus(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGENTIHOOKS_GITHUB_WEBHOOK_SECRET", "s")
    monkeypatch.delenv(github_webhooks.NOTICE_SECRET_ENV, raising=False)
    monkeypatch.setenv("AGENTIHOOKS_BUS", "memory")
    served: list[str] = []

    class Bus:
        def __init__(self, config: BusConfig) -> None:
            served.append("bus")

        def close(self) -> None:
            served.append("bus closed")

    class Server:
        def __init__(self, address: tuple[str, int], handler: type) -> None:
            served.append("init")

        def serve_forever(self) -> None:
            served.append("serve")
            signal.raise_signal(signal.SIGINT)

        def server_close(self) -> None:
            served.append("close")

    monkeypatch.setattr(github_webhooks, "open_bus", Bus)
    monkeypatch.setattr(github_webhooks, "ThreadingHTTPServer", Server)
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        try:
            code: object = github_webhooks.main(["--port", "9001"])
        except SystemExit as exited:
            code = exited.code
        except KeyboardInterrupt:
            code = "KeyboardInterrupt escaped the webhook command"
        interrupted = signal.getsignal(signal.SIGINT)
    finally:
        signal.signal(signal.SIGINT, previous)
    assert code == 0
    assert served == ["bus", "init", "serve", "close", "bus closed"]
    assert "Traceback" not in capsys.readouterr().err
    assert interrupted is signal.default_int_handler


def test_the_cli_delegates_github_webhook_to_this_module():
    from scripts.cli_delegates import DELEGATED_CLIS

    assert DELEGATED_CLIS["github-webhook"] == "scripts.hub.github_webhooks"


def test_the_hub_registry_runs_the_pulls_reactor_on_the_hub_clock():
    from scripts.hub import registry

    bus = open_bus(BusConfig())
    reactors = {reactor.name: reactor for reactor in registry.configured(bus, lambda: 4242)}
    reactors["pulls"].poll(lambda: True)
    assert json.loads(bus.state("hub-github").get("polled").value) == 4242


NOTICE_SECRET = b"notice-secret"
NOTICE = {"repo": REPO, "head": "a1", "base": "b0", "actor": "agenticore-app[bot]"}


def lab_setup():
    bus, ingress, swarm = setup(lambda bus: LabIntake(bus.lab_heads, NOTICE_SECRET))
    return ingress, swarm, bus.lab_heads.consumer("lab", "ci.lab.head.>")


def merge_group(action="checks_requested"):
    return {
        "action": action,
        "repository": {"html_url": REPO},
        "sender": {"login": "github-merge-queue[bot]"},
        "merge_group": {
            "head_sha": "a1",
            "base_sha": "b0",
            "head_ref": "refs/heads/gh-readonly-queue/dev/pr-7-c3",
            "base_ref": "refs/heads/dev",
        },
    }


def lab_pull_request(action: str = "opened", head: str = "a1") -> dict[str, Any]:
    payload = pull_request(action, head=head)
    payload["repository"] = {"html_url": REPO}
    payload["sender"] = {"login": "nestorcolt"}
    payload["pull_request"]["base"] = {"sha": "b0", "label": "acme:dev"}
    payload["pull_request"]["user"] = {"login": "agenticore-app[bot]"}
    return payload


def send_notice(ingress, payload, signature=None):
    body = json.dumps(payload).encode()
    return ingress.notice({"X-Hub-Signature-256": sign(body, NOTICE_SECRET) if signature is None else signature}, body)


def test_a_merge_group_asking_for_checks_is_published_as_a_lab_head():
    ingress, swarm, lab = lab_setup()
    assert deliver(ingress, "merge_group", merge_group()) == 200
    assert [(e.swarm, e.type) for e in drain(swarm)] == [("crew", "merge_group.requested")]
    assert [(e.type, e.data["head"], e.data["base"], e.author) for e in drain(lab)] == [
        ("ci.lab.head.merge_group", "a1", "b0", "github-merge-queue[bot]")
    ]
    assert deliver(ingress, "merge_group", merge_group("destroyed")) == 200
    assert drain(lab) == []


def test_a_wired_lab_keeps_the_pull_request_events_and_adds_the_lab_head():
    ingress, swarm, lab = lab_setup()
    assert deliver(ingress, "pull_request", lab_pull_request()) == 200
    assert [(e.swarm, e.type, e.key) for e in drain(swarm)] == [("crew", "pull.opened", URL)]
    assert [(e.type, e.data["action"]) for e in drain(lab)] == [("ci.lab.head.pull_request", "opened")]
    assert deliver(ingress, "pull_request", lab_pull_request("enqueued")) == 200
    assert [e.type for e in drain(swarm)] == ["merge.queued"]
    assert drain(lab) == []


def test_a_wired_lab_keeps_the_check_suite_events_and_publishes_no_head_for_them():
    ingress, swarm, lab = lab_setup()
    deliver(ingress, "pull_request", lab_pull_request())
    drain(swarm)
    drain(lab)
    assert deliver(ingress, "check_suite", check_suite()) == 200
    assert [e.type for e in drain(swarm)] == ["checks.completed"]
    assert drain(lab) == []


def check_run_delivery(
    action: str = "rerequested", name: str = "lab-shadow/lint", conclusion: str | None = None
) -> dict[str, Any]:
    return {
        "action": action,
        "repository": {"html_url": REPO},
        "sender": {"login": "nestorcolt"},
        "check_run": {
            "id": 11,
            "name": name,
            "head_sha": "a1",
            "conclusion": conclusion,
            "app": {"slug": "github-actions"},
            "output": {"title": None, "summary": None},
            "pull_requests": [{"number": 7, "base": {"sha": "b0"}}],
        },
    }


def test_a_wired_lab_publishes_a_rerequested_lab_check_run_as_a_head() -> None:
    ingress, _, lab = lab_setup()
    assert deliver(ingress, "check_run", check_run_delivery()) == 200
    assert [(e.type, e.key, e.author, e.data["base"], e.data["action"]) for e in drain(lab)] == [
        ("ci.lab.head.check_run", f"{REPO}@a1", "nestorcolt", "b0", "rerequested")
    ]


@pytest.mark.feature("lab-completed-check-shadow")
def test_a_wired_lab_publishes_a_completed_check_run_on_its_own_subject_for_the_shadow_comparison() -> None:
    ingress, _, lab = lab_setup()
    assert deliver(ingress, "check_run", check_run_delivery("completed", "lint", "success")) == 200
    run = {"name": "lint", "head_sha": "a1", "conclusion": "success", "app": {"slug": "github-actions"}}
    assert [(e.type, e.key, e.author, e.data["check_run"]) for e in drain(lab)] == [
        ("ci.lab.head.check_completed", f"{REPO}@a1", "nestorcolt", {**run, "output": {"summary": None}})
    ]


def test_a_wired_lab_answers_400_for_a_head_payload_it_cannot_read():
    ingress, swarm, lab = lab_setup()
    assert deliver(ingress, "merge_group", {"action": "checks_requested"}) == 400
    assert drain(swarm) == []
    assert drain(lab) == []


def test_a_wired_lab_still_records_the_pull_request_when_its_head_cannot_be_read():
    ingress, swarm, lab = lab_setup()
    payload = lab_pull_request()
    del payload["pull_request"]["user"]
    assert deliver(ingress, "pull_request", payload) == 400
    assert [e.type for e in drain(swarm)] == ["pull.opened"]
    assert drain(lab) == []


def test_a_signed_head_notice_is_published_as_a_lab_head():
    ingress, swarm, lab = lab_setup()
    assert send_notice(ingress, NOTICE) == 200
    assert drain(swarm) == []
    assert [(e.type, e.key, e.author, e.data["action"]) for e in drain(lab)] == [
        ("ci.lab.head.notice", f"{REPO}@a1", "agenticore-app[bot]", "pushed")
    ]


@pytest.mark.parametrize(
    "signature", ["", "sha256=" + "0" * 64, sign(json.dumps(NOTICE).encode()), sign(b"other", NOTICE_SECRET)]
)
def test_an_unsigned_or_wrongly_signed_head_notice_answers_401(signature):
    ingress, _, lab = lab_setup()
    assert send_notice(ingress, NOTICE, signature) == 401
    assert ingress.notice({}, json.dumps(NOTICE).encode()) == 401
    assert drain(lab) == []


@pytest.mark.parametrize("payload", [{**NOTICE, "actor": ""}, {"repo": REPO}, [], "x"])
def test_a_signed_but_malformed_head_notice_is_a_bad_request(payload):
    ingress, _, lab = lab_setup()
    assert send_notice(ingress, payload) == 400
    assert drain(lab) == []


def test_a_head_notice_is_not_served_without_the_lab():
    _, ingress, _ = setup()
    assert send_notice(ingress, NOTICE) == 404


def test_a_lab_bus_failure_answers_503_so_the_sender_retries(capsys):
    class Down:
        def publish(self, subject, event):
            raise ConnectionError("lab bus down")

    _, ingress, _ = setup(lambda bus: LabIntake(Down(), NOTICE_SECRET))
    assert send_notice(ingress, NOTICE) == 503
    assert deliver(ingress, "merge_group", merge_group()) == 503
    assert capsys.readouterr().err.endswith("ConnectionError: lab bus down\n")
    assert deliver(ingress, "ping", {}) == 200


class NoticeRecorder(Recorder):
    def __init__(self, status=200):
        super().__init__(status)
        self.notices = []

    def notice(self, headers, body):
        self.notices.append((dict(headers), body))
        return self.status


def test_http_handler_routes_the_head_notice_to_the_ingress():
    ingress = NoticeRecorder(401)
    assert request(ingress, "/lab/head", body=b'{"a": 1}') == [("status", 401), ("Content-Length", "0"), ("end",)]
    assert ingress.notices == [({"Content-Length": "8", "X-GitHub-Event": "ping"}, b'{"a": 1}')]
    assert ingress.calls == []
    assert request(ingress, "/lab/head", length="")[0] == ("status", 411)
    assert request(ingress, "/lab", body=b"{}")[0] == ("status", 404)
    assert len(ingress.notices) == 1


@pytest.mark.feature("lab-head-own-stream")
def test_main_wires_the_lab_intake_only_when_its_notice_secret_is_set(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_GITHUB_WEBHOOK_SECRET", "s")
    monkeypatch.setenv("AGENTIHOOKS_BUS", "memory")
    monkeypatch.delenv("AGENTIHOOKS_CI_LAB_NOTICE_SECRET", raising=False)
    buses = []
    answers = []

    def opener(config):
        buses.append(open_bus(BusConfig()))
        return buses[-1]

    class Server:
        def __init__(self, address, handler):
            body = json.dumps(NOTICE).encode()
            signed = {"X-Hub-Signature-256": sign(body, NOTICE_SECRET)}
            answers.append(request(None, "/lab/head", body=body, cls=handler, headers=signed)[0])

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    monkeypatch.setattr(github_webhooks, "open_bus", opener)
    monkeypatch.setattr(github_webhooks, "ThreadingHTTPServer", Server)
    with pytest.raises(KeyboardInterrupt):
        github_webhooks.main([])
    monkeypatch.setenv("AGENTIHOOKS_CI_LAB_NOTICE_SECRET", NOTICE_SECRET.decode())
    with pytest.raises(KeyboardInterrupt):
        github_webhooks.main([])
    assert answers == [("status", 404), ("status", 200)]
    assert [subject for subject, _ in buses[0].lab_heads.entries] == []
    assert [subject for subject, _ in buses[1].lab_heads.entries] == ["ci.lab.head.notice"]
    assert [subject for subject, _ in buses[1].events.entries] == []


@pytest.mark.feature("github-webhook-scoped-receiver")
def test_a_scoped_receiver_without_provision_publishes_a_merge_group_on_the_swarm_scoped_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTIHOOKS_GITHUB_WEBHOOK_SECRET", SECRET.decode())
    monkeypatch.setenv("AGENTIHOOKS_BUS", "memory")
    monkeypatch.setenv("AGENTIHOOKS_BUS_SCOPED", "1")
    monkeypatch.delenv(github_webhooks.NOTICE_SECRET_ENV, raising=False)
    scoped = open_bus(BusConfig(scoped=True))
    root = open_bus(BusConfig())
    Pulls(cast(Bus, root), clock=lambda: 1000).track("crew", URL)
    opened: list[tuple[str, bool, bool]] = []
    answers: list[object] = []

    class ReceiverBus:
        def __init__(self, config: BusConfig) -> None:
            opened.append(("open", config.scoped, config.provision))
            self.config = config
            self.events = (scoped if config.scoped else root).events

        def state(self, bucket: str) -> StateStore:
            if self.config.scoped and not self.config.provision:
                raise PermissionError("select a swarm before opening scoped state")
            return root.state(bucket)

        def close(self) -> None:
            opened.append(("closed", self.config.scoped, self.config.provision))

    class Server:
        def __init__(self, address: tuple[str, int], handler: type) -> None:
            body = json.dumps(merge_group()).encode()
            headers = {"X-GitHub-Event": "merge_group", "X-Hub-Signature-256": sign(body)}
            answers.extend(request(None, body=body, cls=handler, headers=headers)[:1])

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            pass

    review = scoped.for_swarm("crew").events.consumer("review", subject("crew", "merge_group", "requested"))
    legacy = root.events.consumer("legacy", "swarm.>")
    monkeypatch.setattr(github_webhooks, "open_bus", ReceiverBus)
    monkeypatch.setattr(github_webhooks, "ThreadingHTTPServer", Server)
    with pytest.raises(KeyboardInterrupt):
        github_webhooks.main([])
    assert answers == [("status", 200)]
    assert [(event.swarm, event.type, event.key) for event in drain(review)] == [
        ("crew", "merge_group.requested", "a1")
    ]
    assert drain(legacy) == []
    assert opened == [
        ("open", True, False),
        ("open", False, False),
        ("closed", True, False),
        ("closed", False, False),
    ]


def release(action="published", tag="v1.2.3"):
    return {
        "action": action,
        "repository": {"html_url": REPO},
        "release": {"tag_name": tag, "html_url": f"{REPO}/releases/tag/{tag}"},
    }


def untracked(payload):
    payload["repository"] = {"html_url": "https://github.com/acme/gadgets"}
    return payload


def test_a_merge_group_for_an_untracked_repository_still_reaches_the_lab_and_publishes_no_swarm_event():
    ingress, swarm, lab = lab_setup()
    assert deliver(ingress, "merge_group", untracked(merge_group())) == 200
    assert drain(swarm) == []
    assert [e.type for e in drain(lab)] == ["ci.lab.head.merge_group"]


def test_a_release_for_an_untracked_repository_answers_ok_and_publishes_nothing():
    _, ingress, swarm = setup()
    assert deliver(ingress, "release", untracked(release())) == 200
    assert drain(swarm) == []


@pytest.mark.feature("swarm-review-trigger")
def test_repository_events_from_recorded_payloads():
    assert github_webhooks.repository_events("merge_group", merge_group()) == [
        (
            REPO,
            "merge_group.requested",
            "a1",
            {"head": "a1", "base": "b0", "head_ref": "refs/heads/gh-readonly-queue/dev/pr-7-c3", "number": 7},
        )
    ]
    assert github_webhooks.repository_events("release", release()) == [
        (REPO, "release.published", "v1.2.3", {"tag": "v1.2.3", "url": f"{REPO}/releases/tag/v1.2.3"})
    ]
    assert github_webhooks.repository_events("merge_group", merge_group("destroyed")) == []
    assert github_webhooks.repository_events("release", release("created")) == []
    assert github_webhooks.repository_events("pull_request", pull_request()) == []


@pytest.mark.feature("swarm-review-trigger")
def test_a_merge_group_without_a_head_ref_names_no_pull_request() -> None:
    payload = merge_group()
    del payload["merge_group"]["head_ref"]
    [(_, _, _, data)] = github_webhooks.repository_events("merge_group", payload)
    assert (data["head_ref"], data["number"]) == ("", None)


@pytest.mark.feature("swarm-review-trigger")
def test_the_lazy_pull_tracker_announces_a_repository_event_to_the_tracking_swarm() -> None:
    bus, _, _ = setup()
    consumer = bus.events.consumer("release", "swarm.crew.release.published")
    github_webhooks.LazyPulls(cast(Bus, bus)).announce(REPO, "release.published", "v1", {"tag": "v1"})
    [event] = drain(consumer)
    assert (event.swarm, event.type, event.key, event.data) == (
        "crew",
        "release.published",
        "v1",
        {"repository": REPO, "tag": "v1"},
    )


@pytest.mark.feature("swarm-review-trigger")
def test_a_merge_group_and_a_release_reach_each_swarm_tracking_the_repository():
    _, ingress, swarm = setup()
    assert deliver(ingress, "merge_group", merge_group()) == 200
    assert deliver(ingress, "release", release()) == 200
    events = drain(swarm)
    assert [(e.swarm, e.type, e.key) for e in events] == [
        ("crew", "merge_group.requested", "a1"),
        ("crew", "release.published", "v1.2.3"),
    ]
    assert (events[0].data["number"], events[1].data["repository"]) == (7, REPO)


@pytest.mark.feature("swarm-review-trigger")
def test_a_failed_repository_publish_answers_503_after_the_lab_head_is_published(monkeypatch, capsys):
    bus, ingress, swarm = setup(lambda bus: LabIntake(bus.lab_heads, NOTICE_SECRET))
    lab = bus.lab_heads.consumer("lab", "ci.lab.head.>")

    def refuse(*args):
        raise RuntimeError("broker refused")

    monkeypatch.setattr(Pulls, "announce", refuse)
    assert deliver(ingress, "merge_group", merge_group()) == 503
    assert [e.type for e in drain(lab)] == ["ci.lab.head.merge_group"]
    assert drain(swarm) == []
