from scripts import claude_quota_balancer as balancer
from scripts.claude_quota_balancer import select_credential, select_launch_credential


def cmd_claude(extra_args: list[str]) -> object:
    if extra_args:
        # ruleid: agenti-launch-capped-selector
        return balancer.select_credential({}, sessions={})
    # ruleid: agenti-launch-capped-selector
    return select_credential({}, sessions={})


def cmd_claude_launch(extra_args: list[str]) -> object:
    # ok: agenti-launch-capped-selector
    return select_launch_credential({}, sessions={})


def worker_launch(extra_args: list[str]) -> object:
    # ok: agenti-launch-capped-selector
    return select_credential({}, sessions={})
