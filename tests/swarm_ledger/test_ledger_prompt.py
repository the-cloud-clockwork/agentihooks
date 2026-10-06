from scripts.swarm_ledger import ledger


def test_the_join_paragraph_sends_swarm_members_to_the_inbox_and_others_to_a_watch(capsys):
    ledger.cmd_prompt(ledger.build_parser().parse_args(["--slug", "demo", "--as", "a1", "prompt"]))
    out = capsys.readouterr().out
    assert out.startswith(
        "You are a member of crew ledger `demo` as `a1`. Run once: agentihooks ledger --slug demo --as a1 join. "
        "In a swarm, operator writes reach you as inbox messages at your next tool call and the swarm wakes you "
        "when idle; a ledger without a swarm needs a Monitor on: agentihooks ledger watch demo --as a1. Act on "
        "every OPERATOR line, then run `agentihooks ledger --slug demo --as a1 ack`."
    )
