import importlib
from typing import Any

import pytest


@pytest.mark.feature("swarm-progress-data")
def test_progress_data_preserves_every_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    progress_data = importlib.import_module("scripts.swarm.progress_data")
    names = ("tasks", "freezes", "plans", "phases", "followups", "alerts")
    expected = {name: [{"name": name, "value": index}] for index, name in enumerate(names)}
    calls: list[tuple[str, str, bool]] = []

    def resource(slug: str, path: str, collection: bool = False) -> list[dict[str, Any]]:
        calls.append((slug, path, collection))
        return expected[path]

    monkeypatch.setattr(progress_data, "resource", resource)
    assert progress_data.read("example-swarm") == expected
    assert calls == [("example-swarm", name, True) for name in names]


@pytest.mark.feature("swarm-progress-data")
def test_progress_data_propagates_read_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    progress_data = importlib.import_module("scripts.swarm.progress_data")
    failure = RuntimeError("resource unavailable")

    def resource(slug: str, path: str, collection: bool = False) -> list[dict[str, Any]]:
        raise failure

    monkeypatch.setattr(progress_data, "resource", resource)
    with pytest.raises(RuntimeError) as caught:
        progress_data.read("example-swarm")
    assert caught.value is failure
