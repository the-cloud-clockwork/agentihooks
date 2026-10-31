import argparse
from collections.abc import Sequence

from scripts.swarm import fencing, store


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Migrate execution history after the fencing reader rollout")
    parser.add_argument("slug")
    parser.add_argument("--writers-upgraded", action="store_true")
    args = parser.parse_args(argv)
    if not args.writers_upgraded:
        parser.error("upgrade every live fencing writer before migrating the archive")
    connected = store.connect()  # pyright: ignore[reportUnknownMemberType]
    fencing.migrate_archive(connected, args.slug, writers_upgraded=args.writers_upgraded)


if __name__ == "__main__":
    main()
