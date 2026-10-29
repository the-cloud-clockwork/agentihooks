from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit


def test_members_refuses_a_pipeline_reply_shorter_than_the_index():
    from scripts.hive import scope

    redis = MagicMock()
    redis.smembers.return_value = {"m1", "m2"}
    redis.pipeline.return_value.__enter__.return_value.execute.return_value = [1]

    with pytest.raises(ValueError, match="zip"):
        scope.members(redis)
