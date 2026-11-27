import json
import shutil
import tempfile
from pathlib import Path

from tests.ledger_load import gate


def main() -> int:
    stop, run = gate.stop_server, gate.run

    def measured_stop(process):
        status = Path(f"/proc/{process.pid}/status").read_text().splitlines()
        peak = next(int(line.split()[1]) for line in status if line.startswith("VmHWM:"))
        print(json.dumps({"server_peak_rss_kib": peak}), flush=True)
        return stop(process)

    def memory_run(folder: Path) -> tuple[list[str], float | None]:
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir="/dev/shm", prefix="ledger-load-") as directory:
            scratch = Path(directory)
            try:
                return run(scratch)
            finally:
                for log in scratch.glob("*.log"):
                    shutil.copyfile(log, folder / log.name)

    gate.stop_server = measured_stop
    gate.run = memory_run
    return gate.main()


if __name__ == "__main__":
    raise SystemExit(main())
