import pytest

from scripts.doctor import tick_delivery

pytestmark = pytest.mark.unit


def test_a_proven_scheduled_tick_raises_no_finding():
    assert tick_delivery.findings("", "host") == []


def test_an_unproven_scheduled_tick_raises_one_finding_naming_why():
    why = "no scheduler has delivered a swarm tick yet"
    [finding] = tick_delivery.findings(why, "host")
    assert finding.kind == "scheduled tick"
    assert finding.subject == "host"
    assert finding.id == "scheduled-tick/host"
    assert finding.summary == "the scheduler has not proven the swarm tick, so the systemd timer stays"
    assert finding.evidence == (why,)
    assert finding.threshold == "a swarm tick delivered by the installed code within the last 690 seconds"
    assert finding.measure == 1


def test_the_detector_reads_the_installed_delivery_for_this_machine(monkeypatch):
    from scripts.swarm import timer

    monkeypatch.setattr(timer, "undelivered", lambda: "the last swarm tick was delivered 400 seconds ago")
    monkeypatch.setattr(tick_delivery.socket, "gethostname", lambda: "box")
    [finding] = tick_delivery.read()
    assert finding.id == "scheduled-tick/box"
    assert finding.evidence == ("the last swarm tick was delivered 400 seconds ago",)
    monkeypatch.setattr(timer, "undelivered", lambda: "")
    assert tick_delivery.read() == []


def test_every_doctor_pass_runs_the_scheduled_tick_detector(monkeypatch):
    import fakeredis

    from scripts.doctor import detect
    from scripts.swarm.store import RedisStore

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    found = detect.readers(store, None, "sw", 1_000, environ={}, telemetry=False)
    monkeypatch.setattr(tick_delivery, "read", lambda: ["finding"])
    assert found["scheduled tick"]() == ["finding"]
