"""Delivery requirements summary, shown at every agent start and at the end of agentihooks init.

The summary names the code review file limit (AGENTIHOOKS_CODEREVIEW_FILELIMIT) above which one change may be split in
two pull requests, and the rule that agents deliver or offer the source root fix, never an unasked workaround.
"""

from hooks.common import inject_context


def render(limit: int) -> str:
    """The summary, at most fifty words, for a code review file limit of limit files."""
    return (
        "Delivery requirements: deliver each change as one complete pull request. "
        f"Split it in two only when it changes more than {limit} files; never split smaller for size alone. "
        "Always deliver or offer the source root fix, never a workaround the user did not ask for."
    )


def summary() -> str:
    """The summary for the code review file limit configured now."""
    from hooks import config

    return render(config.CODEREVIEW_FILELIMIT)


def inject_on_session_start() -> None:
    """Hand the summary to the session start context of every agent, ranked with the enforcements."""
    inject_context(summary(), kind="enforcement")
