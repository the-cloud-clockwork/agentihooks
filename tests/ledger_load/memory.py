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

    def memory_run(folder):
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir="/dev/shm", prefix="ledger-load-") as directory:
            scratch = Path(directory)
            try:
                return run(scratch)
            finally:
                if (log := scratch / "server.log").exists():
                    shutil.copyfile(log, folder / "server.log")

    gate.stop_server = measured_stop
    gate.run = memory_run
    return gate.main()


if __name__ == "__main__":
    raise SystemExit(main())
