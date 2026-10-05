"""agentihooks trace: the directives a session received and the layer behind each.

agentihooks trace [SESSION]                                   time, layer, source, text per directive
agentihooks trace [SESSION] --wrong SOURCE --repo PATH --reason TEXT
                                                              mark a directive wrong for a repository
agentihooks trace --corrections                               the whole corrections log

SESSION defaults to CLAUDE_CODE_SESSION_ID. Layers: bundle, profile,
enforcement, condition, broadcast, brain. SOURCE is the enforcement id, the
condition file or the broadcast id: the key that clears it at its source.
"""

import argparse
import os
import sys

from hooks.context import injection_trace


def _print_corrections(rows):
    if not rows:
        return
    print("corrections")
    for row in rows:
        print("\t".join((row["at"], row["layer"], row["source"], row["repo"], row["reason"])))


def build_parser():
    parser = argparse.ArgumentParser(
        prog="agentihooks trace", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("session", nargs="?", default=os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
    parser.add_argument("--wrong", metavar="SOURCE")
    parser.add_argument("--repo", default=os.getcwd())
    parser.add_argument("--reason", default="")
    parser.add_argument("--corrections", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.corrections:
        _print_corrections(injection_trace.corrections())
        return 0
    if not args.session:
        print("agentihooks trace: no session id given and CLAUDE_CODE_SESSION_ID is unset", file=sys.stderr)
        return 2
    if args.wrong:
        try:
            row = injection_trace.correct(args.session, args.wrong, args.repo, args.reason)
        except ValueError as e:
            print(f"agentihooks trace: {e}", file=sys.stderr)
            return 2
        _print_corrections([row])
        return 0
    rows = injection_trace.trace(args.session)
    for row in rows:
        print("\t".join((row["at"], row["layer"], row["source"], row["text"])))
    sources = {row["source"] for row in rows}
    _print_corrections([row for row in injection_trace.corrections() if row["source"] in sources])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
