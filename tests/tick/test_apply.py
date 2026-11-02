"""The engine applies the writes steps return through compare and set appliers and records the ones it drops."""

from dataclasses import dataclass, field

from scripts.tick import engine
from scripts.tick.deadline import Deadline
from scripts.tick.ports.records import RevisionConflict
from scripts.tick.step import Context, Domain, Lane, Result, Write
from scripts.tick.steps import actions, actions_of

TASKS = Domain("tasks")


@dataclass
class Store:
    rows: dict[str, tuple[int, object]] = field(default_factory=lambda: {"t1": (1, "unsized")})
    applied: list[Write] = field(default_factory=list)

    def read(self, domain: Domain, key: str) -> object | None:
        return self.rows.get(key)

    def apply(self, write: Write) -> None:
        revision, _ = self.rows[write.key]
        if revision != write.expected:
            raise RevisionConflict(f"tasks/{write.key} moved from revision {write.expected} to {revision}")
        self.rows[write.key] = (revision + 1, write.value)
        self.applied.append(write)


@dataclass
class SizeStep:
    store: Store
    rival: list[object] = field(default_factory=list)
    name: str = "size"
    group: str = "tasks"
    lane: Lane = Lane("main")
    budget_s: float = 5.0
    interval_s: float = 0.0
    needs: frozenset[str] = frozenset()
    reads: frozenset[Domain] = frozenset({TASKS})
    writes: frozenset[Domain] = frozenset({TASKS})

    def run(self, ctx: Context) -> Result:
        found = ctx.view.read(TASKS, "t1")
        assert isinstance(found, tuple)
        revision = found[0]
        for value in self.rival:
            self.store.rows["t1"] = (revision + 1, value)
        self.rival.clear()
        return Result(
            (
                Write(TASKS, "t1", f"M from revision {revision}", expected=revision),
                Write(actions_of(self.name), self.name, [f"sized from revision {revision}"]),
            )
        )


def _pass(store: Store, step: SizeStep) -> tuple[engine.StepRecord, ...]:
    return engine.run_group("tasks", Context(store, Deadline.after(60.0)), [step], apply={TASKS: store.apply})


def test_a_conflicting_write_is_dropped_and_recomputed_on_the_next_pass() -> None:
    store = Store()
    step = SizeStep(store, rival=["L by a concurrent step"])

    (first,) = _pass(store, step)

    dropped = Write(TASKS, "t1", "M from revision 1", expected=1)
    assert first.dropped == (dropped,)
    assert store.applied == []
    assert store.rows == {"t1": (2, "L by a concurrent step")}
    assert actions([first]) == [
        "sized from revision 1",
        "dropped write tasks t1 of step size: tasks/t1 moved from revision 1 to 2; the next pass recomputes it",
    ]

    (second,) = _pass(store, step)

    assert second.dropped == ()
    assert store.applied == [Write(TASKS, "t1", "M from revision 2", expected=2)]
    assert store.rows == {"t1": (3, "M from revision 2")}
    assert actions([second]) == ["sized from revision 2"]


def test_writes_without_an_applier_pass_through_untouched() -> None:
    store = Store()
    step = SizeStep(store)

    (record,) = engine.run_group("tasks", Context(store, Deadline.after(60.0)), [step])

    assert record.dropped == ()
    assert store.applied == []
    assert store.rows == {"t1": (1, "unsized")}
    assert record.result is not None
    assert len(record.result.writes) == 2


def test_a_write_defaults_to_no_expected_revision() -> None:
    assert Write(TASKS, "t1", "S").expected is None
