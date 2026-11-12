import pytest

from scripts.quick import guard, layout

pytestmark = pytest.mark.unit


@pytest.mark.feature("quick-swarm")
@pytest.mark.parametrize(
    ("binary", "environ", "running", "message"),
    [
        (None, {"HERDR_PANE_ID": "w1:p1"}, True, guard.NOT_INSTALLED),
        ("/bin/herdr", {}, True, guard.NOT_INSIDE),
        ("/bin/herdr", {"HERDR_PANE_ID": "w1:p1"}, False, guard.NOT_RUNNING),
    ],
)
def test_the_quick_swarm_refuses_loudly_outside_a_live_herdr_pane(binary, environ, running, message):
    with pytest.raises(guard.QuickError) as refused:
        guard.require(environ, binary=lambda: binary, running=lambda env: running)
    assert str(refused.value) == message


def test_inside_a_live_herdr_pane_the_caller_pane_is_returned():
    found = guard.require({"HERDR_PANE_ID": "w1:p1"}, binary=lambda: "/bin/herdr", running=lambda env: True)
    assert found == "w1:p1"


def test_the_refusals_name_herdr_and_what_to_do():
    assert "herdr is not installed" in guard.NOT_INSTALLED
    assert "herdr pane" in guard.NOT_INSIDE
    assert "herdr server" in guard.NOT_RUNNING


class FakePanes:
    def __init__(self, width: int) -> None:
        self.rects = {"w1:p1": (0, width)}
        self.splits: list[str] = []

    def split(self, pane_id: str) -> str:
        x, width = self.rects[pane_id]
        new = f"w1:p{len(self.rects) + 1}"
        self.rects[pane_id] = (x, width // 2)
        self.rects[new] = (x + width // 2, width - width // 2)
        self.splits.append(pane_id)
        return new

    def columns(self, pane_id: str) -> dict[str, tuple[int, int]]:
        return dict(self.rects)


@pytest.mark.feature("quick-swarm")
def test_four_agents_fill_four_equal_columns_with_the_caller_first():
    panes = FakePanes(160)
    ordered = layout.build("w1:p1", 4, panes)
    assert ordered == ["w1:p1", "w1:p3", "w1:p2", "w1:p4"]
    assert [panes.rects[pane] for pane in ordered] == [(0, 40), (40, 40), (80, 40), (120, 40)]
    assert panes.splits == ["w1:p1", "w1:p1", "w1:p2"]


@pytest.mark.parametrize("count", [2, 3, 5])
def test_every_count_orders_panes_left_to_right_from_the_caller(count):
    panes = FakePanes(200)
    ordered = layout.build("w1:p1", count, panes)
    assert len(ordered) == count and ordered[0] == "w1:p1"
    assert [panes.rects[pane][0] for pane in ordered] == sorted(panes.rects[pane][0] for pane in ordered)


def test_panes_outside_the_quick_swarm_are_never_split_or_ordered():
    panes = FakePanes(160)
    panes.rects["w1:p9"] = (500, 400)
    ordered = layout.build("w1:p1", 2, panes)
    assert ordered == ["w1:p1", "w1:p2"] and "w1:p9" not in panes.splits


@pytest.mark.feature("quick-swarm")
def test_the_caller_stays_first_when_other_panes_sit_left_of_it():
    panes = FakePanes(160)
    panes.rects = {"w1:p0": (0, 100), "w1:p1": (100, 160)}
    ordered = layout.build("w1:p1", 3, panes)
    assert ordered[0] == "w1:p1" and "w1:p0" not in ordered
    assert [panes.rects[pane][0] for pane in ordered[1:]] == sorted(panes.rects[pane][0] for pane in ordered[1:])


@pytest.mark.feature("quick-swarm")
def test_seats_are_the_master_then_numbered_engineers():
    assert layout.DEFAULT_AGENTS == 4 and layout.MAX_AGENTS == 5
    assert layout.seats(4) == ["master", "eng-1", "eng-2", "eng-3"]
    assert layout.seats(5)[-1] == "eng-4"


@pytest.mark.parametrize("count", [0, 1, 6])
def test_a_count_outside_two_to_five_is_refused(count):
    with pytest.raises(guard.QuickError, match="2 to 5 agents"):
        layout.seats(count)
    with pytest.raises(guard.QuickError):
        layout.build("w1:p1", count, FakePanes(100))
