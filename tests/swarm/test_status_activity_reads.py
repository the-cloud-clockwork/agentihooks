from unittest.mock import patch

from scripts.swarm import status
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm_v2 import accounts


def test_status_batches_account_discovery_without_losing_findings(monkeypatch):
    import fakeredis

    redis = fakeredis.FakeRedis(decode_responses=True)
    store = RedisStore(redis)
    store.create(SwarmConfig("sw", "/repo", 0, 0))
    redis.mset({f"historical:{number}": "old" for number in range(12_000)})
    holders = {"alpha": "sw/eng-1", "middle": "sw/eng-2", "zulu": "sw/eng-3"}
    for account, holder in holders.items():
        slot = accounts.Slot(account, holder, f"execution-{account}", 7, "reserved", expires_ms=1)
        redis.hset(accounts.account_key(account), holder, accounts.encode(slot))
    foreign = accounts.Slot("foreign", "other/eng-1", "other-execution", 9, "reserved", expires_ms=1)
    redis.hset(accounts.account_key("foreign"), foreign.holder, accounts.encode(foreign))
    monkeypatch.setattr(status, "page_quota", lambda: {})

    with patch.object(redis, "execute_command", wraps=redis.execute_command) as commands:
        report = status.status_report(store, "sw", {"tasks": []})

    assert report["account_reconciliation"]["findings"] == [
        {
            "kind": "expired_reservation",
            "action": "release",
            "account": account,
            "holder": holder,
            "execution_id": f"execution-{account}",
            "generation": 7,
            "evidence": "",
        }
        for account, holder in holders.items()
    ]
    assert redis.hget(accounts.account_key("foreign"), foreign.holder) == accounts.encode(foreign)
    assert commands.call_count < 100
