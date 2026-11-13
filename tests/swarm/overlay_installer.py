"""An installer double that resolves the engineer package role and the bundle overlays one host holds."""

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

from hooks.context.profile_chain import PACKAGE_ROLES


def installer(root: Path, bundle: tuple[str, ...]) -> Callable[[], SimpleNamespace]:
    """An install module factory whose host holds the engineer role and each named overlay wearing it under root."""
    found = {"engineer": PACKAGE_ROLES / "engineer"}
    for name in bundle:
        (root / name).mkdir()
        (root / name / "profile.yml").write_text("kind: overlay\nwears: [engineer]\n")
        found[name] = root / name

    def chain(profile: str) -> list[tuple[str, Path]]:
        """The profile's chain when this host holds it, else nothing."""
        return [(profile, found[profile])] if profile in found else []

    return lambda: SimpleNamespace(_resolve_profile_dir=found.get, _resolve_profile_chain=chain)
