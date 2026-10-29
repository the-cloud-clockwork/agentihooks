"""agentihooks timer|stopwatch|cron: schedule a message for the fleet, a swarm, a seat or a session.

agentihooks timer add (--in DURATION | --at TIME) TARGET MESSAGE...
agentihooks timer list | cancel ID
agentihooks stopwatch start --limit DURATION TARGET MESSAGE...
agentihooks stopwatch stop ID | show ID
agentihooks cron add --expr "MIN HOUR DAY MONTH WEEKDAY" [--tz ZONE] TARGET MESSAGE...
agentihooks cron list | remove ID

TARGET is --fleet, --swarm SLUG, --seat SEAT@SLUG or --session NAME. DURATION is seconds or a number with s, m, h or d.
Ids come from the scheduler; the owner is this session: AGENTIHOOKS_AGENT_NAME, else CLAUDE_CODE_SESSION_ID.
"""

import argparse
import json
import sys

from scripts.scheduler import commands


def _targets(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--fleet", dest="target", action="store_const", const=("fleet", ""))
    for target in ("swarm", "seat", "session"):
        group.add_argument(f"--{target}", dest="target", type=lambda name, kind=target: (kind, name))
    parser.add_argument("message", nargs="+")


def _identified(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("id")


def _owner() -> str:
    from scripts.inbox.cli import identity

    return identity()


def _slot(args) -> commands.Slot:
    return commands.target_slot(*args.target)


def _timer_add(scheduler, args):
    when = {"at": commands.moment(args.at)} if args.at else {"delay": commands.seconds(args.delay)}
    return commands.add_timer(scheduler, _owner(), _slot(args), " ".join(args.message), **when)


def _stopwatch_start(scheduler, args):
    return commands.start_stopwatch(
        scheduler, _owner(), _slot(args), " ".join(args.message), limit=commands.seconds(args.limit)
    )


def _cron_add(scheduler, args):
    return commands.add_cron(
        scheduler, _owner(), _slot(args), " ".join(args.message), expression=args.expr, timezone=args.tz
    )


def _timer_commands(sub) -> None:
    add = sub.add_parser("add")
    when = add.add_mutually_exclusive_group(required=True)
    when.add_argument("--in", dest="delay")
    when.add_argument("--at")
    _targets(add)
    add.set_defaults(run=_timer_add)
    sub.add_parser("list").set_defaults(run=lambda scheduler, args: commands.entries(scheduler, "timer"))
    cancel = sub.add_parser("cancel")
    _identified(cancel)
    cancel.set_defaults(run=lambda scheduler, args: commands.cancel(scheduler, "timer", args.id))


def _stopwatch_commands(sub) -> None:
    start = sub.add_parser("start")
    start.add_argument("--limit", required=True)
    _targets(start)
    start.set_defaults(run=_stopwatch_start)
    stop = sub.add_parser("stop")
    _identified(stop)
    stop.set_defaults(run=lambda scheduler, args: commands.stop_stopwatch(scheduler, args.id))
    show = sub.add_parser("show")
    _identified(show)
    show.set_defaults(run=lambda scheduler, args: commands.show_stopwatch(scheduler, args.id))


def _cron_commands(sub) -> None:
    add = sub.add_parser("add")
    add.add_argument("--expr", required=True)
    add.add_argument("--tz", default="UTC")
    _targets(add)
    add.set_defaults(run=_cron_add)
    sub.add_parser("list").set_defaults(run=lambda scheduler, args: commands.entries(scheduler, "cron"))
    remove = sub.add_parser("remove")
    _identified(remove)
    remove.set_defaults(run=lambda scheduler, args: commands.cancel(scheduler, "cron", args.id))


NOUNS = {"timer": _timer_commands, "stopwatch": _stopwatch_commands, "cron": _cron_commands}


def _parser(noun: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"agentihooks {noun}", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    NOUNS[noun](parser.add_subparsers(dest="action", required=True))
    return parser


def _main(noun: str, argv: list[str] | None) -> int:
    from scripts.inbox.store import InboxError

    args = _parser(noun).parse_args(argv)
    try:
        print(json.dumps(args.run(commands.connect(), args)))
    except (commands.CommandError, InboxError) as exc:
        print(f"agentihooks {noun}: {exc}", file=sys.stderr)
        return 1
    return 0


def timer(argv: list[str] | None = None) -> int:
    return _main("timer", argv)


def stopwatch(argv: list[str] | None = None) -> int:
    return _main("stopwatch", argv)


def cron(argv: list[str] | None = None) -> int:
    return _main("cron", argv)
