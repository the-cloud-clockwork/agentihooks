import json
import subprocess

import pytest

from scripts.swarm import cli, commands, quota_refresh, timer
from scripts.swarm.store import PREFIX, RedisStore, SwarmConfig, SwarmError


@pytest.fixture
def store():
    import fakeredis

    saved = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.create(SwarmConfig("sw", ".", 0, 0, state="paused"))
    return saved


@pytest.fixture
def fresh(monkeypatch):
    from scripts import agents_quota

    page = {"probed_at": 50, "rows": ["fresh"]}
    monkeypatch.setattr(agents_quota, "page_quota", lambda: page)
    return page


def test_the_wake_channel_and_page_lifetime():
    assert quota_refresh.WAKE == f"{PREFIX}:quota-refresh"
    assert (quota_refresh.COMMAND, quota_refresh.WAIT_S, quota_refresh.PAGE_TTL_S) == ("quota", 30, 300)


def test_a_request_queues_one_quota_row_and_wakes_the_prober_at_once(store):
    pubsub = store.redis.pubsub()
    pubsub.subscribe(quota_refresh.WAKE)
    assert pubsub.get_message(timeout=1)["type"] == "subscribe"
    row = quota_refresh.request(store, "sw")
    assert [(r["id"], r["command"], r["argv"], r["state"]) for r in commands.rows(store, "sw")] == [
        (row["id"], "quota", [], "pending")
    ]
    message = pubsub.get_message(timeout=1)
    assert (message["channel"], message["data"]) == (quota_refresh.WAKE, "sw")


def test_answer_probes_only_the_quota_rows_and_writes_the_fresh_page_quota(store, fresh, monkeypatch):
    monkeypatch.setattr(commands, "hive_id", lambda: "hive-a")
    commands.submit(store, "sw", "swarm", ["pause"])
    quota_refresh.request(store, "sw")
    ran = []
    actions = quota_refresh.answer(store, "sw", lambda slug, row: ran.append((slug, row["command"])) or "")
    assert actions == ["control quota acknowledged"]
    assert ran == [("sw", "quota")]
    assert [(r["command"], r["state"], r["owner"]) for r in commands.rows(store, "sw")] == [
        ("swarm", "pending", ""),
        ("quota", "acknowledged", "hive-a"),
    ]
    key = quota_refresh.page_key(store, "sw")
    assert key == f"{PREFIX}:sw:quota-page"
    assert json.loads(store.redis.get(key)) == fresh
    assert 299 <= store.redis.ttl(key) <= 300


def test_a_failed_probe_is_recorded_and_leaves_the_page_quota_alone(store, fresh):
    quota_refresh.request(store, "sw")
    assert quota_refresh.answer(store, "sw", lambda slug, row: "probe failed") == ["control quota failed"]
    assert commands.rows(store, "sw")[0]["error"] == "probe failed"
    assert store.redis.get(quota_refresh.page_key(store, "sw")) is None


def test_answer_without_a_pending_quota_row_writes_nothing_and_takes_no_lease(store, fresh):
    commands.submit(store, "sw", "swarm", ["pause"])
    assert quota_refresh.answer(store, "sw", lambda slug, row: pytest.fail("ran a row")) == []
    assert store.redis.get(quota_refresh.page_key(store, "sw")) is None
    assert store.redis.get(store.key("sw", "control-owner")) is None


def test_the_newer_probe_wins_and_a_missing_side_yields_the_other():
    old, new, bare = {"probed_at": 10}, {"probed_at": 20}, {}
    assert quota_refresh.fresher(old, None) is old
    assert quota_refresh.fresher(None, new) is new
    assert quota_refresh.fresher(None, None) is None
    assert quota_refresh.fresher(old, new) is new
    assert quota_refresh.fresher(new, old) is new
    assert quota_refresh.fresher(new, {"probed_at": 20}) == {"probed_at": 20}
    assert quota_refresh.fresher(new, {"probed_at": 20}) is not new
    assert quota_refresh.fresher(bare, {"probed_at": None}) == {"probed_at": None}
    assert quota_refresh.fresher(new, bare) is new
    assert quota_refresh.fresher({"probed_at": None}, {"probed_at": 1}) == {"probed_at": 1}
    unread, probe = {"probed_at": None}, {"probed_at": None, "rows": ["probe"]}
    assert quota_refresh.fresher(unread, probe) is probe
    assert quota_refresh.fresher({"probed_at": 1}, unread) == {"probed_at": 1}


def test_the_status_view_carries_the_newer_of_the_published_and_the_probed_quota(store):
    commands.publish(store, "sw", {"agents": [], "tasks": {}, "quota": {"probed_at": 10, "rows": ["tick"]}}, {})
    assert commands.view(store, "sw")["quota"]["rows"] == ["tick"]
    store.redis.set(quota_refresh.page_key(store, "sw"), json.dumps({"probed_at": 20, "rows": ["probe"]}))
    assert commands.view(store, "sw")["quota"]["rows"] == ["probe"]
    commands.publish(store, "sw", {"agents": [], "tasks": {}, "quota": {"probed_at": 30, "rows": ["later"]}}, {})
    assert commands.view(store, "sw")["quota"]["rows"] == ["later"]


def test_the_status_view_without_any_quota_adds_none(store):
    commands.publish(store, "sw", {"agents": [], "tasks": {}}, {})
    assert "quota" not in commands.view(store, "sw")


def test_a_page_request_reaches_the_status_view_after_one_prober_round(store, fresh):
    commands.publish(store, "sw", {"agents": [], "tasks": {}, "quota": {"probed_at": 10, "rows": ["tick"]}}, {})
    quota_refresh.request(store, "sw")
    assert quota_refresh.answer_all(store, lambda slug, row: "") == ["sw: control quota acknowledged"]
    assert commands.view(store, "sw")["quota"] == fresh


def test_claim_accepts_a_pending_row_once(store):
    row = quota_refresh.request(store, "sw")
    records = store.key("sw", "commands")
    claimed = commands.claim(store, records, row["id"], "hive-a", 3)
    assert (claimed["state"], claimed["owner"], claimed["epoch"]) == ("accepted", "hive-a", 3)
    assert json.loads(store.redis.hget(records, row["id"])) == claimed
    assert commands.claim(store, records, row["id"], "hive-b", 3) is None


def test_a_row_another_process_accepts_mid_claim_is_left_to_it(store, monkeypatch):
    row = quota_refresh.request(store, "sw")
    records = store.key("sw", "commands")
    real, fired = store.redis.pipeline, []

    def pipeline(*args, **kwargs):
        pipe = real(*args, **kwargs)
        read = pipe.hget

        def racing(name, key):
            value = read(name, key)
            if not fired:
                fired.append(key)
                store.redis.hset(records, key, json.dumps({**json.loads(value), "state": "accepted"}))
            return value

        pipe.hget = racing
        return pipe

    monkeypatch.setattr(store.redis, "pipeline", pipeline)
    assert commands.claim(store, records, row["id"], "hive-a", 1) is None
    assert fired == [row["id"]]
    assert json.loads(store.redis.hget(records, row["id"]))["owner"] == ""


def test_consume_moves_past_a_row_another_process_claimed_to_the_next_one(store, monkeypatch):
    lost, kept = quota_refresh.request(store, "sw"), quota_refresh.request(store, "sw")
    real = commands.claim
    monkeypatch.setattr(
        commands,
        "claim",
        lambda store_, records, key, owner, epoch: (
            None if key == lost["id"] else real(store_, records, key, owner, epoch)
        ),
    )
    ran = []
    assert commands.consume(store, "sw", "hive-a", lambda row: ran.append(row["id"]) or "") == [
        "control quota acknowledged"
    ]
    assert ran == [kept["id"]]


def test_answer_all_reports_every_swarm_and_carries_on_past_a_refused_one(store, monkeypatch, capsys):
    store.create(SwarmConfig("two", ".", 0, 0, state="paused"))

    def answer(store, slug, execute):
        if slug == "sw":
            raise SwarmError("the controller lease is stale")
        return ["control quota acknowledged"]

    monkeypatch.setattr(quota_refresh, "answer", answer)
    lines = quota_refresh.answer_all(store, lambda slug, row: "")
    assert lines == ["sw: the controller lease is stale", "two: control quota acknowledged"]
    assert capsys.readouterr().out == "sw: the controller lease is stale\ntwo: control quota acknowledged\n"


def test_answer_all_flushes_each_line_so_the_service_journal_shows_it_at_once(store, monkeypatch):
    printed = []
    monkeypatch.setattr(quota_refresh, "answer", lambda store, slug, execute: ["control quota acknowledged"])
    monkeypatch.setattr(quota_refresh, "print", lambda *args, **kwargs: printed.append((args, kwargs)), raising=False)
    quota_refresh.answer_all(store, lambda slug, row: "")
    assert printed == [(("sw: control quota acknowledged",), {"flush": True})]


class Stop(Exception):
    pass


def test_run_listens_on_the_wake_channel_and_answers_every_swarm_on_each_wake(store, monkeypatch):
    seen, rounds = [], []

    class PubSub:
        def subscribe(self, channel):
            seen.append(("subscribe", channel))

        def get_message(self, timeout):
            seen.append(("wait", timeout))

    def answer_all(store_, execute):
        rounds.append((store_, execute))
        if len(rounds) == 2:
            raise Stop

    def execute(slug, row):
        return ""

    monkeypatch.setattr(store.redis, "pubsub", PubSub)
    monkeypatch.setattr(quota_refresh, "answer_all", answer_all)
    with pytest.raises(Stop):
        quota_refresh.run(store, execute, 7)
    assert seen == [("subscribe", quota_refresh.WAKE), ("wait", 7), ("wait", 7)]
    assert rounds == [(store, execute), (store, execute)]


def test_timer_ensure_also_runs_the_quota_prober_service(tmp_path):
    calls = []
    timer.ensure(
        "/bin/agentihooks",
        tmp_path,
        run=lambda argv, **kw: calls.append((argv, kw)) or subprocess.CompletedProcess(argv, 0),
    )
    assert (tmp_path / "agentihooks-quota-prober.service").read_text() == (
        "[Unit]\nDescription=agentihooks quota prober for the ledger page refresh\n\n"
        "[Service]\nType=simple\nEnvironment=PYTHONUNBUFFERED=1\n"
        "Environment=PATH=%h/.local/bin:%h/.cargo/bin:/usr/local/bin:/usr/bin:/bin\n"
        "EnvironmentFile=-%h/.agentihooks/.env\n"
        'ExecStart="/bin/agentihooks" swarm prober\n'
        "Restart=always\nRestartSec=5\n\n"
        "[Install]\nWantedBy=default.target\n"
    )
    enable = ["systemctl", "--user", "enable", "--now", "agentihooks-quota-prober.service"]
    assert (enable, {"capture_output": True, "text": True, "timeout": 30}) in calls


def test_main_runs_the_quota_prober_without_a_slug_on_the_hive_command_runner(monkeypatch):
    from scripts.swarm import command_runner

    ran, executed = [], []
    monkeypatch.setattr(cli, "connect", lambda: "store")
    monkeypatch.setattr(quota_refresh, "run", lambda store, execute, wait_s: ran.append((store, wait_s, execute)))
    monkeypatch.setattr(command_runner, "execute", lambda store, slug, row: executed.append((store, slug, row)) or "")
    assert cli.main(["prober"]) == 0
    [(store, wait_s, execute)] = ran
    assert (store, wait_s) == ("store", 30)
    assert execute("sw", {"command": "quota"}) == ""
    assert executed == [("store", "sw", {"command": "quota"})]
