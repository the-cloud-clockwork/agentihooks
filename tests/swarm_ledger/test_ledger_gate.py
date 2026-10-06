import ledger_gate
import ledger_hook


def test_a_relay_counts_as_ledger_progress():
    assert "relay" in ledger_gate.WRITE_COMMANDS
    assert ledger_hook.touched('agentihooks ledger --slug s --as m relay questions/q "Use it." --quote "use it"')
    assert not ledger_hook.touched("agentihooks ledger --slug s --as m status")
