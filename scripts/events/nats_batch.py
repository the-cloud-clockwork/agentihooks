"""Reads the last message of many JetStream subjects with one API request, through a short lived consumer."""

import json
from dataclasses import dataclass

from nats.js import JetStreamContext
from nats.js.errors import APIError

from . import nats_payload

INACTIVE_NS = 5_000_000_000
TIMEOUT_S = 5.0


@dataclass(frozen=True)
class Last:
    data: bytes
    headers: dict[str, str]
    seq: int


async def last_per_subject(js: JetStreamContext, stream: str, subjects: list[str]) -> dict[str, Last]:
    """Maps each subject that holds a message to its last one; one consumer create is the only JetStream request."""
    wanted = list(dict.fromkeys(subjects))
    if not wanted:
        return {}
    nc = js._nc
    sub = await nc.subscribe(nc.new_inbox())
    try:
        config = {
            "deliver_policy": "last_per_subject",
            "ack_policy": "none",
            "filter_subjects": wanted,
            "deliver_subject": sub.subject,
            "inactive_threshold": INACTIVE_NS,
            "mem_storage": True,
            "num_replicas": 1,
        }
        body = json.dumps({"stream_name": stream, "config": config}).encode()
        create = f"{js._prefix}.CONSUMER.CREATE.{stream}"
        nats_payload.validate(nc, create, body)
        reply = await nc.request(create, body, timeout=TIMEOUT_S)
        info = json.loads(reply.data)
        if "error" in info:
            raise APIError.from_error(info["error"])
        found: dict[str, Last] = {}
        for _ in range(info["num_pending"] + info["delivered"]["consumer_seq"]):
            msg = await sub.next_msg(timeout=TIMEOUT_S)
            found[msg.subject] = Last(msg.data or b"", dict(msg.headers or {}), msg.metadata.sequence.stream)
        return found
    finally:
        await sub.unsubscribe()
