import asyncio
import hashlib
import threading
from collections.abc import Coroutine, Iterator

import nats as nats_client
from nats.aio.msg import Msg
from nats.errors import TimeoutError as FetchTimeout
from nats.js import JetStreamContext, api
from nats.js.errors import KeyNotFoundError, KeyWrongLastSequenceError, NotFoundError

from .bus import StateConflict, StateEntry
from .config import BusConfig, QueuePolicy
from .envelope import Envelope
from .subjects import PREFIX

DEFAULT_URL = "nats://127.0.0.1:4222"
EVENT_STREAM = "AGENTIHOOKS_EVENTS"
QUEUE_SUBJECT = "agentihooks.queue"
MSG_ID = "Nats-Msg-Id"
KV_HISTORY = 64
# nats-py refuses a zero fetch timeout, and the server needs a moment to answer one that is nearly zero.
MIN_FETCH_MS = 10
LOOP_START_S = 5
LOOP_STOP_S = 5


def queue_stream(name: str) -> str:
    return f"AGENTIHOOKS_QUEUE_{hashlib.sha256(name.encode()).hexdigest()}"


def _seconds(ms: int) -> float:
    return ms / 1000


class _Loop:
    def __init__(self):
        self.closed = False
        self._loop = asyncio.new_event_loop()
        started = threading.Event()
        self._loop.call_soon(started.set)
        self.thread = threading.Thread(target=self._loop.run_forever, name="agentihooks-nats-bus", daemon=True)
        self.thread.start()
        if not started.wait(LOOP_START_S):
            self.close()
            raise RuntimeError("bus loop did not start")

    def run(self, coro: Coroutine):
        if self.closed:
            coro.close()
            raise RuntimeError("bus is closed")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._loop.call_soon_threadsafe(self._loop.stop)
        self.thread.join(LOOP_STOP_S)
        if self.thread.is_alive():
            raise RuntimeError("bus loop did not stop")
        self._loop.close()


def _consumer_config(durable: str, subject: str, policy: QueuePolicy) -> api.ConsumerConfig:
    backoff = [_seconds(ms) for ms in policy.backoff_ms[: policy.max_deliver - 1]]
    return api.ConsumerConfig(
        durable_name=durable,
        filter_subject=subject,
        max_deliver=policy.max_deliver,
        ack_wait=_seconds(policy.ack_wait_ms),
        backoff=backoff or None,
    )


async def _pull(js, stream: str, durable: str, subject: str, policy: QueuePolicy):
    try:
        info = await js.consumer_info(stream, durable)
    except NotFoundError:
        await js.add_consumer(stream, config=_consumer_config(durable, subject, policy))
    else:
        if info.config.filter_subject != subject:
            raise ValueError(f"durable consumer {durable!r} filters {info.config.filter_subject!r}, not {subject!r}")
    return await js.pull_subscribe_bind(durable=durable, stream=stream)


class NatsDelivery:
    def __init__(self, loop: _Loop, msg: Msg):
        self._loop = loop
        self._msg = msg
        self.event = Envelope.decode(msg.data)
        self.attempt = msg.metadata.num_delivered

    def ack(self) -> None:
        self._loop.run(self._msg.ack())

    def nak(self, delay_ms: int = 0) -> None:
        self._loop.run(self._msg.nak(delay=_seconds(delay_ms)))

    def term(self) -> None:
        self._loop.run(self._msg.term())


class NatsConsumer:
    def __init__(self, loop: _Loop, subscription: JetStreamContext.PullSubscription):
        self._loop = loop
        self._subscription = subscription

    def fetch(self, batch: int, timeout_ms: int) -> list[NatsDelivery]:
        return [NatsDelivery(self._loop, msg) for msg in self._loop.run(self._fetch(batch, timeout_ms))]

    async def _fetch(self, batch: int, timeout_ms: int) -> list:
        try:
            return await self._subscription.fetch(batch, timeout=_seconds(max(timeout_ms, MIN_FETCH_MS)))
        except FetchTimeout:
            return []


async def _publish(js, subject: str, stream: str, event: Envelope) -> bool:
    ack = await js.publish(subject, event.encode(), stream=stream, headers={MSG_ID: event.id})
    return not ack.duplicate


class NatsEventLog:
    def __init__(self, loop: _Loop, js: JetStreamContext, policy: QueuePolicy):
        self._loop = loop
        self._js = js
        self._policy = policy

    def publish(self, subject: str, event: Envelope) -> bool:
        return self._loop.run(_publish(self._js, subject, EVENT_STREAM, event))

    def consumer(self, durable: str, subject: str) -> NatsConsumer:
        return NatsConsumer(self._loop, self._loop.run(_pull(self._js, EVENT_STREAM, durable, subject, self._policy)))


class NatsWorkQueue:
    def __init__(self, loop: _Loop, js: JetStreamContext, name: str, policy: QueuePolicy, dedupe_window_ms: int):
        self.policy = policy
        self._loop = loop
        self._js = js
        self._stream = queue_stream(name)
        self._subject = f"{QUEUE_SUBJECT}.{name}"
        loop.run(
            js.add_stream(
                name=self._stream,
                subjects=[self._subject],
                retention=api.RetentionPolicy.WORK_QUEUE,
                duplicate_window=_seconds(dedupe_window_ms),
            )
        )
        self._consumer = NatsConsumer(loop, loop.run(_pull(js, self._stream, name, self._subject, policy)))

    def offer(self, event: Envelope) -> bool:
        return self._loop.run(_publish(self._js, self._subject, self._stream, event))

    def take(self, timeout_ms: int) -> NatsDelivery | None:
        deliveries = self._consumer.fetch(1, timeout_ms)
        return deliveries[0] if deliveries else None


class NatsStateStore:
    def __init__(self, loop: _Loop, js: JetStreamContext, bucket: str):
        self._loop = loop
        self._js = js
        self._bucket = bucket
        self._kv = loop.run(js.create_key_value(bucket=bucket, history=KV_HISTORY))

    def get(self, key: str) -> StateEntry | None:
        try:
            entry = self._loop.run(self._kv.get(key))
        except KeyNotFoundError:
            return None
        return StateEntry(key=key, value=entry.value, revision=entry.revision)

    def create(self, key: str, value: bytes) -> int:
        try:
            return self._loop.run(self._kv.create(key, value))
        except KeyWrongLastSequenceError as err:
            raise StateConflict(f"state key {key!r} exists") from err

    def update(self, key: str, value: bytes, revision: int) -> int:
        try:
            return self._loop.run(self._kv.update(key, value, last=revision))
        except KeyWrongLastSequenceError as err:
            raise StateConflict(f"state key {key!r} is not at revision {revision}") from err

    def watch(self, key: str, from_revision: int = 1) -> Iterator[StateEntry]:
        subject = f"$KV.{self._bucket}.{key}"
        sequence = max(from_revision, 1)
        while (msg := self._loop.run(self._next(subject, sequence))) is not None:
            sequence = msg.seq + 1
            yield StateEntry(key=key, value=msg.data or b"", revision=msg.seq)

    async def _next(self, subject: str, sequence: int):
        try:
            return await self._js.get_msg(f"KV_{self._bucket}", seq=sequence, subject=subject, next=True)
        except NotFoundError:
            return None


class NatsBus:
    def __init__(self, config: BusConfig, url: str):
        self._config = config
        self._loop = _Loop()
        self._lock = threading.Lock()
        self._queues: dict[str, NatsWorkQueue] = {}
        self._buckets: dict[str, NatsStateStore] = {}
        try:
            self._nc = self._loop.run(nats_client.connect(url, allow_reconnect=False))
            self._js = self._nc.jetstream()
            self._loop.run(
                self._js.add_stream(
                    name=EVENT_STREAM,
                    subjects=[f"{PREFIX}.>"],
                    duplicate_window=_seconds(config.dedupe_window_ms),
                )
            )
        except BaseException:
            self._loop.close()
            raise
        self.events = NatsEventLog(self._loop, self._js, config.queue)

    def queue(self, name: str, policy: QueuePolicy) -> NatsWorkQueue:
        with self._lock:
            queue = self._queues.get(name)
            if queue is None:
                queue = NatsWorkQueue(self._loop, self._js, name, policy, self._config.dedupe_window_ms)
                self._queues[name] = queue
            elif queue.policy != policy:
                raise ValueError(f"work queue {name!r} already has another policy")
            return queue

    def state(self, bucket: str) -> NatsStateStore:
        with self._lock:
            store = self._buckets.get(bucket)
            if store is None:
                store = self._buckets[bucket] = NatsStateStore(self._loop, self._js, bucket)
            return store

    def close(self) -> None:
        with self._lock:
            try:
                if not self._loop.closed:
                    self._loop.run(self._nc.close())
            finally:
                self._loop.close()


def open_bus(config: BusConfig) -> NatsBus:
    return NatsBus(config, config.url or DEFAULT_URL)
