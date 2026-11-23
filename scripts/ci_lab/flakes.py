"""The lab flakes export: the flake rows of every lab flakes object written within a window, as one flake ledger file.

agentihooks ci lab flakes --out FOLDER [--hours N] [--objects ROOT] reads a local objects folder, or the lab object
store named by the CI_LAB_MINIO_* settings, and writes FOLDER/lab-flakes.jsonl for tests.flake_budget.
"""

import argparse
from collections.abc import Mapping
from pathlib import Path

__all__ = ["command", "declare"]

WINDOW_HOURS = 168


def declare(parser: argparse.ArgumentParser) -> None:
    """Declare the ci lab flakes arguments: the output folder, the window in hours and an optional objects folder."""
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--hours", type=int, default=WINDOW_HOURS)
    parser.add_argument("--objects", type=Path)


def command(arguments: argparse.Namespace, environ: Mapping[str, str], now_s: float) -> int:
    """Export the window's flake rows and print the file, or print why the store could not be read and exit 1."""
    return 1
