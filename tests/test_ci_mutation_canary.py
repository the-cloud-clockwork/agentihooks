from hooks.ci_mutation_canary import required_value


def test_required_value():
    assert required_value() == 7
