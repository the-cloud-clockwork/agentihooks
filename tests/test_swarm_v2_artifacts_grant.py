import pytest

from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_v2.artifacts import base, local
from scripts.swarm_v2.auth_context import GrantRefused, LaunchAuthority, LaunchKey

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

SLUG = "fixture"
PROJECT = "github.com/the-cloud-clockwork/agentihooks"
NOW = 1_791_600_000
DATA = b"artifact body"


class Grants:
    def __init__(self):
        import fakeredis

        self.store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
        self.store.create(SwarmConfig(SLUG, "agentihooks", 1, 0))
        self.authority = self.signer(b"k" * 32)
        record = AgentRecord(self.store.next_name(SLUG, "eng"), "eng", "t1", seat="eng-1", started_at=123)
        self.execution = self.store.start_execution(SLUG, record, "")

    def signer(self, secret: bytes) -> LaunchAuthority:
        return LaunchAuthority(self.store, LaunchKey("fixture-key", secret), "controller", "claims", lambda: NOW)

    def issue(self, authority: LaunchAuthority | None = None) -> str:
        authority = authority or self.authority
        return authority.issue(SLUG, self.execution.execution_id, project_ids=[PROJECT], brain_id="b", account="a")

    def register(self, token: str) -> None:
        body = {"execution_id": self.execution.execution_id, "generation": self.execution.generation}
        self.authority.register(SLUG, token, body)

    def bound(self, token: str):
        return self.authority.bound(SLUG, token)


@pytest.fixture
def grants():
    return Grants()


def test_store_takes_the_scope_of_a_registered_launch_grant(grants, tmp_path):
    token = grants.issue()
    grants.register(token)
    store = base.ArtifactStore(local.LocalBackend(tmp_path), grants.bound, token)
    execution = grants.execution
    assert store.scope == base.Scope(SLUG, "t1", execution.execution_id, execution.generation)
    ref = store.put("a1", DATA)
    assert store.backend.keys("") == ["fixture/t1/artifacts/a1.json", f"fixture/t1/objects/{ref.sha256}"]


def test_hand_built_scope_is_refused_by_the_store(grants, tmp_path):
    execution = grants.execution
    scope = base.Scope(SLUG, "t1", execution.execution_id, execution.generation)
    with pytest.raises(base.ArtifactError) as raised:
        base.ArtifactStore(local.LocalBackend(tmp_path), grants.bound, scope)
    assert str(raised.value) == base.UNGRANTED
    assert list(tmp_path.iterdir()) == []


def test_unregistered_grant_builds_no_store(grants, tmp_path):
    with pytest.raises(GrantRefused) as raised:
        base.ArtifactStore(local.LocalBackend(tmp_path), grants.bound, grants.issue())
    assert str(raised.value) == "launch grant is not registered"


def test_grant_signed_with_another_key_builds_no_store(grants, tmp_path):
    forged = grants.issue(grants.signer(b"z" * 32))
    with pytest.raises(GrantRefused) as raised:
        base.ArtifactStore(local.LocalBackend(tmp_path), grants.bound, forged)
    assert str(raised.value) == "launch grant signature is invalid"


def test_revoked_grant_builds_no_store(grants, tmp_path):
    token = grants.issue()
    grants.register(token)
    assert grants.authority.revoke(SLUG, grants.execution.execution_id) is True
    with pytest.raises(GrantRefused) as raised:
        base.ArtifactStore(local.LocalBackend(tmp_path), grants.bound, token)
    assert str(raised.value) == "launch grant was revoked"
