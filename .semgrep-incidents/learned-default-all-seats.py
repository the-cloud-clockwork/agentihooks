from argparse import Namespace
from typing import Any


def broad(store: Any, args: Namespace) -> None:
    # ruleid: learned-default-all-seats
    _list_learned(store, args.slug)


def scoped(store: Any, args: Namespace, seat: str) -> None:
    # ok: learned-default-all-seats
    _list_learned(store, args.slug, seat, all_seats=args.all)
