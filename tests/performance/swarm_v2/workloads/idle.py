"""Idle CLI stand in: one process that starts and waits, the sample an idle agent CLI gives."""

import argparse
import time


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hold", type=float, default=3.0)
    time.sleep(parser.parse_args().hold)


if __name__ == "__main__":
    main()
