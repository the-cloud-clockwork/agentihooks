import json

import pytest

from hooks.observability import agent_trace
from hooks.observability.transcript import mask_value

SENTINEL = "QASENTINEL" + "tokvalue99"


@pytest.mark.parametrize("key", ["token", "secret_key", "auth_token", "x-api-key", "passwd"])
def test_nested_secret_key_names_are_masked(key):
    masked = mask_value({"input": {key: SENTINEL}})
    assert SENTINEL not in json.dumps(masked)
    assert masked == {"input": {key: "[REDACTED:generic_secret]"}}


@pytest.mark.parametrize("key", ["token", "x-api-key"])
def test_values_nested_under_secret_key_names_are_masked(key):
    assert mask_value({key: {"value": SENTINEL}}) == {key: {"value": "[REDACTED:generic_secret]"}}


@pytest.mark.parametrize("key", ["max_tokens", "tokenizer", "input_tokens"])
def test_token_counts_keep_their_values(key):
    assert mask_value({key: "literal-value-123"}) == {key: "literal-value-123"}


def test_nested_bearer_header_is_masked():
    header = "Be" + "arer " + SENTINEL + "abcdefghij"
    masked = mask_value({"input": {"headers": {"Authorization": header}}})
    assert SENTINEL not in json.dumps(masked)


def test_passwd_assignment_is_masked():
    assert SENTINEL not in mask_value("run with passwd=" + SENTINEL)


def test_quoted_password_in_embedded_json_is_masked():
    text = 'the config was {"password": "' + SENTINEL + '"} last time'
    assert SENTINEL not in mask_value(text)


def test_etc_passwd_output_is_kept():
    text = "/etc/passwd:root:x:0:0:root:/root:/bin/bash"
    assert mask_value(text) == text


def test_private_key_body_is_masked_through_its_end_line():
    pem = "-----BEGIN RSA PRIV" + "ATE KEY-----\nQASENTINELpem\n-----END RSA PRIV" + "ATE KEY-----\nafter"
    masked = mask_value(pem)
    assert "QASENTINELpem" not in masked
    assert masked == "[REDACTED:private_key]\nafter"


def test_unterminated_private_key_body_is_masked():
    pem = "-----BEGIN RSA PRIV" + "ATE KEY-----\nQASENTINELpem"
    assert "QASENTINELpem" not in mask_value(pem)


def test_failure_log_reason_is_redacted(capsys):
    agent_trace._log_failure(None, "ValueError: Authorization: Bearer " + SENTINEL + "abcdefghij")
    assert SENTINEL not in capsys.readouterr().err
