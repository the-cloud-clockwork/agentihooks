import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from scripts.events.config import BusConfig, QueuePolicy
from scripts.events.envelope import Envelope
from scripts.events.factory import open_bus
from scripts.hub import lease
from scripts.swarm import readiness
from tests.chaos import audit
from tests.chaos.hub import REACTOR
from tests.chaos.journal import Journal, now_ms, read
from tests.chaos.server import NatsServer

LANE = "eng"
TICK_S = 0.2
HUBS = ("hub-a", "hub-b")


class Fleet:
    def __init__(self, folder: Path, url: str, swarm: str):
        self.folder, self.url, self.swarm = folder, url, swarm
        self.procs: dict[str, subprocess.Popen] = {}
        self.argv: dict[str, list[str]] = {}
        self.down: set[str] = set()

    def spawn(self, name: str, module: str, *extra: str) -> None:
        self.argv[name] = [sys.executable, "-m", module, "--url", self.url, "--swarm", self.swarm]
        self.argv[name] += ["--journal", str(self.folder / f"{name}.jsonl"), *extra]
        self.restart(name)

    def restart(self, name: str) -> None:
        with open(self.folder / f"{name}.log", "a") as log:
            self.procs[name] = subprocess.Popen(self.argv[name], stdout=log, stderr=subprocess.STDOUT)

    def kill(self, name: str) -> None:
        self.down.add(name)
        self.procs[name].kill()
        self.procs[name].wait(10)

    def exited(self) -> list[str]:
        return [name for name, proc in self.procs.items() if name not in self.down and proc.poll() is not None]

    def stop(self) -> None:
        for proc in self.procs.values():
            if proc.poll() is None:
                proc.kill()
                proc.wait(10)


def last_kind(path: Path) -> str | None:
    lines = [line for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
    return json.loads(lines[-1])["kind"] if lines else None


class Chaos:
    def __init__(self, args: argparse.Namespace, folder: Path):
        self.args, self.folder = args, folder
        self.journal = Journal(folder / "chaos.jsonl", "chaos")
        self.server = NatsServer(folder / "nats-store", folder / "nats.log", args.nats_server)
        self.fleet = Fleet(folder, self.server.url, args.swarm)
        self.bus, self.offering, self.sequence, self.next_offer, self.holder = None, True, 0, 0.0, None
        self.unsent: list[Envelope] = []

    def _bus(self):
        if self.bus is None:
            self.bus = open_bus(BusConfig(backend="nats", url=self.server.url))
        return self.bus

    def _drop_bus(self) -> None:
        try:
            self.bus.close()
        except Exception:
            pass
        self.bus = None

    def _offer(self) -> None:
        if self.offering and time.monotonic() >= self.next_offer:
            self.sequence += 1
            task = f"t{self.sequence:04d}"
            self.unsent.append(
                Envelope(
                    id=readiness.offer_id(self.args.swarm, {"id": task, "claim_generation": 1}),
                    type="task.ready",
                    swarm=self.args.swarm,
                    entity="task",
                    key=task,
                    version=1,
                    time=now_ms(),
                    author="chaos",
                    data={"lane": LANE, "generation": 1},
                )
            )
            self.next_offer = time.monotonic() + self.args.offer_s
        queue = self._bus().queue(readiness.queue_name(self.args.swarm, LANE), QueuePolicy())
        while self.unsent:
            queue.offer(self.unsent[0])
            self.journal.write("offered", task=self.unsent.pop(0).key)

    def _lease(self) -> None:
        entry = self._bus().state(lease.BUCKET).get(f"reactor.{REACTOR}")
        state = lease.LeaseState.decode(entry.value) if entry else None
        holder = state.holder if state and state.expires_ms > lease.wall_ms() else None
        if holder != self.holder:
            self.holder = holder
            self.journal.write("lease", holder=holder)

    def tick(self) -> None:
        if self.server.process.poll() is not None:
            return
        for name in self.fleet.exited():
            self.journal.write("restarted", target=name)
            self.fleet.restart(name)
        try:
            self._offer()
            self._lease()
        except Exception:
            self._drop_bus()

    def run_for(self, seconds: float, until=None) -> bool:
        stop = time.monotonic() + seconds
        while time.monotonic() < stop:
            self.tick()
            if until is not None and until():
                return True
            time.sleep(TICK_S)
        return False

    def kill_hive(self) -> None:
        target = "hive-1"
        if not self.run_for(30, lambda: last_kind(self.folder / f"{target}.jsonl") == "launching"):
            raise RuntimeError(f"{target} started no launch within 30 s, so its kill would hold no work")
        self.fleet.kill(target)
        self.journal.write("fault", fault="hive-kill", target=target)
        self.run_for(self.args.window_s)
        self.journal.write("window-end", fault="hive-kill")

    def restart_nats(self) -> None:
        hives = [name for name in self.fleet.procs if name.startswith("hive-") and name not in self.fleet.down]
        self.run_for(15, lambda: any(last_kind(self.folder / f"{name}.jsonl") == "launching" for name in hives))
        self.server.kill()
        self.journal.write("fault", fault="nats-restart", target="nats")
        self._drop_bus()
        time.sleep(self.args.outage_s)
        self.server.start()
        self.journal.write("fault-cleared", fault="nats-restart")
        self.run_for(self.args.window_s)
        self.journal.write("window-end", fault="nats-restart")

    def kill_hub(self) -> None:
        if not self.run_for(30, lambda: self.holder is not None):
            raise RuntimeError("no hub replica held the singleton lease within 30 s")
        target = self.holder
        self.fleet.kill(target)
        self.journal.write("fault", fault="hub-kill", target=target)
        self.run_for(self.args.window_s, lambda: self.holder not in (None, target))
        self.fleet.down.discard(target)
        self.run_for(self.args.window_s)
        self.journal.write("window-end", fault="hub-kill")

    def drain(self) -> None:
        self.offering = False
        self.run_for(self.args.drain_s)
        self.journal.write("drained")

    def run(self) -> dict:
        try:
            self.server.start()
            for name in HUBS:
                self.fleet.spawn(name, "tests.chaos.hub", "--name", name)
            for index in range(1, self.args.hives + 1):
                hive = f"hive-{index}"
                self.fleet.spawn(hive, "tests.chaos.hive", "--hive", hive, "--capacity", str(self.args.capacity))
            self.run_for(self.args.warmup_s)
            self.kill_hive()
            self.restart_nats()
            self.kill_hub()
            self.drain()
        finally:
            self.fleet.stop()
            self.server.stop()
        return audit.report(read(self.folder))


def summary(found: dict) -> str:
    lines = []
    for window in found["windows"]:
        lines.append(
            f"{window['fault']} ({window['target']}): duplicate claims {window['duplicate_claims']}, "
            f"orphans at window end among work offered before the fault {window['orphans']}, "
            f"exit events handled twice {window['exit_events_handled_twice']}, "
            f"never handled {window['exit_events_never_handled']}, reclaim {json.dumps(window['reclaim'])}"
        )
    final = found["final"]
    lines.append(
        f"final: offered {final['offered']}, launches {final['launches']}, duplicate claims "
        f"{final['duplicate_claims']}, orphans {final['orphans']} {json.dumps(final['orphaned'])}, exit events "
        f"handled twice {final['exit_events_handled_twice']}, never handled {final['exit_events_never_handled']}"
    )
    lines.append(f"contract met: {found['contract_met']}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="chaos run")
    parser.add_argument("--folder", required=True)
    parser.add_argument("--swarm", default="chaos")
    parser.add_argument("--nats-server")
    parser.add_argument("--hives", type=int, default=3)
    parser.add_argument("--capacity", type=int, default=4)
    parser.add_argument("--offer-s", type=float, default=1.5)
    parser.add_argument("--warmup-s", type=float, default=20)
    parser.add_argument("--window-s", type=float, default=120)
    parser.add_argument("--outage-s", type=float, default=3)
    parser.add_argument("--drain-s", type=float, default=120)
    args = parser.parse_args(argv)
    folder = Path(args.folder)
    folder.mkdir(parents=True, exist_ok=False)
    found = Chaos(args, folder).run()
    (folder / "report.json").write_text(json.dumps(found, indent=2, default=str))
    print(summary(found))
    return 0 if found["contract_met"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
