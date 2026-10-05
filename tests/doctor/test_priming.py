from scripts.swarm import prompt

WATCHED, DOCTOR = "watch", "watch-doctor"


def master(slug, peer):
    return prompt.build_master(slug, "/repo", f"{slug}-master-1", {"id": "master", "peer": peer})


def test_the_doctor_master_is_primed_with_its_loop():
    text = master(DOCTOR, WATCHED)
    for line in (
        f"agentihooks doctor {WATCHED} verdict",
        f"agentihooks doctor {WATCHED} task",
        f"agentihooks doctor {WATCHED} measure",
        f"agentihooks doctor {WATCHED} intervene",
        "pull-dev",
        "restart-ledger-server",
        "refresh-rules",
        "handoff-at-stop",
        "Never change the watched swarm's tasks",
        "both ledgers",
        "a code fix with a test first",
        "bundle pull request",
        "two hours",
    ):
        assert line in text


def test_a_watched_master_with_a_doctor_peer_gets_no_doctor_loop():
    assert "agentihooks doctor" not in master(WATCHED, DOCTOR)
    assert "agentihooks doctor" not in master(WATCHED, "")
