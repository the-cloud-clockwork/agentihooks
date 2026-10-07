from scripts.gates import modes
from scripts.gates.claims import CAP, GATE, refusal
from scripts.swarm_ledger.ledger_comments import problems


def test_the_cap_is_three_lives_and_ships_enforcing():
    assert (CAP, GATE.name, GATE.default_mode) == (3, "claims", "enforce")
    assert modes.env_name(GATE.name) == "AGENTIHOOKS_GATE_CLAIMS"


def test_the_summary_names_the_lives_the_cap_the_last_handoff_and_launch_failure_and_the_way_out():
    assert refusal(3, "recycle", "herdr down", "rig-grade-swarm", "g10") == (
        "The swarm blocked this task before a fourth agent life: claimed 3 times, cap 3. "
        "Last handoff reason: recycle. Last launch failure: herdr down. Change its scope or split it, then reopen it for three more lives with "
        "agentihooks ledger --slug rig-grade-swarm task set g10 state=open"
    )


def test_the_summary_passes_the_ledger_comment_check():
    for slug, task in (("rig-grade-swarm", "g10"), ("proof-323133-g10-1", "t1"), ("sw", "lg1")):
        assert problems(refusal(13, "succession", "none", slug, task), "comment") == []
