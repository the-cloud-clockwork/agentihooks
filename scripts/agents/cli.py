import argparse
import json

from hooks.context.profile_chain import card
from scripts import install


def show_card(name: str) -> dict:
    path = install._resolve_profile_dir(name)
    if path is None:
        raise ValueError(f"Unknown agent profile: {name}")
    return card(name, path)


def list_cards(capability: str | None = None) -> list[dict]:
    cards = [show_card(name) for name in install._available_profiles()]
    if capability is not None:
        cards = [item for item in cards if capability in item["capabilities"]]
    return cards


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentihooks agents")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="List profile cards as JSON")
    listing.add_argument("--capability", help="Require this declared capability")
    showing = commands.add_parser("show", help="Show one profile card as JSON")
    showing.add_argument("name")
    args = parser.parse_args(argv)
    try:
        result = list_cards(args.capability) if args.command == "list" else show_card(args.name)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))
    return 0
