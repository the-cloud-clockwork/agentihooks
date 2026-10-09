from scripts.swarm import test_signatures

TEST = "tests/swarm/test_outbox.py::test_spool_survives"


def test_the_same_failure_under_different_temporary_paths_shares_a_signature():
    first = "AssertionError: assert '/tmp/pytest-of-runner/pytest-3/test_spool0/a.sqlite' == 'x'"
    second = "AssertionError: assert '/tmp/pytest-of-runner/pytest-17/test_spool2/a.sqlite' == 'x'"
    assert test_signatures.signature(TEST, first) == test_signatures.signature(TEST, second)


def test_a_different_exception_gets_a_different_signature():
    assertion = "AssertionError: assert '/tmp/pytest-3/a.sqlite' == 'x'"
    key_error = "KeyError: '/tmp/pytest-3/a.sqlite'"
    assert test_signatures.signature(TEST, assertion) != test_signatures.signature(TEST, key_error)


def test_the_same_message_on_another_test_gets_another_signature():
    message = "AssertionError: assert 1 == 2"
    assert test_signatures.signature(TEST, message) != test_signatures.signature(TEST + "_again", message)


def test_numbers_hashes_paths_and_addresses_are_removed():
    text = (
        "ValueError: <Box object at 0x7f3a9c2b1d40> run 37980794838 head e5c54e6f9a"
        " id 3f2b8c1e-9d4a-4e6b-8f7c-2a1b3c4d5e6f took 1.25s at /home/runner/work/a/b.py:12"
    )
    assert test_signatures.normalize(text) == (
        "ValueError: <Box object at <addr>> run <n> head <hash> id <uuid> took <n>s at <path>"
    )


def test_relative_test_paths_and_words_are_kept():
    assert (
        test_signatures.normalize("tests/swarm/test_a.py   failed  badcafe") == "tests/swarm/test_a.py failed badcafe"
    )


def test_a_signature_is_a_short_stable_hex_digest():
    value = test_signatures.signature(TEST, "AssertionError: assert 1 == 2")
    assert value == test_signatures.signature(TEST, "AssertionError: assert 7 == 9")
    assert len(value) == 16
    int(value, 16)
