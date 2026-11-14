from scripts.swarm import launch_check


def unowned(store: object, slug: str, agent: object, now: int) -> None:
    # ruleid: hub-launch-check-without-owner
    launch_check.begin(store, slug, agent, now)


def unowned_relaunch(store: object, slug: str, agent: object, now: int) -> None:
    # ruleid: hub-launch-check-without-owner
    launch_check.begin(store, slug, agent, now, relaunch=False)


def owned(store: object, slug: str, agent: object, now: int) -> None:
    # ok: hub-launch-check-without-owner
    launch_check.begin(store, slug, agent, now, owner=launch_check.HUB)
