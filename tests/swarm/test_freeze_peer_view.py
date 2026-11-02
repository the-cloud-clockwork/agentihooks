import urllib.error
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from scripts.scheduler import service
from scripts.swarm import freeze, ledger_client
from scripts.swarm_ledger.api import client as api
from scripts.tick import view


@pytest.mark.feature("swarm-tick-delta-reads")
@pytest.mark.parametrize("service_mode", [False, True])
@pytest.mark.parametrize(
    ("target", "label"),
    [("kind:research", "research tasks"), ("plans/a", "plan Alpha"), ("slices/s", "slice Slice")],
)
def test_peer_changes_use_a_retained_view_and_restore_the_swarm_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, service_mode: bool, target: str, label: str
) -> None:
    client = ledger_client.LedgerClient(service=service_mode)
    store = SimpleNamespace(
        config=lambda slug: SimpleNamespace(template=freeze.priming.TEMPLATE),
        peer=lambda slug: "watched",
    )
    endpoint = SimpleNamespace(
        base=lambda slug: f"http://ledger/{slug}",
        credentials=lambda slug, service: {"X-Peer": f"{slug} {service}"},
        REQUEST_TIMEOUT=4,
    )
    requests: list[tuple[str, dict[str, str], float, str, int, tuple[str, ...]]] = []
    whole: list[str] = []
    generation = 1
    focus: list[dict[str, str]] = []

    def changes(self: api.ResourceClient, slug: str, since: int, parts: tuple[str, ...]) -> dict[str, Any]:
        requests.append((self.base, self.credentials, self.timeout, slug, since, parts))
        if since == 0:
            data = {
                "freezes": focus,
                "plans": [{"id": "a", "title": "Alpha"}],
                "slices": [{"id": "s", "title": "Slice"}],
            }
            return {"generation": generation, "data": {part: data[part] for part in parts if part in data}}
        updates = [[["freezes"], focus]] if since != generation else []
        return {"generation": generation, "changes": {"set": updates, "drop": [], "lists": {}}}

    monkeypatch.setattr(ledger_client, "_ledger", lambda: endpoint)
    monkeypatch.setattr(service, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(api.ResourceClient, "changes", changes)
    monkeypatch.setattr(client, "state", lambda slug: whole.append(slug) or {"freezes": focus})
    own = {"tasks": []}
    own_view = view.LedgerView(8, own)
    own_refresh = ("doctor", lambda: own_view)
    with view.holding():
        view.CURRENT.set(own_view)
        view.REFRESH.set(own_refresh)
        first = freeze.watched("doctor", cast(Any, store), client, own)
        assert first == {"tasks": [], freeze.WATCHED: []}
        assert whole == []
        assert requests[0] == (
            "http://ledger/watched",
            {"X-Peer": f"watched {service_mode}"},
            4,
            "watched",
            0,
            view.PARTS,
        )
        assert view.CURRENT.get() is own_view
        assert view.REFRESH.get() is own_refresh
        requests.clear()
        unchanged = freeze.watched("doctor", cast(Any, store), client, own)
        assert unchanged == first
        assert requests
        assert all(request[4] == 1 for request in requests)
        assert whole == []
        generation, focus = 2, [{"verb": "focus", "target": target}]
        changed = freeze.watched("doctor", cast(Any, store), client, own)
        assert changed[freeze.WATCHED] == [f"the focus on {label} in the watched swarm"]
        assert freeze.outside_watched({"kind": "code", "phase": "ordinary"}, changed, "fixes")
        generation, focus = 3, []
        cleared = freeze.watched("doctor", cast(Any, store), client, own)
        assert not freeze.outside_watched({"kind": "code", "phase": "ordinary"}, cleared, "fixes")
        assert whole == []
        assert view.CURRENT.get() is own_view
        assert view.REFRESH.get() is own_refresh
    assert own == {"tasks": []}


@pytest.mark.parametrize("status", [400, 404, 503])
def test_a_refused_peer_view_preserves_the_swarm_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:

    client = ledger_client.LedgerClient(service=True)
    store = SimpleNamespace(
        config=lambda slug: SimpleNamespace(template=freeze.priming.TEMPLATE),
        peer=lambda slug: "watched",
    )
    endpoint = SimpleNamespace(
        base=lambda slug: "http://ledger", credentials=lambda slug, service: {}, REQUEST_TIMEOUT=4
    )
    whole: list[str] = []

    def changes(self: api.ResourceClient, slug: str, since: int, parts: tuple[str, ...]) -> dict[str, Any]:
        raise urllib.error.HTTPError("http://ledger", status, "refused", Message(), None)

    monkeypatch.setattr(ledger_client, "_ledger", lambda: endpoint)
    monkeypatch.setattr(service, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(api.ResourceClient, "changes", changes)
    monkeypatch.setattr(client, "state", lambda slug: whole.append(slug) or {"freezes": []})
    own: dict[str, Any] = {"tasks": []}
    own_view = view.LedgerView(8, own)
    own_refresh = ("doctor", lambda: own_view)
    with view.holding():
        view.CURRENT.set(own_view)
        view.REFRESH.set(own_refresh)
        actual = freeze.watched("doctor", cast(Any, store), client, own)
        if status == 400:
            assert actual == {"tasks": [], freeze.WATCHED: []}
            assert whole == ["watched"]
        else:
            assert actual is own
            assert whole == []
        assert view.CURRENT.get() is own_view
        assert view.REFRESH.get() is own_refresh
