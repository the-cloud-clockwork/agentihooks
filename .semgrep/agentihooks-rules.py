import asyncio
import itertools
import threading
import time

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
        # ok: periodic-sleep-loop
        time.sleep(0.01)


def bounded(probe, deadline):
    while time.monotonic() < deadline and not probe():
        # ok: periodic-sleep-loop
        time.sleep(0.1)


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


# ok: machine-timer-unit
ANNOTATION = "Iterator[Timer]"
