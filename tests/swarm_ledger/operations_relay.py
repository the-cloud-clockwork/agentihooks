from dataclasses import asdict
from unittest.mock import Mock


def install(tmp_path, monkeypatch, slug, server):
    import fakeredis

    from hooks import config
    from scripts.inbox.store import InboxStore
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

    home = tmp_path / "home"
    home.mkdir(parents=True)
    monkeypatch.setattr(config, "AGENTIHOOKS_HOME", str(home))
    redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("scripts.inbox.store.now_ms", lambda: 1791939600000)
    store = RedisStore(redis)
    store.create(SwarmConfig(slug, str(tmp_path), 1, 0))
    master = AgentRecord("replay-master", "master", "master", seat=f"master@{slug}")
    store.put_agent(slug, master)
    store.seats.occupy(master.seat, master.name, 1)
    control = Mock(return_value={"ok": True})
    monkeypatch.setattr(server, "swarm_control", control)
    callbacks = {
        name: Mock(wraps=getattr(server, name)) for name in ("relay_to_inbox", "doctor_phrase", "deliver_alerts")
    }
    for name, callback in callbacks.items():
        monkeypatch.setattr(server, name, callback)
    inbox = InboxStore(redis)
    monkeypatch.setattr("scripts.inbox.store.connect", lambda environ=None: inbox)

    def outcomes():
        marks = sorted(
            [*redis.scan_iter(inbox.key("ledger-sent", "*")), *redis.scan_iter(inbox.key("alert-sent", "*"))]
        )
        return {
            "items": [asdict(item) for item in inbox.inbox(master.seat)],
            "receipts": {mark.removeprefix(inbox.key()): redis.get(mark) for mark in marks},
            "unrelayed": server.unrelayed(),
            "control": [call.args for call in control.call_args_list],
        }

    return callbacks, outcomes
