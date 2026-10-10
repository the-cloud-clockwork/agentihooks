import pytest

from scripts.swarm import store as swarm_store
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm_ledger import ledger_freezes
from tests.swarm_ledger import legacy_page
from tests.swarm_ledger.test_ledger_authority import SLUG, core, ledger, new_ledger, operation, pinned
from tests.swarm_ledger.test_ledger_authority import live as _authority_live

pytestmark = pytest.mark.xdist_group("fakeredis")

authority_live = _authority_live
DISPATCHER, ENGINEER = "dispatcher@323133-0002", "engineer@323133-0256"


@pytest.fixture
def swarm(authority_live, monkeypatch):
    import fakeredis

    content = {"title": "Freeze transport", "phases": [{"title": "Proof"}]}
    html, document = core.paths(SLUG)
    html.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, authority_live["port"]))
    document.unlink(missing_ok=True)
    core.sync(SLUG)
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(SLUG, "/repo", 1, 0))
    monkeypatch.setattr(swarm_store, "connect", lambda: store)
    return store


def freezes():
    return [(row["verb"], row["target"], row["by"]) for row in core.sync(SLUG)[0]["freezes"]]


def test_the_live_dispatcher_credential_freezes_focuses_and_unfreezes_at_full_autonomy(swarm):
    swarm.update(SLUG, autonomy="full")
    assert ledger_freezes.autonomy(SLUG) == "full"
    with pinned(DISPATCHER):
        frozen = operation("freeze_set", by=DISPATCHER, verb="freeze", target="lane:ci")
        focused = operation("freeze_set", by=DISPATCHER, verb="focus", target="kind:code")
        assert ledger.request(SLUG, [frozen, focused])["rejected"] == []
        assert freezes() == [("freeze", "lane:ci", DISPATCHER), ("focus", "kind:code", DISPATCHER)]
        cleared = operation("freeze_clear", by=DISPATCHER, target="lane:ci")
        assert ledger.request(SLUG, [cleared])["rejected"] == []
    assert freezes() == [("focus", "kind:code", DISPATCHER)]


def refused_verbs(name):
    assert core.sync(SLUG, ops=[operation("freeze_set", verb="freeze", target="lane:eng")])[1] == []
    with pinned(name):
        ops = [
            operation("freeze_set", by=name, verb="freeze", target="lane:ci"),
            operation("freeze_set", by=name, verb="focus", target="kind:code"),
            operation("freeze_clear", by=name, target="lane:eng"),
        ]
        assert ledger.request(SLUG, ops)["rejected"] == [op["id"] for op in ops]
    assert freezes() == [("freeze", "lane:eng", "operator")]


@pytest.mark.parametrize("autonomy", ["manual", "assist", "delegate"])
def test_the_live_dispatcher_credential_below_full_autonomy_is_refused(swarm, autonomy):
    swarm.update(SLUG, autonomy=autonomy)
    refused_verbs(DISPATCHER)


def test_an_engineer_credential_at_full_autonomy_is_refused(swarm):
    swarm.update(SLUG, autonomy="full")
    refused_verbs(ENGINEER)
