from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit


def test_members_refuses_an_unreadable_store_reply():
    from scripts.hive import scope

    redis = MagicMock()
    redis.hget.return_value = "not json"

    with pytest.raises(ValueError):
        scope.members(redis)
