"""The paused swarm config a swarm create command asks for, built before any ledger is marked."""

import argparse
import os
from pathlib import Path
from typing import Any, cast

from scripts.swarm import templates
from scripts.swarm.store import APP_SERVER_TRANSPORT, DEFAULT_MASTERS_STEP, DELEGATE, EVENTS_ENGINE, SwarmConfig
from scripts.swarm_ledger import ledger_creator


def config(args: argparse.Namespace, repo: str, template: templates.Template) -> SwarmConfig:
    """The paused swarm create ARGS asks for in REPO, with caps from TEMPLATE unless a flag pins them."""
    from scripts.hive.daemon import origin

    lanes = cast(dict[str, templates.Lane], template.lanes)  # pragma: no mutate
    lane_fields: dict[str, dict[str, Any]] = templates.lane_map(template)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    caps = {key: lane.cap for key, lane in lanes.items()}
    return SwarmConfig(
        args.slug,
        repo,
        caps["eng"] if args.max_eng_agents is None else args.max_eng_agents,
        caps["ci"] if args.max_ci_agents is None else args.max_ci_agents,
        max_plan=caps["plan"] if args.max_plan_agents is None else args.max_plan_agents,
        state="paused",
        compact_limit=template.compact_limit,
        template=args.template,
        lanes=lane_fields,
        links=cast(list[Any], template.links),  # pragma: no mutate
        autonomy=template.autonomy or DELEGATE,
        scratch=not ledger_creator.shared(os.environ),  # pyright: ignore[reportUnknownMemberType]
        caps_pinned=args.max_eng_agents is not None or args.max_ci_agents is not None,
        codex_inbox_transport=APP_SERVER_TRANSPORT,
        masters_step=DEFAULT_MASTERS_STEP,
        engine=EVENTS_ENGINE,
        repo_origin=origin(Path(repo)),
    )
