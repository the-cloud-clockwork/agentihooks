"""Agent commands that read a change request and post a review comment through the repository's forge."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from scripts.forge import remote
from scripts.forge.model import ForgeError


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="agentihooks forge", description="Read a change request or post a review")
    sub = parser.add_subparsers(dest="action", required=True)
    read = sub.add_parser("read", help="print the change request as JSON")
    post = sub.add_parser("post", help="post a review comment and print its URL")
    post.add_argument("--body-file", required=True, help="file holding the comment, - for standard input")
    for action in (read, post):
        action.add_argument("number", type=int)
        action.add_argument("--remote", help="remote URL, default the origin of the current checkout")
        action.add_argument("--forge", choices=remote.KINDS, help="forge kind for a host that does not name one")
    return parser.parse_args(argv)


def body_of(name: str) -> str:
    return sys.stdin.read() if name == "-" else Path(name).read_text()


def main(argv: list[str]) -> int:
    args = parse(argv)
    try:
        forge = remote.pick(remote.parse(args.remote or remote.origin(".")), args.forge, os.environ)
        text = (
            json.dumps(forge.read(args.number).as_dict(), indent=2)
            if args.action == "read"
            else forge.post(args.number, body_of(args.body_file))
        )
    except (ForgeError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"forge: {error}", file=sys.stderr)
        return 1
    print(text)
    return 0
