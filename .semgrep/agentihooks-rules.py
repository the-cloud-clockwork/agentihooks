import asyncio
import itertools
import threading
import time
from itertools import count

TIMER = (
    # ruleid: machine-timer-unit
    "[Timer]\n"
    # ruleid: machine-timer-unit
    "OnUnitActiveSec=60s\n"
)
# ruleid: machine-timer-unit
CALENDAR = "OnCalendar=hourly"
# ruleid: machine-cron-entry
CRON = ["crontab", "-l"]
# ruleid: machine-cron-entry
TRANSIENT = "systemd-run --user --on-active=5m agentihooks swarm tick"
# ruleid: machine-cron-entry
AT = ["at", "now", "+", "5", "minutes"]
# ok: machine-timer-unit
SERVICE = "[Service]\nRestart=always\n"


def beat(work):
    while True:
        work()
        # ruleid: periodic-sleep-loop
        time.sleep(30)


async def evict(pool):
    while True:
        # ruleid: periodic-sleep-loop
        await asyncio.sleep(60)
        await pool.evict()


def watch(work, interval):
    for _ in itertools.count():
        work()
        # ruleid: periodic-sleep-loop
        time.sleep(interval)


def poll(stop: threading.Event, work):
    # ruleid: periodic-wait-loop
    while not stop.wait(5):
        work()


def ready(probe, deadline):
    while True:
        if probe():
            return True
        if time.monotonic() >= deadline:
            return False
        # ok: periodic-sleep-loop
        time.sleep(0.25)


def backoff(connect):
    delay = 1.0
    while True:
        try:
            return connect()
        except OSError:
            # ok: periodic-sleep-loop
            time.sleep(delay)
            delay = min(delay * 2, 30.0)


def drain(queue):
    while True:
        item = queue.get()
        if item is None:
            break
        # ruleid: periodic-sleep-loop
        time.sleep(0.01)


def bounded(probe, deadline):
    while time.monotonic() < deadline and not probe():
        # ok: periodic-sleep-loop
        time.sleep(0.1)


def beat_on_the_scheduler(beat, delay):
    from scripts.scheduler.process_timer import wait as scheduled_wait

    while True:
        beat()
        # ok: periodic-sleep-loop
        scheduled_wait(delay, timeout=delay + 1)


def reconnect(connect):
    delay = 1.0
    while True:
        try:
            connect()
            delay = 1.0
        except OSError:
            # ok: periodic-sleep-loop
            time.sleep(delay)
            delay = min(delay * 2, 30.0)


class Pump:
    def run(self, timing):
        while True:
            self.work()
            # ruleid: periodic-sleep-loop
            timing.sleep(5)


UNIT_KEYS = (
    # ruleid: machine-timer-unit
    "OnBootSec=1min",
    # ruleid: machine-timer-unit
    "OnStartupSec=1min",
    # ruleid: machine-timer-unit
    "OnActiveSec=5m",
    # ruleid: machine-timer-unit
    "OnUnitInactiveSec=5m",
)
CRON_PATHS = (
    # ruleid: machine-cron-entry
    "/etc/cron.d/agentihooks",
    # ruleid: machine-cron-entry
    "/var/spool/cron/iamroot",
    # ruleid: machine-cron-entry
    "<key>StartInterval</key>",
    # ruleid: machine-cron-entry
    "<key>StartCalendarInterval</key>",
    # ruleid: machine-cron-entry
    "schtasks /create /sc minute",
    # ruleid: machine-cron-entry
    "echo agentihooks swarm tick | at now",
    # ruleid: machine-cron-entry
    "echo agentihooks swarm tick | batch now",
)


def legacy(work):
    while 1:
        work()
        # ruleid: periodic-sleep-loop
        time.sleep(30)


def counted(work):
    for _ in count():
        work()
        # ruleid: periodic-sleep-loop
        time.sleep(30)


def flagged(stop: threading.Event, work):
    while not stop.is_set():
        work()
        # ruleid: periodic-sleep-loop
        stop.wait(5)


class Worker:
    def run(self, work):
        while self.running:
            work()
            # ruleid: periodic-sleep-loop
            time.sleep(5)


def paced(stop: threading.Event, work):
    while True:
        work()
        # ruleid: periodic-sleep-loop
        stop.wait(60)


def keyword_paced(stop: threading.Event, work):
    while True:
        work()
        # ruleid: periodic-sleep-loop
        stop.wait(timeout=60)


def once(args, work):
    while True:
        work()
        if args.once:
            return 0
        # ruleid: periodic-sleep-loop
        time.sleep(60)


def stream(hub, send):
    while True:
        # ruleid: periodic-sleep-loop
        pending = hub.wait(5)
        send(pending)


def retried(read, attempts):
    for attempt in itertools.count(1):
        try:
            return read()
        except OSError:
            if attempt >= attempts:
                raise
        # ok: periodic-sleep-loop
        time.sleep(attempt)


def polled(probe, clock, timeout):
    deadline = clock() + timeout
    while True:
        if probe():
            return True
        if clock() >= deadline:
            return False
        # ok: periodic-sleep-loop
        time.sleep(0.25)


def timed(work):
    while True:
        started = time.monotonic()
        work(started)
        # ruleid: periodic-sleep-loop
        time.sleep(30)


def counting(work):
    attempt = 0
    while True:
        attempt += 1
        work(attempt)
        # ruleid: periodic-sleep-loop
        time.sleep(30)


def unread(work, deadline):
    while True:
        work()
        # ruleid: periodic-sleep-loop
        time.sleep(30)


def guarded(work):
    while True:
        try:
            work()
            # ruleid: periodic-sleep-loop
            time.sleep(60)
        except OSError:
            # ok: periodic-sleep-loop
            time.sleep(1)


def expiring(order, clock, ack):
    while True:
        if ack():
            return True
        if clock() >= order.expires_at:
            return False
        # ok: periodic-sleep-loop
        time.sleep(1)


# ok: machine-cron-entry
# The quota refresh runs at 09:00 UTC, so cache it.
# ok: machine-cron-entry
NOTE = "the digest is sent at midnight"
# ok: machine-cron-entry
LABEL = "rows | batch size 50"
# ok: machine-cron-entry
TEXT = "looks at now and then"


# ok: machine-timer-unit
ANNOTATION = "Iterator[Timer]"
