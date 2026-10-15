from __future__ import annotations

import argparse
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("kernel_vectors", type=Path)
    args = parser.parse_args(argv)
    local = Path(__file__).resolve().parents[1] / "tests/fixtures/marker-idempotency-vectors.json"
    if local.read_bytes() != args.kernel_vectors.read_bytes():
        print("::error::Marker key vectors differ from kernel dev.")
        return 1
    print("Marker key vectors match kernel dev.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
