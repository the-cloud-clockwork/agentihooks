import os


def test_a_leaks_a_setting():
    os.environ["AGENTIHOOKS_PLANTED_LEAK"] = "1"


def test_b_needs_a_clean_environment():
    assert "AGENTIHOOKS_PLANTED_LEAK" not in os.environ
