import json
from dataclasses import replace
from pathlib import Path

import pytest

from hooks.context import broadcast as hb
from scripts.swarm.store import RedisStore, SwarmError
from scripts.swarm_v2 import broadcasts
from scripts.swarm_v2.auth_context import GrantRefused
from tests.sv2_ctl02_cases import build

pytestmark = pytest.mark.unit

INPUTS = json.loads((Path(__file__).parent / "fixtures/swarm_v2/fleet-broadcasts.json").read_text())
SLUG = "fixture"
LOCAL, REMOTE, CHANNELS, PROJECT = INPUTS["local_seat"], INPUTS["remote_seat"], INPUTS["channels"], INPUTS["project"]
WARNING, EXPIRED, PERSONAL = INPUTS["warning"], INPUTS["expired"], INPUTS["personal"]
OTHER, NOTE, FOLLOWUP = INPUTS["other_fleet"], INPUTS["agent_note"], INPUTS["followup"]
PUBLISH_MS, CLAIM_MS = INPUTS["publish_ms"], INPUTS["claim_ms"]
OPERATOR = "-".join(("operator", "fixture", "credential"))


class World:
    def __init__(self, monkeypatch):
        self.store, self.authority, _, self.clock, self.start = build(monkeypatch)
        self.agents = {}
        self.fleet = self.broadcasts()

    def operator(self, token):
        if token != OPERATOR:
            raise SwarmError("unauthenticated")
        return INPUTS["operator"]

    def broadcasts(self, store=None, slug=SLUG, authorize=None, operator=None):
        return broadcasts.FleetBroadcasts(
            store or self.store,
            slug,
            authorize or self.authority.authorize,
            operator or self.operator,
            lambda: self.clock[0],
        )

    def token(self, seat, previous=""):
        agent, token = self.start(seat=seat, previous=previous)
        self.agents[seat] = agent
        return token

    def announce(self, draft, operation_id=""):
        return self.fleet.publish_operator(OPERATOR, dict(draft), operation_id)


class Racing:
    """A store whose pipelines run `before` ahead of each commit."""

    def __init__(self, store, before):
        self.store, self.before = store, before

    @property
    def redis(self):
        return self

    def key(self, *parts):
        return self.store.key(*parts)

    def pipeline(self):
        pipe = self.store.redis.pipeline()
        real = pipe.execute

        def execute():
            self.before()
            return real()

        pipe.execute = execute
        return pipe

    def __getattr__(self, name):
        return getattr(self.store.redis, name)


@pytest.fixture
def world(monkeypatch):
    found = World(monkeypatch)
    found.clock[0] = PUBLISH_MS
    return found


def refusal(call, *args):
    with pytest.raises(SwarmError) as error:
        call(*args)
    return str(error.value)


def ids(found):
    return [delivery.broadcast.broadcast_id for delivery in found]


def snapshot(world):
    redis, found = world.store.redis, {}
    for key in sorted(redis.scan_iter(match="*broadcast*")):
        kind = redis.type(key)
        found[key] = (
            redis.hgetall(key) if kind == "hash" else redis.lrange(key, 0, -1) if kind == "list" else redis.get(key)
        )
    return found


@pytest.mark.parametrize("run", ["first", "second"])
def test_a_critical_operator_warning_reaches_local_and_remote_subscribers_once_per_policy(world, run):
    local, remote = world.token(LOCAL), world.token(REMOTE)
    remote_fleet = world.broadcasts(RedisStore(world.store.redis))
    warning = world.announce(WARNING)
    world.announce(EXPIRED)
    world.announce(PERSONAL)
    note = world.fleet.publish(local, dict(NOTE))
    world.clock[0] = CLAIM_MS
    first = world.fleet.claim(local, CHANNELS)
    assert ids(first) == ["agent-note", "fleet-warning"]
    assert [(d.delivered_ms, d.lag_seconds) for d in first] == [(CLAIM_MS, 60.0), (CLAIM_MS, 60.0)]
    assert ids(remote_fleet.claim(remote, CHANNELS)) == ["agent-note", "fleet-warning"]
    world.clock[0] = CLAIM_MS + 5000
    again = world.fleet.claim(local, CHANNELS)
    assert ids(again) == ["fleet-warning"]
    assert (again[0].delivered_ms, again[0].lag_seconds) == (CLAIM_MS, 60.0)
    for token, fleet in ((local, world.fleet), (remote, remote_fleet)):
        assert fleet.acknowledge(token, "fleet-warning", 1) is True
        assert fleet.acknowledge(token, "fleet-warning", 1) is False
        assert fleet.claim(token, CHANNELS) == []
    assert world.fleet.duplicate_acknowledgements(LOCAL) == 1
    assert remote_fleet.duplicate_acknowledgements(REMOTE) == 1
    assert world.fleet.delivery(LOCAL, "fleet-warning") == {
        "revision": 1,
        "delivered_ms": CLAIM_MS,
        "acked": True,
        "execution_id": world.agents[LOCAL].execution_id,
        "generation": 1,
    }
    assert world.fleet.delivery(REMOTE, "agent-note")["acked"] is False
    assert world.fleet.broadcast_delivery_lag_seconds(REMOTE, "fleet-warning") == 60.0
    assert (warning.revision, warning.author, warning.fleet, warning.policy) == (
        1,
        "operator:operator",
        SLUG,
        "until_ack",
    )
    assert (warning.published_ms, warning.expires_ms, warning.project_id, warning.target_role) == (1000, 601000, "", "")
    assert (note.author, note.brain_id, note.project_id, note.policy, note.severity) == (
        f"{LOCAL}/{world.agents[LOCAL].execution_id}",
        "swarm",
        PROJECT,
        "once",
        "info",
    )
    assert world.fleet.current("fleet-warning") == warning
    assert [b.broadcast_id for b in world.fleet.history()] == [
        "fleet-warning",
        "expired-warning",
        "personal-note",
        "agent-note",
    ]


def test_claimed_fleet_revisions_land_in_the_local_cache_in_the_hook_format(world, tmp_path, monkeypatch):
    path = tmp_path / "broadcast.json"
    monkeypatch.setattr(hb, "_broadcast_path", lambda: path)
    hb._save_broadcasts([{"id": "local-1", "message": "Local note", "severity": "info"}])
    local = world.token(LOCAL)
    world.announce(WARNING)
    world.fleet.publish(local, dict(NOTE))
    entries = [broadcasts.local_entry(d) for d in world.fleet.claim(local, CHANNELS)]
    assert hb.cache_fleet_broadcasts(entries) == 2
    cached = hb.list_broadcasts()
    assert [m["id"] for m in cached] == ["local-1", "fixture:agent-note:1", "fixture:fleet-warning:1"]
    warning = {
        "id": "fixture:fleet-warning:1",
        "message": WARNING["message"],
        "severity": "critical",
        "persistent": True,
        "source": "fleet",
        "created_at": "1970-01-01T00:00:01Z",
        "ttl_seconds": 600,
        "expires_at": "1970-01-01T00:10:01Z",
        "delivered_to": [],
        "fleet": {"swarm": SLUG, "broadcast_id": "fleet-warning", "revision": 1},
        "channel": "amygdala",
    }
    assert cached[2] == {**warning, "content_hash": hb._msg_hash(warning)}
    assert cached[1]["persistent"] is False and cached[1]["channel"] == "brain"
    assert hb._message_matches_channel(cached[2], ["amygdala"])
    assert not hb._message_matches_channel(cached[2], ["brain"])


def test_the_local_cache_keeps_only_the_newest_fleet_revision_and_leaves_local_entries(world, tmp_path, monkeypatch):
    path = tmp_path / "broadcast.json"
    monkeypatch.setattr(hb, "_broadcast_path", lambda: path)
    local_note = {"id": "local-1", "message": "Local note", "severity": "info"}
    hb._save_broadcasts([local_note])
    local = world.token(LOCAL)
    world.announce({**WARNING, "channel": ""})
    first = [broadcasts.local_entry(d) for d in world.fleet.claim(local, CHANNELS)]
    assert "channel" not in first[0]
    world.announce({**WARNING, "channel": "", "message": "Merges into dev are open again."})
    second = [broadcasts.local_entry(d) for d in world.fleet.claim(local, CHANNELS)]
    assert hb.cache_fleet_broadcasts(second) == 1
    assert hb.cache_fleet_broadcasts(first) == 0
    assert [m["id"] for m in hb.list_broadcasts()] == ["local-1", "fixture:fleet-warning:2"]
    hb._save_broadcasts([local_note])
    assert hb.cache_fleet_broadcasts(first) == 1
    assert hb.cache_fleet_broadcasts(second + first) == 1
    assert [m["id"] for m in hb.list_broadcasts()] == ["local-1", "fixture:fleet-warning:2"]
    saves = []
    monkeypatch.setattr(hb, "_save_broadcasts", saves.append)
    assert hb.cache_fleet_broadcasts(second) == 0
    assert hb.cache_fleet_broadcasts([]) == 0
    assert saves == []


def test_the_local_cache_keeps_the_newest_entries_within_the_message_cap(world, tmp_path, monkeypatch):
    path = tmp_path / "broadcast.json"
    monkeypatch.setattr(hb, "_broadcast_path", lambda: path)
    monkeypatch.setattr(hb, "BROADCAST_MAX_MESSAGES", 2)
    hb._save_broadcasts([{"id": "local-1", "message": "Local note", "severity": "info"}])
    local = world.token(LOCAL)
    world.announce(WARNING)
    world.fleet.publish(local, dict(NOTE))
    entries = [broadcasts.local_entry(d) for d in world.fleet.claim(local, CHANNELS)]
    assert hb.cache_fleet_broadcasts(entries) == 2
    assert [m["id"] for m in hb.list_broadcasts()] == ["fixture:agent-note:1", "fixture:fleet-warning:1"]


def test_b_a_personal_brain_message_or_another_fleets_warning_never_matches_on_the_channel_name(world):
    local = world.token(LOCAL)
    world.announce(PERSONAL)
    other = world.broadcasts(slug="other")
    other.publish_operator(OPERATOR, dict(OTHER))
    before = snapshot(world)
    assert refusal(other.claim, local, CHANNELS) == "forbidden_scope"
    assert refusal(other.acknowledge, local, "other-warning", 1) == "forbidden_scope"
    assert refusal(other.publish, local, dict(NOTE)) == "forbidden_scope"
    assert snapshot(world) == before
    assert world.fleet.claim(local, ["*"]) == []
    assert snapshot(world) == before
    assert world.fleet.delivery(LOCAL, "personal-note") is None
    assert world.fleet.broadcast_delivery_lag_seconds(LOCAL, "personal-note") is None
    assert world.fleet.current("other-warning") is None


@pytest.mark.parametrize(
    "change",
    [
        {"severity": "critical"},
        {"severity": "nuclear"},
        {"project_id": ""},
        {"project_id": "github.com/the-cloud-clockwork/other"},
        {"brain_id": "personal"},
    ],
)
def test_b_an_agent_grant_stays_inside_its_brain_projects_and_severity(world, change):
    local = world.token(LOCAL)
    before = snapshot(world)
    assert refusal(world.fleet.publish, local, {**NOTE, **change}) == "forbidden_scope"
    assert snapshot(world) == before


def test_b_an_unauthenticated_publisher_is_refused_without_writing(world):
    before = snapshot(world)
    assert refusal(world.fleet.publish, "", dict(NOTE)) == "forbidden_scope"
    assert refusal(world.fleet.claim, "", CHANNELS) == "forbidden_scope"
    assert refusal(world.fleet.publish_operator, "forged", dict(WARNING)) == "unauthenticated"
    assert refusal(world.fleet.publish_operator, "", dict(WARNING)) == "unauthenticated"
    nameless = world.broadcasts(operator=lambda token: "")
    assert refusal(nameless.publish_operator, OPERATOR, dict(WARNING)) == "unauthenticated"
    unnamed = world.broadcasts(operator=lambda token: None)
    assert refusal(unnamed.publish_operator, OPERATOR, dict(WARNING)) == "unauthenticated"
    shapeless = world.broadcasts(authorize=lambda token: {"swarm_id": SLUG})
    assert refusal(shapeless.claim, "token", CHANNELS) == "forbidden_scope"
    brainless = {k: v for k, v in WARNING.items() if k != "brain_id"}
    assert refusal(world.fleet.publish_operator, OPERATOR, brainless) == "invalid_request"
    assert snapshot(world) == before


@pytest.mark.parametrize(
    "draft",
    [
        [],
        {**WARNING, "extra": 1},
        {k: v for k, v in WARNING.items() if k != "message"},
        {k: v for k, v in WARNING.items() if k != "severity"},
        {k: v for k, v in WARNING.items() if k != "ttl_seconds"},
        {**WARNING, "message": "  "},
        {**WARNING, "message": 5},
        {**WARNING, "message": "x" * (broadcasts.MAX_MESSAGE + 1)},
        {**WARNING, "severity": "loud"},
        {**WARNING, "ttl_seconds": 0},
        {**WARNING, "ttl_seconds": broadcasts.MAX_TTL_SECONDS + 1},
        {**WARNING, "ttl_seconds": True},
        {**WARNING, "ttl_seconds": "60"},
        {**WARNING, "policy": "always"},
        {**WARNING, "project_id": 7},
        {**WARNING, "channel": "Bad Channel"},
        {**WARNING, "channel": None},
        {**WARNING, "target_role": "ENG"},
        {**WARNING, "broadcast_id": "bad/id"},
        {**WARNING, "brain_id": "Personal Brain"},
        {**WARNING, "brain_id": ""},
    ],
)
def test_b_malformed_drafts_are_refused_without_writing(world, draft):
    before = snapshot(world)
    assert refusal(world.fleet.publish_operator, OPERATOR, draft) == "invalid_request"
    assert snapshot(world) == before


def test_draft_limits_accept_their_boundaries_and_trim_the_message(world):
    longest = world.announce(
        {**WARNING, "message": " " + "x" * (broadcasts.MAX_MESSAGE - 1), "ttl_seconds": broadcasts.MAX_TTL_SECONDS}
    )
    assert (longest.message, longest.expires_ms) == ("x" * (broadcasts.MAX_MESSAGE - 1), 1000 + 86400 * 1000)
    shortest = world.announce({**WARNING, "broadcast_id": "short", "ttl_seconds": 1, "policy": "once"})
    assert (shortest.expires_ms, shortest.policy) == (2000, "once")
    generated = world.announce({**WARNING, "broadcast_id": ""})
    assert generated.broadcast_id.startswith("bc-") and len(generated.broadcast_id) == 19
    assert world.announce({k: v for k, v in FOLLOWUP.items() if k != "channel"}).channel == ""


def test_operation_and_claim_ids_must_be_names(world):
    local = world.token(LOCAL)
    before = snapshot(world)
    assert refusal(world.fleet.publish_operator, OPERATOR, dict(WARNING), "Bad Id") == "invalid_request"
    assert refusal(world.fleet.claim, local, CHANNELS, "Bad Id") == "invalid_request"
    assert snapshot(world) == before


def test_freeze_disables_distributed_publication_and_keeps_the_event_history(world):
    local = world.token(LOCAL)
    world.announce(WARNING)
    history = world.fleet.history()
    world.fleet.freeze()
    assert world.fleet.frozen() is True
    before = snapshot(world)
    assert refusal(world.fleet.publish_operator, OPERATOR, dict(FOLLOWUP)) == "distribution_disabled"
    assert refusal(world.fleet.publish, local, dict(NOTE)) == "distribution_disabled"
    assert snapshot(world) == before
    assert ids(world.fleet.claim(local, CHANNELS)) == ["fleet-warning"]
    assert world.fleet.history() == history
    world.fleet.thaw()
    assert world.fleet.frozen() is False
    assert world.announce(FOLLOWUP).revision == 1
    assert [b.broadcast_id for b in world.fleet.history()] == ["fleet-warning", "fleet-followup"]


def test_c_a_reconnecting_worker_replays_unexpired_undelivered_messages_without_acknowledged_ones(world):
    old = world.token(LOCAL)
    world.announce(WARNING)
    assert ids(world.fleet.claim(old, CHANNELS)) == ["fleet-warning"]
    assert world.fleet.acknowledge(old, "fleet-warning", 1) is True
    world.clock[0] = 2000
    world.announce(FOLLOWUP)
    world.announce(EXPIRED)
    world.fleet.publish(old, dict(NOTE))
    new = world.token(LOCAL, previous=world.agents[LOCAL].execution_id)
    world.clock[0] = CLAIM_MS
    before = snapshot(world)
    for call, args in ((world.fleet.claim, (old, CHANNELS)), (world.fleet.acknowledge, (old, "fleet-warning", 1))):
        with pytest.raises(GrantRefused) as stale:
            call(*args)
        assert stale.value.error_class == "stale_generation"
    assert snapshot(world) == before
    replay = world.fleet.claim(new, CHANNELS, "claim-1")
    assert ids(replay) == ["agent-note", "fleet-followup"]
    after = snapshot(world)
    assert world.fleet.claim(new, CHANNELS, "claim-1") == replay
    assert snapshot(world) == after
    assert ids(world.fleet.claim(new, CHANNELS, "claim-2")) == ["fleet-followup"]
    assert world.fleet.acknowledge(new, "fleet-followup", 1) is True
    assert world.fleet.claim(new, CHANNELS) == []
    assert world.fleet.delivery(LOCAL, "fleet-followup")["generation"] == 2
    assert world.fleet.delivery(LOCAL, "fleet-warning")["generation"] == 1
    revised = world.announce({**WARNING, "message": "Merges into dev are open again."}, "op-1")
    assert revised.revision == 2
    assert world.announce({**WARNING, "message": "Something else entirely."}, "op-1") == revised
    assert world.announce({**WARNING, "message": "Merges into dev are open again."}) == revised
    assert [b.revision for b in world.fleet.history() if b.broadcast_id == "fleet-warning"] == [1, 2]
    assert world.fleet.broadcast_delivery_lag_seconds(LOCAL, "fleet-warning") is None
    assert ids(world.fleet.claim(new, CHANNELS)) == ["fleet-warning"]
    assert refusal(world.fleet.acknowledge, new, "fleet-warning", 1) == "stale_revision"
    assert refusal(world.fleet.acknowledge, new, "fleet-warning", 3) == "not_delivered"
    assert refusal(world.fleet.acknowledge, new, "never-sent", 1) == "not_delivered"
    assert world.fleet.acknowledge(new, "fleet-warning", 2) is True
    assert world.fleet.broadcast_delivery_lag_seconds(LOCAL, "fleet-warning") == 0.0


def test_a_claim_retry_from_an_older_generation_does_not_reuse_the_newer_answer(world):
    old = world.token(LOCAL)
    world.announce(FOLLOWUP)
    assert ids(world.fleet.claim(old, CHANNELS, "claim-1")) == ["fleet-followup"]
    new = world.token(LOCAL, previous=world.agents[LOCAL].execution_id)
    assert ids(world.fleet.claim(new, CHANNELS, "claim-1")) == ["fleet-followup"]
    assert json.loads(world.store.redis.hget(world.fleet.key("broadcast-claims"), LOCAL))["generation"] == 2


def test_an_older_generation_cannot_claim_or_acknowledge_over_a_newer_delivery(world):
    world.token(LOCAL)
    new = world.token(LOCAL, previous=world.agents[LOCAL].execution_id)
    world.announce(FOLLOWUP)
    assert ids(world.fleet.claim(new, CHANNELS)) == ["fleet-followup"]
    older = world.broadcasts(authorize=lambda token: replace(world.authority.authorize(token), generation=1))
    before = snapshot(world)
    assert refusal(older.claim, new, CHANNELS) == "stale_generation"
    assert refusal(older.acknowledge, new, "fleet-followup", 1) == "stale_generation"
    assert snapshot(world) == before


def test_republishing_unchanged_content_keeps_its_revision_until_it_expires(world):
    first = world.announce(EXPIRED)
    world.clock[0] = first.expires_ms - 1
    assert world.announce(EXPIRED) == first
    world.clock[0] = first.expires_ms
    renewed = world.announce(EXPIRED)
    assert (renewed.revision, renewed.published_ms) == (2, first.expires_ms)
    for change in ({"severity": "alert"}, {"policy": "once"}, {"channel": "brain"}, {"target_role": "eng"}):
        assert world.announce({**EXPIRED, **change}).revision > renewed.revision
        renewed = world.fleet.current("expired-warning")


def test_routing_matches_expiry_project_role_and_channel_together(world):
    local = world.token(LOCAL)
    world.announce({**FOLLOWUP, "broadcast_id": "for-ci", "target_role": "ci"})
    world.announce({**FOLLOWUP, "broadcast_id": "for-eng", "target_role": "eng"})
    world.announce({**FOLLOWUP, "broadcast_id": "other-project", "project_id": "github.com/the-cloud-clockwork/other"})
    world.announce({**FOLLOWUP, "broadcast_id": "this-project", "project_id": PROJECT})
    world.announce({**FOLLOWUP, "broadcast_id": "global", "channel": ""})
    world.announce({**FOLLOWUP, "broadcast_id": "on-brain", "channel": "brain"})
    world.announce({**FOLLOWUP, "broadcast_id": "short", "ttl_seconds": 1})
    assert ids(world.fleet.claim(local, [])) == ["global"]
    assert ids(world.fleet.claim(local, ["brain"])) == ["global", "on-brain"]
    world.clock[0] = PUBLISH_MS + 999
    assert ids(world.fleet.claim(local, ["*"])) == ["for-eng", "global", "on-brain", "short", "this-project"]
    world.clock[0] = PUBLISH_MS + 1000
    assert ids(world.fleet.claim(local, ["*"])) == ["for-eng", "global", "on-brain", "this-project"]


def test_concurrent_claims_by_one_seat_hand_out_a_once_revision_a_single_time(world):
    local = world.token(LOCAL)
    world.fleet.publish(local, dict(NOTE))
    rival = []

    def race():
        if not rival:
            rival.append(world.fleet.claim(local, CHANNELS))

    raced = world.broadcasts(Racing(world.store, race)).claim(local, CHANNELS)
    assert ids(rival[0]) == ["agent-note"]
    assert raced == []


def test_writes_give_up_after_their_attempts(world):
    from redis.exceptions import WatchError

    local = world.token(LOCAL)
    world.announce(WARNING)
    world.fleet.claim(local, CHANNELS)
    world.announce(FOLLOWUP)
    attempts = []

    def fail():
        attempts.append(1)
        raise WatchError

    before = snapshot(world)
    failing = world.broadcasts(Racing(world.store, fail))
    assert refusal(failing.publish_operator, OPERATOR, dict(NOTE, brain_id="swarm")) == "dependency_unavailable"
    assert refusal(failing.claim, local, CHANNELS) == "dependency_unavailable"
    assert refusal(failing.acknowledge, local, "fleet-warning", 1) == "dependency_unavailable"
    assert len(attempts) == 3 * broadcasts.WRITE_ATTEMPTS
    assert snapshot(world) == before


def test_records_round_trip_and_seats_name_their_role(world):
    broadcast = world.announce(WARNING)
    assert broadcasts.decode(broadcasts.encode(broadcast)) == broadcast
    assert [broadcasts.role_of(seat) for seat in ("eng-12@fixture", "master@fixture", "ci-1@x")] == [
        "eng",
        "master",
        "ci",
    ]
    assert broadcast.content() == (
        "swarm",
        "",
        "",
        "amygdala",
        "critical",
        "until_ack",
        WARNING["message"],
    )
