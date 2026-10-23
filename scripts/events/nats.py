import asyncio
import base64
import hashlib
import re
import threading
from collections.abc import Callable, Coroutine, Iterator

import nats as nats_client
from nats.aio.msg import Msg
from nats.errors import NoRespondersError
from nats.errors import TimeoutError as FetchTimeout
from nats.js import JetStreamContext, api
from nats.js.errors import APIError, KeyNotFoundError, KeyWrongLastSequenceError, NotFoundError

from . import nkey
from .bus import Bus, EventLog, StateConflict, StateEntry, WorkQueue
from .config import SHARED_SLOT_BUCKETS, BusConfig, QueuePolicy
from .dead import DEAD_STREAM, DEAD_SUBJECT, dead_letter
from .envelope import Envelope
from .nats_legacy import legacy_filters, legacy_target, provision_events
from .scope import RoutedLog, queue_lane, require_owner
from .subjects import PREFIX, inbox, stream_name
from .subjects import subject as event_subject

DEFAULT_URL = "nats://127.0.0.1:4222"
EVENT_STREAM = "AGENTIHOOKS_EVENTS"
QUEUE_SUBJECT = "agentihooks.queue"
MSG_ID = "Nats-Msg-Id"
KV_HISTORY = 64
# nats-py refuses a zero fetch timeout, and the server needs a moment to answer one that is nearly zero.
MIN_FETCH_MS = 10
LOOP_START_S = 5
LOOP_STOP_S = 5
ATTEMPT_BASE = "AgentiHooks-Delivery-Base"


def queue_stream(name: str) -> str:
    suffix = name if re.fullmatch("[A-Za-z0-9_-]{1,64}", name) else "h" + hashlib.sha256(name.encode()).hexdigest()
    return f"AGENTIHOOKS_QUEUE_{suffix}"


def _seconds(ms: int) -> float:
    return ms / 1000


def auth_options(credentials: str | None) -> dict:
    if credentials is None:
        return {}
    jwt, user = nkey.parse_creds(credentials)
    return {
        "user_jwt_cb": lambda: jwt.encode(),
        "signature_cb": lambda nonce: base64.b64encode(user.sign(nonce.encode())),
        "inbox_prefix": inbox(user.public),
    }


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
        max_deliver=-1,
        ack_wait=_seconds(policy.ack_wait_ms),
        backoff=backoff or None,
        inactive_threshold=None if policy.inactive_ms is None else _seconds(policy.inactive_ms),
    )


async def _pull(js, stream: str, durable: str, subject: str, policy: QueuePolicy, provision: bool):
    wanted = legacy_filters(subject) if stream == EVENT_STREAM else [subject]
    try:
        info = await js.consumer_info(stream, durable)
    except NotFoundError:
        if not provision:
            raise
    else:
        found = info.config.filter_subjects or [info.config.filter_subject]
        if found != wanted and not (stream == EVENT_STREAM and found == [subject]):
            raise ValueError(f"durable consumer {durable!r} filters {found[0]!r}, not {subject!r}")
    if provision:
        config = _consumer_config(durable, subject, policy)
        if len(wanted) > 1:
            config.filter_subject, config.filter_subjects = None, wanted
        if stream.endswith("_DEAD"):
            config.max_deliver = policy.max_deliver
        await js.add_consumer(stream, config=config)
    return await js.pull_subscribe_bind(durable=durable, stream=stream)


def _attempt(message: Msg) -> int:
    return message.metadata.num_delivered + int((message.headers or {}).get(ATTEMPT_BASE, "0"))


class NatsDelivery:
    def __init__(self, loop: _Loop, msg: Msg):
        self._loop = loop
        self._msg = msg
        self.event = Envelope.decode(msg.data)
        self.attempt = _attempt(msg)

    def ack(self) -> None:
        self._loop.run(self._msg.ack())

    def nak(self, delay_ms: int = 0) -> None:
        self._loop.run(self._msg.nak(delay=_seconds(delay_ms)))

    def term(self) -> None:
        self._loop.run(self._msg.term())


class NatsConsumer:
    def __init__(
        self,
        loop: _Loop,
        subscription: JetStreamContext.PullSubscription,
        js: JetStreamContext,
        policy: QueuePolicy,
        identity: str,
        swarm: str | None = None,
    ):
        self._loop = loop
        self._subscription = subscription
        self._js = js
        self._policy = policy
        self._identity = identity
        self._swarm = swarm

    def fetch(self, batch: int, timeout_ms: int) -> list[NatsDelivery]:
        return [NatsDelivery(self._loop, msg) for msg in self._loop.run(self._fetch(batch, timeout_ms))]

    async def _fetch(self, batch: int, timeout_ms: int) -> list:
        try:
            messages = await self._subscription.fetch(batch, timeout=_seconds(max(timeout_ms, MIN_FETCH_MS)))
        except FetchTimeout:
            return []
        deliveries = []
        for msg in messages:
            if _attempt(msg) > self._policy.max_deliver:
                event = Envelope.decode(msg.data)
                await _publish_dead(self._js, event, self._identity, self._policy.max_deliver, self._swarm)
                await msg.term()
            else:
                deliveries.append(msg)
        return deliveries


async def _publish(js, subject: str, stream: str, event: Envelope) -> bool:
    ack = await js.publish(subject, event.encode(), stream=stream, headers={MSG_ID: event.id})
    return not ack.duplicate


async def _publish_dead(
    js: JetStreamContext, event: Envelope, identity: str, attempts: int, swarm: str | None = None
) -> None:
    letter = dead_letter(event, identity, attempts)
    stream = stream_name(swarm, "DEAD") if swarm is not None else DEAD_STREAM
    target = event_subject(swarm, "dead", letter.id) if swarm is not None else f"dead.{event.swarm}.{letter.id}"
    try:
        await js.publish(
            target,
            letter.encode(),
            stream=stream,
            headers={MSG_ID: letter.id, "Nats-Expected-Last-Subject-Sequence": "0"},
        )
    except APIError:
        stored = await js.get_last_msg(stream, target)
        if Envelope.decode(stored.data).id != letter.id:
            raise


class NatsEventLog:
    def __init__(
        self,
        loop: _Loop,
        js: JetStreamContext,
        policy: QueuePolicy,
        stream: str,
        provision: bool,
        swarm: str | None = None,
        credentials: str | None = None,
    ):
        self._loop, self._js, self._policy = loop, js, policy
        self._stream, self._provision, self._swarm = stream, provision, swarm
        self._permissions = (
            nkey.claims(nkey.parse_creds(credentials)[0])["nats"].get("pub", {}) if credentials is not None else {}
        )
        self._policies: dict[str, QueuePolicy] = {}

    def publish(self, subject: str, event: Envelope) -> bool:
        target = legacy_target(subject, self._permissions) if self._stream == EVENT_STREAM else subject
        return self._loop.run(_publish(self._js, target, self._stream, event))

    def consumer(self, durable: str, subject: str, policy: QueuePolicy | None = None) -> NatsConsumer:
        policy = policy or self._policy
        if self._policies.setdefault(durable, policy) != policy:
            raise ValueError(f"durable consumer {durable!r} already has another policy")
        return NatsConsumer(
            self._loop,
            self._loop.run(_pull(self._js, self._stream, durable, subject, policy, self._provision)),
            self._js,
            policy,
            f"events.{durable}",
            self._swarm,
        )


class NatsWorkQueue:
    def __init__(
        self,
        loop: _Loop,
        js: JetStreamContext,
        name: str,
        policy: QueuePolicy,
        config: BusConfig,
        swarm: str | None = None,
    ):
        self.policy, self._loop, self._js, self._swarm = policy, loop, js, swarm
        lane = queue_lane(swarm, name) if swarm is not None else None
        self._stream = stream_name(swarm, f"QUEUE_{lane}") if swarm is not None else queue_stream(name)
        base = event_subject(swarm, "queue", lane) if swarm is not None else f"{QUEUE_SUBJECT}.{name}"
        self._subject = f"{base}.work" if swarm is not None else base
        self._filter = f"{base}.>" if swarm is not None else base
        if config.provision:
            loop.run(self._provision(config))
        elif swarm is not None:
            info = loop.run(js.stream_info(self._stream))
            if (info.config.metadata or {}).get("agentihooks_import_pending"):
                raise RuntimeError("scoped queue migration is incomplete")
        durable = lane if swarm is not None else self._stream.removeprefix("AGENTIHOOKS_QUEUE_")
        self._consumer = NatsConsumer(
            loop,
            loop.run(_pull(js, self._stream, durable, self._filter, policy, config.provision)),
            js,
            policy,
            f"queue.{name}",
            swarm,
        )

    async def _provision(self, config: BusConfig) -> None:
        if self._swarm is not None:
            try:
                await self._js.stream_info(self._stream)
            except NotFoundError:
                pass
            else:
                return
        await self._js.add_stream(
            name=self._stream,
            subjects=[self._filter],
            retention=api.RetentionPolicy.WORK_QUEUE,
            duplicate_window=_seconds(config.dedupe_window_ms),
        )

    def offer(self, event: Envelope) -> bool:
        if self._swarm is not None:
            require_owner(self._swarm, event)
        return self._loop.run(_publish(self._js, self._subject, self._stream, event))

    def take(self, timeout_ms: int) -> NatsDelivery | None:
        deliveries = self._consumer.fetch(1, timeout_ms)
        return deliveries[0] if deliveries else None


class NatsStateStore:
    def __init__(self, loop: _Loop, js: JetStreamContext, bucket: str, provision: bool):
        self._loop = loop
        self._js = js
        self._bucket = bucket
        self._kv = loop.run(
            js.create_key_value(bucket=bucket, history=KV_HISTORY) if provision else js.key_value(bucket)
        )

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
        self._views: dict[str, Bus] = {}
        try:
            self._nc = self._loop.run(
                nats_client.connect(url, allow_reconnect=False, **auth_options(config.credentials))
            )
            self._js = self._nc.jetstream()
            if config.provision:
                self._loop.run(provision_events(self._js, EVENT_STREAM, _seconds(config.dedupe_window_ms)))
                self._loop.run(
                    self._js.add_stream(
                        name=DEAD_STREAM, subjects=[DEAD_SUBJECT], duplicate_window=_seconds(config.dedupe_window_ms)
                    )
                )
        except BaseException:
            self._loop.close()
            raise
        self._legacy_events = NatsEventLog(
            self._loop, self._js, config.queue, EVENT_STREAM, config.provision, credentials=config.credentials
        )
        self._legacy_dead = NatsEventLog(self._loop, self._js, config.queue, DEAD_STREAM, config.provision)
        self.events = (
            RoutedLog(lambda swarm: self.for_swarm(swarm).events, lambda: self._logs("events"), config.queue)
            if config.scoped
            else self._legacy_events
        )
        self.dead = (
            RoutedLog(lambda swarm: self.for_swarm(swarm).dead, lambda: self._logs("dead"), config.queue)
            if config.scoped
            else self._legacy_dead
        )

    def for_swarm(self, swarm: str) -> Bus:
        from .nats_scope import NatsScopedBus

        event_subject(swarm, "events")
        if self._loop.closed:
            raise RuntimeError("bus is closed")
        if not self._config.scoped:
            return self
        with self._lock:
            view = self._views.get(swarm)
            if view is None:
                view = self._views[swarm] = NatsScopedBus(self, swarm)
            return view

    def _logs(self, kind: str) -> list[EventLog]:
        from .nats_scope import discover_swarms

        if self._config.provision:
            for swarm in self._loop.run(discover_swarms(self._js)):
                self.for_swarm(swarm)
        with self._lock:
            legacy = [getattr(self, f"_legacy_{kind}")] if self._config.provision else []
            return legacy + [getattr(view, kind) for view in self._views.values()]

    def queue(self, name: str, policy: QueuePolicy) -> WorkQueue:
        if self._config.scoped:
            parts = name.split(".")
            if len(parts) != 4 or parts[0] != PREFIX or parts[2] != "ready":
                raise ValueError("select a swarm before opening a scoped queue")
            return self.for_swarm(parts[1]).queue(name, policy)
        with self._lock:
            queue = self._queues.get(name)
            if queue is None:
                queue = self._queues[name] = NatsWorkQueue(self._loop, self._js, name, policy, self._config)
            elif queue.policy != policy:
                raise ValueError(f"work queue {name!r} already has another policy")
            return queue

    def state(self, bucket: str) -> NatsStateStore:
        if self._config.scoped and not self._config.provision and bucket not in SHARED_SLOT_BUCKETS:
            raise PermissionError("select a swarm before opening scoped state")
        with self._lock:
            store = self._buckets.get(bucket)
            if store is None:
                store = self._buckets[bucket] = NatsStateStore(self._loop, self._js, bucket, self._config.provision)
            return store

    def serve(self, subject: str, queue: str, handler: Callable[[str, bytes], bytes]) -> None:
        async def respond(msg: Msg) -> None:
            answer = await asyncio.get_running_loop().run_in_executor(None, handler, msg.subject, msg.data)
            await msg.respond(answer)

        self._loop.run(self._nc.subscribe(subject, queue=queue, cb=respond))

    def request(self, subject: str, data: bytes, timeout_ms: int) -> bytes:
        try:
            return self._loop.run(self._nc.request(subject, data, timeout=_seconds(timeout_ms))).data
        except (FetchTimeout, NoRespondersError):
            raise TimeoutError(f"no responders for {subject!r}") from None

    def migrate_state(self, swarm: str, bucket: str, keys: list[str]) -> None:
        from .nats_migration import migrate_state

        migrate_state(self, swarm, bucket, keys)

    def migrate_queue(self, swarm: str, name: str, policy: QueuePolicy) -> None:
        from .nats_migration import migrate_queue

        migrate_queue(self, swarm, name, policy)

    def migrate_dead(self, swarm: str) -> None:
        from .nats_migration import migrate_dead

        migrate_dead(self, swarm)

    def close(self) -> None:
        with self._lock:
            try:
                if not self._loop.closed:
                    self._loop.run(self._nc.close())
            finally:
                self._loop.close()


def open_bus(config: BusConfig) -> NatsBus:
    return NatsBus(config, config.url or DEFAULT_URL)
