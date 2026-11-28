from collections.abc import Callable
from pathlib import Path


def shared(home: Path, ordinary: Path) -> None:
    # ruleid: native-default-shared-login
    (home / "auth.json").symlink_to(ordinary / "auth.json")
    # ruleid: native-default-shared-login
    (home / ".credentials.json").symlink_to(ordinary / ".credentials.json")


def isolated(home: Path, ordinary: Path, copy: Callable[[Path, Path], object]) -> None:
    # ok: native-default-shared-login
    copy(ordinary / "auth.json", home / "auth.json")
    # ok: native-default-shared-login
    copy(ordinary / ".credentials.json", home / ".credentials.json")


def stripped_route(
    route: str, environ: dict[str, str], forced: Callable[..., object], _route: Callable[..., object]
) -> None:
    # ruleid: native-named-route-stripped
    forced(environ, route.removeprefix("named:"))
    # ruleid: native-named-route-stripped
    _route(environ, route.removeprefix("named:"), None, "")


def preserved_route(
    route: str, environ: dict[str, str], forced: Callable[..., object], _route: Callable[..., object]
) -> None:
    # ok: native-named-route-stripped
    forced(environ, route)
    # ok: native-named-route-stripped
    _route(environ, route, None, "")


def empty_reference(route: str, select: Callable[..., object], prefix: str) -> None:
    reference = route.removeprefix("named:").removeprefix(prefix)
    # ruleid: native-named-route-empty
    select({}, {}, {}, 0, reference, {})


def checked_reference(route: str, select: Callable[..., object], prefix: str) -> None:
    reference = route.removeprefix("named:").removeprefix(prefix)
    if not reference:
        raise ValueError("empty reference")
    # ok: native-named-route-empty
    select({}, {}, {}, 0, reference, {})
