from collections.abc import Iterator
from typing import TYPE_CHECKING

from nats.js.errors import APIError, NotFoundError

from .bus import StateConflict, StateEntry
from .subjects import stream_name, subject

if TYPE_CHECKING:
    from .nats import NatsBus

REVISION = "AgentiHooks-State-Revision"
REVISION_BASE = "agentihooks_revision_base"
IMPORT_PENDING = "agentihooks_import_pending"
LAST_SUBJECT_SEQUENCE = "Nats-Expected-Last-Subject-Sequence"


class ScopedStateStore:
    def __init__(self, root: "NatsBus", swarm: str, bucket: str):
        self._root, self._swarm, self._bucket = root, swarm, bucket
        self._stream = stream_name(swarm, f"STATE_{bucket}")
        if root._config.provision:
            root._loop.run(self._provision())
        info = root._loop.run(root._js.stream_info(self._stream))
        metadata = info.config.metadata or {}
        if metadata.get(IMPORT_PENDING) and not root._config.provision:
            raise RuntimeError("scoped state migration is incomplete")
        self._revision_base = int(metadata.get(REVISION_BASE, "0"))

    async def _provision(self) -> None:
        try:
            await self._root._js.stream_info(self._stream)
        except NotFoundError:
            await self._root._js.add_stream(
                name=self._stream,
                subjects=[f"{subject(self._swarm, 'state', self._bucket)}.>"],
                max_msgs_per_subject=64,
            )

    def _subject(self, key: str) -> str:
        return subject(self._swarm, "state", self._bucket, *key.split("."))

    def _revision(self, message) -> int:
        return int((message.headers or {}).get(REVISION, self._revision_base + message.seq))

    async def _last(self, key: str):
        try:
            return await self._root._js.get_last_msg(self._stream, self._subject(key))
        except NotFoundError:
            return None

    def get(self, key: str) -> StateEntry | None:
        message = self._root._loop.run(self._last(key))
        if message is None:
            return None
        return StateEntry(key=key, value=message.data or b"", revision=self._revision(message))

    def create(self, key: str, value: bytes) -> int:
        try:
            return self._root._loop.run(self._write(key, value, 0))
        except APIError as exc:
            if exc.err_code != 10071:
                raise
            raise StateConflict(f"state key {key!r} exists") from exc

    async def _write(self, key: str, value: bytes, last: int) -> int:
        ack = await self._root._js.publish(
            self._subject(key), value, stream=self._stream, headers={LAST_SUBJECT_SEQUENCE: str(last)}
        )
        return self._revision_base + ack.seq

    def update(self, key: str, value: bytes, revision: int) -> int:
        current = self._root._loop.run(self._last(key))
        if current is None or self._revision(current) != revision:
            raise StateConflict(f"state key {key!r} is not at revision {revision}")
        try:
            return self._root._loop.run(self._write(key, value, current.seq))
        except APIError as exc:
            if exc.err_code != 10071:
                raise
            raise StateConflict(f"state key {key!r} is not at revision {revision}") from exc

    async def _next(self, key: str, sequence: int):
        try:
            return await self._root._js.get_msg(self._stream, seq=sequence, subject=self._subject(key), next=True)
        except NotFoundError:
            return None

    def watch(self, key: str, from_revision: int = 1) -> Iterator[StateEntry]:
        sequence = max(from_revision - self._revision_base, 1)
        while (message := self._root._loop.run(self._next(key, sequence))) is not None:
            sequence = message.seq + 1
            revision = self._revision(message)
            if revision >= from_revision:
                yield StateEntry(key=key, value=message.data or b"", revision=revision)
