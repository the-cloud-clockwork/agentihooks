import argparse
import json
import os
import sys
import time
from collections.abc import Callable


def _routing_settings():
    from scripts.routing import place
    from scripts.routing.settings import open_store

    return open_store(place._client(os.environ), os.environ)


def _setting_text(value: object) -> str:
    return "unset" if value is None else str(value)


def _setting_pair(pair: str) -> tuple[str, object]:
    from scripts.routing.settings import VALIDATORS

    key, sep, text = pair.partition("=")
    if not sep:
        raise ValueError(f"expected KEY=VALUE, got {pair}")
    if key not in VALIDATORS:
        raise ValueError(f"unknown routing setting {key}")
    if text == "none":
        return key, None
    try:
        value = text if key.startswith("master-") else json.loads(text)
    except json.JSONDecodeError:
        value = text
    if not VALIDATORS[key](value):
        raise ValueError(f"invalid value for {key}: {text}")
    return key, value


def _store_label(store: object) -> str:
    from scripts.routing.settings import FileSettings

    return f"file {store.path}" if isinstance(store, FileSettings) else "redis"


def cmd_balance_set(pairs: list[str], now: float | None = None) -> int:
    try:
        changes = [_setting_pair(pair) for pair in pairs]
    except ValueError as exc:
        print(f"agentihooks balance set: {exc}", file=sys.stderr)
        return 2
    store = _routing_settings()
    actor = os.environ.get("AGENTIHOOKS_AGENT_NAME") or "operator"
    at = time.time() if now is None else now
    print(f"store={_store_label(store)}")
    for key, value in changes:
        before = store.get(key)
        store.set(key, value, actor, at)
        print(f"{key}: {_setting_text(before)} -> {_setting_text(store.get(key))}")
    return 0


def cmd_balance_settings() -> int:
    from scripts.routing.settings import VALIDATORS

    store = _routing_settings()
    values = store.all()
    print(f"store={_store_label(store)}")
    for key in VALIDATORS:
        print(f"{key}={_setting_text(values.get(key))}")
    return 0


def add_parser(sub: argparse._SubParsersAction) -> None:
    balance_p = sub.add_parser("balance", help="Probe and rank Claude OAuth accounts without launching workload")
    balance_p.add_argument("--dry-run", action="store_true", help="Report routing state without launching Claude")
    balance_p.add_argument("--fable", action="store_true", help="Include the separate Fable weekly quota")
    balance_p.add_argument(
        "--show-account-metadata",
        metavar="SLUG",
        default="",
        help="Print every JSON event returned by a fresh probe for AH_CC_TOKEN_<SLUG>",
    )
    balance_p.add_argument("--refresh", action="store_true", help="Ignore the 60-second quota cache")
    balance_p.add_argument(
        "--current",
        action="store_true",
        help="Name the account this Claude session runs on; other accounts come from the quota cache",
    )
    balance_p.add_argument("--timeout", type=float, default=60, help="Per-account probe timeout in seconds")
    balance_sub = balance_p.add_subparsers(dest="balance_command")
    balance_set_p = balance_sub.add_parser("set", help="Write routing settings: set KEY=VALUE ... (VALUE none clears)")
    balance_set_p.add_argument("pairs", nargs="+", metavar="KEY=VALUE")
    balance_sub.add_parser("settings", help="List every routing setting key with its value")


def run(args: argparse.Namespace, cmd_balance: Callable[..., int]) -> int:
    if args.balance_command == "set":
        return cmd_balance_set(args.pairs)
    if args.balance_command == "settings":
        return cmd_balance_settings()
    return cmd_balance(
        include_fable=args.fable,
        refresh=args.refresh,
        timeout=args.timeout,
        show_account_metadata=args.show_account_metadata,
        current=args.current,
    )
