"""One time move of a swarm's fencing document onto per record keys, run after the dual reader rollout."""

import argparse
from collections.abc import Sequence

from scripts.swarm import fencing, store


def main(argv: Sequence[str] | None = None) -> None:
    """Migrates one swarm and prints how many records it wrote; an already migrated swarm writes none."""
    parser = argparse.ArgumentParser()
    parser.add_argument("slug")
    parser.add_argument("--writers-upgraded", action="store_true")
    args = parser.parse_args(argv)
    if not args.writers_upgraded:
        parser.error("upgrade every live fencing reader and writer before migrating to per record keys")
    connected = store.connect()  # pyright: ignore[reportUnknownMemberType]
    print(fencing.migrate_records(connected, args.slug, writers_upgraded=True))


if __name__ == "__main__":
    main()
