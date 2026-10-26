"""Operator command line over the session catalog's revocation consumers."""

import argparse
import json
import time
from collections.abc import Callable
from pathlib import Path

from hooks.memory.catalog import Catalog
from hooks.memory.revocation import Propagator


def _register(propagator: Propagator, domain: str, document: dict) -> dict:
    for pack in document.get("packs", []):
        propagator.put_pack(domain, pack["pack_id"], pack["body"], pack["sessions"])
    for lesson in document.get("lessons", []):
        propagator.put_lesson(domain, lesson["lesson_id"], lesson["text"], lesson["support"])
    return {"packs": len(document.get("packs", [])), "lessons": len(document.get("lessons", []))}


def main(argv: list[str] | None = None, clock: Callable[[], float] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("catch-up", "exposures", "lags", "register"))
    parser.add_argument("catalog")
    parser.add_argument("domain")
    parser.add_argument("--session")
    parser.add_argument("--document", type=Path)
    args = parser.parse_args(argv)
    if (args.command == "exposures") != (args.session is not None):
        parser.error("--session is given with exposures and only with it")
    if (args.command == "register") != (args.document is not None):
        parser.error("--document is given with register and only with it")
    catalog = Catalog(args.catalog)
    propagator = Propagator(catalog)
    if args.command == "catch-up":
        answer = {"notices": propagator.catch_up(args.domain, time.time() if clock is None else clock())}
    elif args.command == "exposures":
        answer = {"exposures": propagator.exposures(args.domain, args.session)}
    elif args.command == "lags":
        answer = {"lags": propagator.lags(args.domain)}
    else:
        answer = _register(propagator, args.domain, json.loads(args.document.read_bytes()))
    catalog.close()
    print(json.dumps(answer))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
