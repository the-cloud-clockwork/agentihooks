"""agentihooks trace: the directives a session received and the layer behind each.

agentihooks trace [SESSION]                                   time, layer, source, locator, text per directive
agentihooks trace [SESSION] --wrong SOURCE --repo PATH --reason TEXT [--quote PASSAGE]
                                                              mark a directive wrong for a repository;
                                                              PASSAGE names the wrong part of a rule or
                                                              doctrine file
agentihooks trace --corrections                               the whole corrections log
agentihooks trace sweep [--apply] [--root DIR] [--ledger SLUG]
                                                              every place an open correction's directive
                                                              still lives, and the action for each

SESSION defaults to CLAUDE_CODE_SESSION_ID. Layers: bundle, profile,
enforcement, condition, broadcast, brain, rule, doctrine, culture, recap,
learned. SOURCE is the enforcement id, the condition file, the broadcast id,
the rule or doctrine file (repo/path), or the swarm priming entry
(culture:SLUG#LINE, recap:SEAT#N, learned:SEAT#N): the key that clears it at
its source. LOCATOR finds that source: the enforcement store file, the
condition file and its layer, the broadcast id, the brain entry id and the file
it came from, the repo, path and blob of a rule or doctrine file, or the seat
and note number of a recap or learned note.
"""

import argparse
import os
import sys
from pathlib import Path

from hooks.context import injection_trace, trace_sweep


def _print_corrections(rows):
    if not rows:
        return
    print("corrections")
    for row in rows:
        locator = injection_trace.format_locator(row.get("locator"))
        quote = [row["quote"]] if row.get("quote") else []
        print("\t".join((row["at"], row["layer"], row["source"], locator, row["repo"], row["reason"], *quote)))


def build_parser():
    parser = argparse.ArgumentParser(
        prog="agentihooks trace", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("session", nargs="?", default=os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
    parser.add_argument("--wrong", metavar="SOURCE")
    parser.add_argument("--repo", default=os.getcwd())
    parser.add_argument("--reason", default="")
    parser.add_argument("--quote", default="")
    parser.add_argument("--corrections", action="store_true")
    return parser


def build_sweep_parser():
    parser = argparse.ArgumentParser(prog="agentihooks trace sweep", description=trace_sweep.__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--root", default=str(Path.home() / "dev"))
    parser.add_argument("--ledger", default=os.environ.get("AGENTIHOOKS_SWARM", ""))
    parser.add_argument("--session", default=os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
    return parser


def sweep(argv):
    args = build_sweep_parser().parse_args(argv)
    report = trace_sweep.sweep(args.root, apply=args.apply, session_id=args.session, ledger=args.ledger)
    for row in trace_sweep.plan_rows(report):
        print(row)
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "sweep":
        return sweep(argv[1:])
    args = build_parser().parse_args(argv)
    if args.corrections:
        _print_corrections(injection_trace.corrections())
        return 0
    if not args.session:
        print("agentihooks trace: no session id given and CLAUDE_CODE_SESSION_ID is unset", file=sys.stderr)
        return 2
    if args.wrong:
        try:
            row = injection_trace.correct(args.session, args.wrong, args.repo, args.reason, args.quote)
        except ValueError as e:
            print(f"agentihooks trace: {e}", file=sys.stderr)
            return 2
        _print_corrections([row])
        return 0
    rows = injection_trace.trace(args.session)
    for row in rows:
        locator = injection_trace.format_locator(row.get("locator"))
        print("\t".join((row["at"], row["layer"], row["source"], locator, row["text"])))
    sources = {row["source"] for row in rows}
    _print_corrections([row for row in injection_trace.corrections() if row["source"] in sources])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
