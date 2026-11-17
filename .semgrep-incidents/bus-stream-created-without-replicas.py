from nats.js.api import StreamConfig

from scripts.events import nats_replicas


async def unreplicated(js: object) -> None:
    # ruleid: bus-stream-created-without-replicas
    await js.add_stream(name="S", subjects=["s"])
    # ruleid: bus-stream-created-without-replicas
    await js.create_key_value(bucket="b", history=64)


async def replicated(js: object, replicas: int) -> None:
    # ok: bus-stream-created-without-replicas
    await js.add_stream(name="S", subjects=["s"], num_replicas=replicas)
    # ok: bus-stream-created-without-replicas
    await js.create_key_value(bucket="b", replicas=replicas, history=64)
    # ok: bus-stream-created-without-replicas
    await nats_replicas.create(js, replicas, "S", StreamConfig(subjects=["s"]))
    # ok: bus-stream-created-without-replicas
    await js.add_stream(StreamConfig(subjects=["s"]), name="S", num_replicas=replicas)
