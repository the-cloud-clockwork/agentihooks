import argparse
import subprocess
import sys

from hooks.secrets import redact
from scripts.hive.orders import Orders
from scripts.swarm.store import connect

LOG_LIMIT = 1024 * 1024


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    split = arguments.index("--")
    parser = argparse.ArgumentParser()
    parser.add_argument("--slug", required=True)
    parser.add_argument("--execution", required=True)
    args = parser.parse_args(arguments[:split])
    store = connect()
    agent = store.execution_registry.get(args.slug, args.execution)
    receipt = Orders(store.redis).launch_receipt(
        agent.runtime_target["host_id"], args.slug, agent.execution_id, agent.generation
    )
    retained = ""
    store.redis.hset(receipt, "supervisor_log", retained)
    with subprocess.Popen(
        arguments[split + 1 :], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace"
    ) as process:
        for line in process.stdout:
            clean = redact(line, mode="strict")
            retained = (retained + clean)[-LOG_LIMIT:]
            store.redis.hset(receipt, "supervisor_log", retained)
            print(clean, end="", flush=True)
        return process.wait()


if __name__ == "__main__":
    sys.exit(main())
