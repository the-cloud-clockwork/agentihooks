"""Memory heavy workload: each child process holds a touched buffer of the given size."""

import argparse
import subprocess
import sys
import time

PAGE = 4096


def hold(mib: int, seconds: float) -> None:
    buffer = bytearray(mib << 20)
    buffer[::PAGE] = b"\x01" * len(range(0, len(buffer), PAGE))
    time.sleep(seconds)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--children", type=int, required=True)
    parser.add_argument("--mib", type=int, default=256)
    parser.add_argument("--hold", type=float, default=2.0)
    parser.add_argument("--child", action="store_true")
    args = parser.parse_args()
    if args.child:
        hold(args.mib, args.hold)
        return 0
    command = [sys.executable, __file__, "--children", "0", "--mib", str(args.mib), "--hold", str(args.hold), "--child"]
    children = [subprocess.Popen(command) for _ in range(args.children)]
    return max((child.wait() for child in children), default=0)


if __name__ == "__main__":
    sys.exit(main())
