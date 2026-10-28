"""Redis ACL users for hive members, kept while members still talk Redis."""

from collections.abc import Callable

# A Redis started without --aclfile keeps users in memory only; there is nothing to save.
NO_ACL_FILE = "not configured to use an ACL file"


class Refused(Exception):
    pass


def _call(action: Callable[[], object]) -> object:
    from redis.exceptions import RedisError

    try:
        return action()
    except RedisError as exc:
        raise Refused(str(exc)) from exc


def set_user(client, user: str, password_digest: str, categories, keys, channels) -> None:
    _call(
        lambda: client.acl_setuser(
            user,
            enabled=True,
            reset=True,
            hashed_passwords=[f"+{password_digest}"],
            categories=categories,
            keys=keys,
            channels=channels,
        )
    )


def regrant(client, user: str, categories, keys, channels) -> None:
    _call(
        lambda: client.acl_setuser(
            user,
            enabled=True,
            reset_keys=True,
            reset_channels=True,
            categories=categories,
            keys=keys,
            channels=channels,
        )
    )


def drop(client, user: str) -> None:
    _call(lambda: client.acl_deluser(user))


def save(client) -> None:
    from redis.exceptions import ResponseError

    try:
        _call(client.acl_save)
    except Refused as exc:
        if not isinstance(exc.__cause__, ResponseError) or NO_ACL_FILE not in str(exc):
            raise
