import pytest

from scripts.hive import redis_acl

pytestmark = pytest.mark.unit


@pytest.fixture
def client():
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


def _error(kind: str, text: str) -> BaseException:
    from redis import exceptions

    return getattr(exceptions, kind)(text)


class Recorder:
    def __init__(self, error: BaseException | None = None):
        self.calls = []
        self.error = error

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if self.error is not None:
            raise self.error

    def acl_setuser(self, *args, **kwargs):
        self._record("acl_setuser", *args, **kwargs)

    def acl_deluser(self, *args, **kwargs):
        self._record("acl_deluser", *args, **kwargs)

    def acl_save(self, *args, **kwargs):
        self._record("acl_save", *args, **kwargs)


def test_set_user_resets_the_user_with_its_hashed_password_and_grants():
    client = Recorder()
    redis_acl.set_user(client, "hive-m1", "digest", ("+@all",), ("~k",), ("&c",))
    assert client.calls == [
        (
            "acl_setuser",
            ("hive-m1",),
            {
                "enabled": True,
                "reset": True,
                "hashed_passwords": ["+digest"],
                "categories": ("+@all",),
                "keys": ("~k",),
                "channels": ("&c",),
            },
        )
    ]


def test_regrant_resets_only_keys_and_channels():
    client = Recorder()
    redis_acl.regrant(client, "hive-m1", ("+@all",), ("~k",), ("&c",))
    assert client.calls == [
        (
            "acl_setuser",
            ("hive-m1",),
            {
                "enabled": True,
                "reset_keys": True,
                "reset_channels": True,
                "categories": ("+@all",),
                "keys": ("~k",),
                "channels": ("&c",),
            },
        )
    ]


def test_a_real_user_is_created_and_dropped(client):
    redis_acl.set_user(client, "hive-m1", "a" * 64, ("+@all",), ("~k",), ("&c",))
    assert client.acl_getuser("hive-m1")["flags"] == ["on"]
    redis_acl.drop(client, "hive-m1")
    assert client.acl_users() == ["default"]


@pytest.mark.parametrize(
    "call",
    [
        lambda client: redis_acl.set_user(client, "u", "d", (), (), ()),
        lambda client: redis_acl.regrant(client, "u", (), (), ()),
        lambda client: redis_acl.drop(client, "u"),
        redis_acl.save,
    ],
)
def test_a_redis_refusal_is_raised_as_refused_with_its_cause(call):
    error = _error("ConnectionError", "down")
    with pytest.raises(redis_acl.Refused, match="^down$") as refused:
        call(Recorder(error))
    assert refused.value.__cause__ is error


def test_save_writes_the_acl_file():
    client = Recorder()
    redis_acl.save(client)
    assert client.calls == [("acl_save", (), {})]


def test_save_passes_a_redis_without_an_acl_file():
    redis_acl.save(Recorder(_error("ResponseError", f"ERR This Redis instance is {redis_acl.NO_ACL_FILE}")))


@pytest.mark.parametrize(
    ("kind", "text"),
    [("ResponseError", "ERR other"), ("ConnectionError", redis_acl.NO_ACL_FILE)],
)
def test_save_raises_any_other_refusal(kind, text):
    with pytest.raises(redis_acl.Refused):
        redis_acl.save(Recorder(_error(kind, text)))


def test_errors_outside_redis_pass_through():
    with pytest.raises(ValueError):
        redis_acl.drop(Recorder(ValueError("bad")), "u")
