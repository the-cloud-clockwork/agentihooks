"""The quick swarm runs only inside a herdr pane on a live herdr server."""

from collections.abc import Callable, Mapping

from scripts import herdr_host

NOT_INSTALLED = "herdr is not installed: the quick swarm runs only inside herdr"
NOT_INSIDE = "this session is not inside a herdr pane: start the quick swarm from a herdr pane"
NOT_RUNNING = "the herdr server does not answer: start herdr and run the quick swarm from one of its panes"


class QuickError(RuntimeError):
    """A quick swarm command that cannot run, carrying the reason the operator reads."""


def require(
    environ: Mapping[str, str],
    binary: Callable[[], str | None] = herdr_host.binary,
    running: Callable[[dict[str, str]], bool] = herdr_host.server_running,
) -> str:
    """The caller's herdr pane id; refuses loudly when herdr is missing, the caller is outside it or it is down."""
    if binary() is None:
        raise QuickError(NOT_INSTALLED)
    pane = environ.get("HERDR_PANE_ID", "")
    if not pane:
        raise QuickError(NOT_INSIDE)
    if not running(dict(environ)):
        raise QuickError(NOT_RUNNING)
    return pane
