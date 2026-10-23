import hashlib
import json
from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING

from nats.errors import TimeoutError as FetchTimeout
from nats.js.errors import APIError, NotFoundError

from .config import QueuePolicy
from .dead import DEAD_STREAM
from .envelope import Envelope
from .nats import ATTEMPT_BASE, MSG_ID, NatsWorkQueue, _attempt
from .nats_state import IMPORT_PENDING, LAST_SUBJECT_SEQUENCE, REVISION, REVISION_BASE, ScopedStateStore
from .scope import queue_lane
from .subjects import subject

if TYPE_CHECKING:
    from .nats import NatsBus

IMPORT_PLAN = "agentihooks_import_plan"


def require_migration(root: "NatsBus") -> None:
    if not root._config.provision:
        raise PermissionError("migration requires privileged provisioning")
    if not root._config.scoped:
        raise ValueError("scoped migration is not enabled")


async def _metadata(root: "NatsBus", stream: str, changes: dict) -> None:
    info = await root._js.stream_info(stream)
    await root._js.update_stream(config=replace(info.config, metadata=changes))


def _begin_state(root: "NatsBus", target: "ScopedStateStore", plan: str, highwater: int) -> dict | None:
    info = root._loop.run(root._js.stream_info(target._stream))
    metadata = dict(info.config.metadata or {})
    known = metadata.get(IMPORT_PLAN)
    if known is not None and known != plan:
        raise ValueError("state migration ownership or source revisions changed")
    if known is None and info.state.messages:
        raise ValueError("scoped state already contains live writes")
    target._revision_base = int(metadata.get(REVISION_BASE, highwater))
    if known is not None and not metadata.get(IMPORT_PENDING):
        return None
    metadata.update({IMPORT_PLAN: plan, REVISION_BASE: str(target._revision_base), IMPORT_PENDING: "state"})
    root._loop.run(_metadata(root, target._stream, metadata))
    return metadata


def _copy_state(root: "NatsBus", target: "ScopedStateStore", records: list, plan: str) -> None:
    imported = {(entry.key, entry.revision) for key in {row.key for row in records} for entry in target.watch(key)}
    for entry in records:
        if (entry.key, entry.revision) in imported:
            continue
        current = root._loop.run(target._last(entry.key))
        root._loop.run(
            root._js.publish(
                target._subject(entry.key),
                entry.value,
                stream=target._stream,
                headers={
                    REVISION: str(entry.revision),
                    LAST_SUBJECT_SEQUENCE: str(current.seq if current else 0),
                    MSG_ID: f"state-import:{plan}:{entry.revision}",
                },
            )
        )


def migrate_state(root: "NatsBus", swarm: str, bucket: str, keys: Sequence[str]) -> None:
    """The caller quiesces legacy writers and supplies authoritative key ownership."""
    require_migration(root)
    source = root.state(bucket)
    records = sorted((entry for key in set(keys) for entry in source.watch(key)), key=lambda entry: entry.revision)
    expected = {key: max((entry.revision for entry in records if entry.key == key), default=None) for key in keys}
    plan = hashlib.sha256(
        json.dumps(
            {"keys": sorted(set(keys)), "revisions": [(entry.key, entry.revision) for entry in records]}
        ).encode()
    ).hexdigest()
    highwater = root._loop.run(root._js.stream_info(f"KV_{bucket}")).state.last_seq
    target = ScopedStateStore(root, swarm, bucket, importing=True)
    metadata = _begin_state(root, target, plan, highwater)
    if metadata is None:
        return
    _copy_state(root, target, records, plan)
    for key, revision in expected.items():
        current = source.get(key)
        if (current.revision if current else None) != revision:
            raise RuntimeError("legacy state changed during migration")
    metadata.pop(IMPORT_PENDING)
    root._loop.run(_metadata(root, target._stream, metadata))


async def _copy_work(root: "NatsBus", target: NatsWorkQueue, message) -> None:
    sequence = message.metadata.sequence.stream
    destination = f"{target._filter.removesuffix('.>')}.migration.{sequence}"
    try:
        existing = await root._js.get_last_msg(target._stream, destination)
    except NotFoundError:
        try:
            await root._js.publish(
                destination,
                message.data,
                stream=target._stream,
                headers={
                    MSG_ID: f"migration:{target._stream}:{sequence}",
                    LAST_SUBJECT_SEQUENCE: "0",
                    ATTEMPT_BASE: str(_attempt(message) - 1),
                },
            )
        except APIError:
            existing = await root._js.get_last_msg(target._stream, destination)
            if existing.data != message.data:
                raise
    else:
        if existing.data != message.data:
            raise ValueError("retained migration copy differs from its source")
    await message.ack_sync()


async def _drain_work(
    root: "NatsBus", source: NatsWorkQueue, target: NatsWorkQueue, policy: QueuePolicy, swarm: str
) -> None:
    info = await root._js.stream_info(source._stream)
    highwater = info.state.last_seq
    while info.state.messages:
        try:
            messages = await source._consumer._subscription.fetch(
                100, timeout=(policy.ack_wait_ms + sum(policy.backoff_ms) + 1000) / 1000
            )
        except FetchTimeout:
            raise RuntimeError("legacy queue still has undelivered work") from None
        for message in messages:
            if message.metadata.sequence.stream > highwater or Envelope.decode(message.data).swarm != swarm:
                raise ValueError("legacy queue changed or contains work for another swarm")
            await _copy_work(root, target, message)
        info = await root._js.stream_info(source._stream)


def migrate_queue(root: "NatsBus", swarm: str, name: str, policy: QueuePolicy) -> None:
    """The caller quiesces legacy queue producers and consumers before handover."""
    require_migration(root)
    lane = queue_lane(swarm, name)
    source = NatsWorkQueue(
        root._loop, root._js, f"swarm.{swarm}.ready.{lane}", policy, replace(root._config, scoped=False)
    )
    target = root.for_swarm(swarm).queue(lane, policy)
    info = root._loop.run(root._js.stream_info(target._stream))
    metadata = dict(info.config.metadata or {})
    metadata[IMPORT_PENDING] = "queue"
    root._loop.run(_metadata(root, target._stream, metadata))
    root._loop.run(_drain_work(root, source, target, policy, swarm))
    metadata.pop(IMPORT_PENDING)
    root._loop.run(_metadata(root, target._stream, metadata))


async def _copy_dead(root: "NatsBus", swarm: str, target: str) -> None:
    info = await root._js.stream_info(DEAD_STREAM)
    sequence = info.state.first_seq
    while sequence <= info.state.last_seq:
        try:
            message = await root._js.get_msg(DEAD_STREAM, seq=sequence)
        except NotFoundError:
            sequence += 1
            continue
        event = Envelope.decode(message.data)
        if event.swarm == swarm:
            destination = subject(swarm, "dead", hashlib.sha256(event.id.encode()).hexdigest())
            try:
                await root._js.publish(
                    destination, message.data, stream=target, headers={MSG_ID: event.id, LAST_SUBJECT_SEQUENCE: "0"}
                )
            except APIError:
                retained = await root._js.get_last_msg(target, destination)
                if retained.data != message.data:
                    raise
        sequence += 1


def migrate_dead(root: "NatsBus", swarm: str) -> None:
    require_migration(root)
    view = root.for_swarm(swarm)
    root._loop.run(_copy_dead(root, swarm, view.dead._log._stream))
