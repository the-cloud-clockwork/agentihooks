from pathlib import Path

import pytest

from scripts.quick import paths, registry, seeds
from scripts.quick.member import Member

pytestmark = pytest.mark.unit


@pytest.mark.feature("quick-native-authentication")
def test_master_context_exposes_each_safe_authentication_selection(tmp_path: Path) -> None:
    master = registry.Seat(1, "master", "w1:p1", "claude", "quick-proof-master")
    swarm = registry.Swarm(
        "quick-proof",
        str(tmp_path),
        (
            master,
            registry.Seat(2, "eng-1", "w1:p2", "claude", "quick-proof-eng-1", auth="default"),
            registry.Seat(3, "eng-2", "w1:p3", "codex", "quick-proof-eng-2", auth="named", account="alpha"),
            registry.Seat(4, "eng-3", "w1:p4", "claude", "quick-proof-eng-3"),
        ),
    )
    lines = seeds.opening(Member(tmp_path, swarm, master)).splitlines()
    for name, position, harness, choice, tiers in (
        ("eng-1", 2, "claude", "default", "outside automatic tiers"),
        ("eng-2", 3, "codex", "named:alpha", "outside automatic tiers"),
        ("eng-3", 4, "claude", "routed", "automatic tiers"),
    ):
        assert (
            f"- {name} in pane {position} ({harness}, session quick-proof-{name}); "
            f"seat folder {paths.seat_dir(tmp_path, name)}; authentication {choice}, {tiers}"
        ) in lines
