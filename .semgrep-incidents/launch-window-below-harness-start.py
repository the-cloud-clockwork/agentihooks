import argparse

# ruleid: launch-window-below-harness-start
ROUTE_TIMEOUT_S = 30

# ok: launch-window-below-harness-start
LAUNCH_TIMEOUT_S = 300

CANARY_TIMEOUT_S = {
    # ok: launch-window-below-harness-start
    "claude": 120.0,
    # ruleid: launch-window-below-harness-start
    "codex": 45.0,
}


def argv(agent, budget):
    return [
        "init-agent",
        "--route-timeout",
        # ruleid: launch-window-below-harness-start
        "30" if agent == "codex" else "90",
        "--canary-timeout",
        # ok: launch-window-below-harness-start
        str(budget.canary),
    ]


def plain(agent):
    # ruleid: launch-window-below-harness-start
    return ["init-agent", "--canary-timeout", "60"]


def parser():
    found = argparse.ArgumentParser()
    # ruleid: launch-window-below-harness-start
    found.add_argument("--route-timeout", type=float, default=30)
    # ok: launch-window-below-harness-start
    found.add_argument("--canary-timeout", type=float, default=None)
    return found
