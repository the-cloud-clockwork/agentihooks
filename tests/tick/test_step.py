from importlib import import_module


def test_contract_values_and_context() -> None:
    module = import_module("scripts.tick.step")
    deadline_module = import_module("scripts.tick.deadline")

    class Snapshot:
        def read(self, domain: str, key: str) -> object | None:
            return {("agents", "one"): "running"}.get((domain, key))

    domain = module.Domain("agents")
    lane = module.Lane("local")
    write = module.Write(domain, "one", "done")
    result = module.Result((write,))
    view = Snapshot()
    deadline = deadline_module.Deadline.after(5)
    context = module.Context(view, deadline)
    assert domain == "agents"
    assert lane == "local"
    assert write.domain == domain
    assert write.key == "one"
    assert write.value == "done"
    assert result.writes == (write,)
    assert module.Result().writes == ()
    assert context.view is view
    assert context.deadline is deadline
    assert context.view.read(domain, "one") == "running"
    assert context.view.read(domain, "missing") is None
