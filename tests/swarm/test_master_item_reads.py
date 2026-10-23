from pathlib import Path

from scripts.swarm import prompt

ROLE_RULE = Path(__file__).resolve().parents[2] / "profiles/package/roles/master/.claude/rules/master-role.md"
GET = "agentihooks ledger --slug sw get tasks/<id>"
TASKS = "agentihooks ledger --slug sw tasks --state <state> --lane <lane> --text <words>"


def test_master_prompt_reads_one_item_at_a_time_and_forbids_a_full_show():
    text = prompt.build("sw", "/repo", "master", "master", {"id": "master", "title": "Master"})

    assert "read the ledger in full" not in text
    assert GET in text and TASKS in text
    assert "Never run agentihooks ledger --slug sw show" in text


def test_master_role_rule_names_the_item_reads_and_forbids_ledger_show():
    rule = ROLE_RULE.read_text()

    assert "agentihooks ledger --slug <slug> get tasks/<id>" in rule
    assert "agentihooks ledger --slug <slug> tasks --state <state> --lane <lane> --text <words>" in rule
    assert "never `agentihooks ledger --slug <slug> show`" in rule
