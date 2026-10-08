import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_agent_ops  # noqa: E402
import ledger_core as core  # noqa: E402
import ledger_gate  # noqa: E402
import new_ledger  # noqa: E402

from tests.swarm_ledger import legacy_page  # noqa: E402

SLUG = "atsign-2026-01-01"
NAME = "engineer@a1b2c3-0002"


def make_ledger():
    content = {"title": "Demo", "overview": "o", "sources": [], "phases": [{"title": "one", "description": "d"}]}
    html_path, json_path = core.paths(SLUG)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    core.sync(SLUG)


def test_a_name_with_the_at_sign_joins_the_ledger_crew():
    assert ledger_agent_ops.AUTHOR_RE.match(NAME)
    make_ledger()
    state, rejected = core.sync(SLUG, ops=[{"op": "join", "id": "j-at", "by": NAME}])
    assert not rejected
    assert NAME in state["_meta"]["members"]


def test_a_chat_mention_carries_the_whole_name():
    assert ledger_gate.MENTION_RE.match(f"@{NAME} look").group(1) == NAME
