import json
from pathlib import Path

from tests.ledger_load import gate


def main() -> int:
    stop = gate.stop_server

    def measured_stop(process):
        status = Path(f"/proc/{process.pid}/status").read_text().splitlines()
        peak = next(int(line.split()[1]) for line in status if line.startswith("VmHWM:"))
        print(json.dumps({"server_peak_rss_kib": peak}), flush=True)
        return stop(process)

    gate.stop_server = measured_stop
    return gate.main()


if __name__ == "__main__":
    raise SystemExit(main())
