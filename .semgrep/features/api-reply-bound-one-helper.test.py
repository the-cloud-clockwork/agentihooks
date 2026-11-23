from scripts.swarm_ledger.api import resources
from scripts.swarm_ledger.api.errors import APIError
from scripts.swarm_ledger.api.resources import MAX_REPLY, reply_size


def by_hand(reply):
    # ruleid: feature-api-reply-bound-one-helper
    if resources.reply_size(reply) > resources.MAX_REPLY:
        raise APIError(413, "resource_too_large", "The read passes the reply limit: read one item")
    return reply


def by_name(reply):
    # ruleid: feature-api-reply-bound-one-helper
    return reply_size(reply) > MAX_REPLY


def through_the_helper(reply):
    # ok: feature-api-reply-bound-one-helper
    return resources.bounded(reply, "The read passes the reply limit: read one item")


def through_the_default(reply):
    # ok: feature-api-reply-bound-one-helper
    return resources.bounded(reply)
