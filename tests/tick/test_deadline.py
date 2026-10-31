from importlib import import_module


def test_deadline_uses_injected_monotonic_clock_and_clamps_remaining() -> None:
    module = import_module("scripts.tick.deadline")
    now = [10.0]
    deadline = module.Deadline.after(5.0, clock=lambda: now[0])
    assert deadline.expires_at == 15.0
    assert deadline.remaining == 5.0
    assert deadline.expired is False
    now[0] = 15.0
    assert deadline.remaining == 0.0
    assert deadline.expired is True
    now[0] = 18.0
    assert deadline.remaining == 0.0


def test_child_cannot_extend_parent_deadline() -> None:
    module = import_module("scripts.tick.deadline")
    now = [10.0]
    parent = module.Deadline.after(5.0, clock=lambda: now[0])
    child = parent.child(2.0)
    assert child.expires_at == 12.0
    assert parent.child(20.0).expires_at == 15.0
    now[0] = 11.0
    assert child.remaining == 1.0
    assert parent.remaining == 4.0


def test_default_clock_and_zero_budget() -> None:
    module = import_module("scripts.tick.deadline")
    assert module.Deadline.after(0).expired is True
    assert module.Deadline.after(-1).remaining == 0.0
