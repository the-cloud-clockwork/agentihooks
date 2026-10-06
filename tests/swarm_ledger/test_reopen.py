import re

from tests.swarm_ledger.test_close import SLUG, apply, make_ledger


def test_reopen_clears_closed_time_preserves_summary_and_survives_sync():
    import ledger_core as core

    make_ledger()
    state, _ = apply({"op": "summary_set", "id": "s", "by": "swarm", "note": "The work stopped here."})
    overview = state["overview"]
    apply({"op": "close", "id": "c", "by": "swarm"})
    state, rejected = apply({"op": "reopen", "id": "r", "by": "operator"})
    assert rejected == []
    assert state["closed_at"] is None
    assert state["overview"] == overview
    assert state["_meta"]["events"][-1]["kind"] == "ledger reopened"
    assert core.sync(SLUG)[0]["closed_at"] is None


def test_page_and_home_reopen_route_to_the_same_swarm_command():
    import ledger_server as server

    assert server.control_argv({"action": "reopen"}) == ["reopen"]
    make_ledger()
    apply({"op": "close", "id": "c", "by": "swarm"})
    page = server.index_page()
    assert "<h1>CLOSED</h1>" not in page
    assert f'data-act="reopen" data-slug="{SLUG}"' in page
    assert ">Reopen</button>" in page


def test_reopen_buttons_call_the_existing_authenticated_swarm_endpoint():
    import json
    import subprocess

    import ledger_server as server

    page = (server.CODE_DIR / "template.html").read_text()
    assert 'id="closed-label"' in page
    assert 'data-swarm="reopen"' in page
    assert '$("closed-label").textContent = closedText(doc.closed_at)' in page
    assert '$("closed-banner").addEventListener("click", (event) => {' in page
    assert '$("closed-banner").querySelectorAll("button[data-swarm]")' in page
    home = server.HOME_PAGE.read_text(encoding="utf-8")
    script = re.search(r"<script>(async function reopenLedger.*?)</script>", home, re.S).group(1)
    probe = (
        """
const calls=[];
global.document={addEventListener(){}};
global.DOMParser=class {parseFromString(){return {querySelector(){return {content:'ledger-token'}}}}};
global.fetch=async (url,options)=>{calls.push([url,options]);return {ok:true,text:async()=>'<html>'}};
"""
        + script
        + "\nreopenLedger({dataset:{slug:'sw'}}).then(()=>process.stdout.write(JSON.stringify(calls)));"
    )
    result = subprocess.run(["node", "-e", probe], capture_output=True, text=True, check=True)
    calls = json.loads(result.stdout)
    assert calls[0][0] == "/sw"
    assert calls[1][0] == "/api/swarm/sw"
    assert calls[1][1]["method"] == "PUT"
    assert calls[1][1]["headers"]["X-Ledger-Token"] == "ledger-token"
    assert json.loads(calls[1][1]["body"]) == {"action": "reopen"}
