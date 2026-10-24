import io
import json
import urllib.error

import pytest

from scripts import openbao_kv
from tests.codex_fakes import BAO_TOKEN, BAO_URL, FakeBao, http_error


def _bao(fake):
    return openbao_kv.OpenBao(BAO_URL, BAO_TOKEN, opener=fake)


def test_a_missing_secret_reads_as_version_zero_and_is_created_with_cas_zero():
    fake = FakeBao()
    assert openbao_kv.read(_bao(fake), openbao_kv.CODEX_CREDS) == ({}, 0)
    assert openbao_kv.merge(_bao(fake), openbao_kv.CODEX_CREDS, {"A": "1"}) == 1
    assert (fake.data, fake.version) == ({"A": "1"}, 1)


def test_a_merge_keeps_other_keys_drops_the_stale_ones_and_writes_at_the_read_version():
    fake = FakeBao({"KEEP": "k", "OLD": "o", "A": "0"}, version=3)
    assert openbao_kv.merge(_bao(fake), openbao_kv.CODEX_CREDS, {"A": "1", "B": "2"}, stale=("OLD",)) == 3
    assert fake.data == {"KEEP": "k", "A": "1", "B": "2"}
    assert fake.version == 4


def test_a_check_and_set_conflict_rereads_and_retries_up_to_three_times():
    fake = FakeBao({"KEEP": "k"}, version=1, conflicts=3)
    assert openbao_kv.merge(_bao(fake), openbao_kv.CODEX_CREDS, {"A": "1"}) == 2
    assert len(fake.writes) == 4
    fake = FakeBao({"KEEP": "k"}, version=1, conflicts=4)
    with pytest.raises(openbao_kv.OpenBaoError):
        openbao_kv.merge(_bao(fake), openbao_kv.CODEX_CREDS, {"A": "1"})
    assert len(fake.writes) == 4


def test_create_writes_only_a_missing_record():
    fake = FakeBao()
    assert openbao_kv.create(_bao(fake), openbao_kv.CODEX_CREDS, {"A": "1"}) is True
    assert (fake.data, fake.version) == ({"A": "1"}, 1)
    assert json.loads(fake.writes[0].data) == {"options": {"cas": 0}, "data": {"A": "1"}}


def test_create_reports_a_record_another_writer_created_first():
    fake = FakeBao(conflicts=1)
    assert openbao_kv.create(_bao(fake), openbao_kv.CODEX_CREDS, {"A": "1"}) is False
    assert fake.data == {} and len(fake.writes) == 1


def test_create_raises_a_failure_that_is_not_a_conflict():
    with pytest.raises(openbao_kv.OpenBaoError) as raised:
        openbao_kv.create(_bao(FakeBao(fail_writes=True)), openbao_kv.CODEX_CREDS, {"A": "1"})
    assert raised.value.status == 503


def test_a_response_that_is_not_json_raises_an_openbao_error():
    fake = FakeBao({"KEEP": "k"})
    fake.garbage_writes = True
    with pytest.raises(openbao_kv.OpenBaoError):
        openbao_kv.merge(_bao(fake), openbao_kv.CODEX_CREDS, {"A": "1"})


def test_the_token_stays_out_of_the_repr():
    assert BAO_TOKEN not in repr(_bao(FakeBao()))


def test_a_failed_request_raises_with_its_status_and_never_a_value():
    fake = FakeBao({"A": "secret-value"}, fail_writes=True)
    with pytest.raises(openbao_kv.OpenBaoError) as raised:
        openbao_kv.merge(_bao(fake), openbao_kv.CODEX_CREDS, {"B": "other-value"})
    assert raised.value.status == 503
    assert "secret-value" not in str(raised.value) and "other-value" not in str(raised.value)


def test_the_summary_names_keys_and_the_total_only():
    assert openbao_kv.summary(openbao_kv.CODEX_CREDS, ["B", "A"], 5) == (
        "OpenBao secret/agentihooks/codex-creds: wrote A, B (5 keys total)"
    )


def test_credentials_come_from_both_environment_variables_or_not_at_all():
    found = openbao_kv.from_env({"OPENBAO_URL": BAO_URL, "OPENBAO_TOKEN": BAO_TOKEN})
    assert (found.url, found.token) == (BAO_URL, BAO_TOKEN)
    assert openbao_kv.from_env({"OPENBAO_URL": BAO_URL}) is None
    assert openbao_kv.from_env({"OPENBAO_TOKEN": BAO_TOKEN}) is None


def _opener(*, raises=None, body=b"{}"):
    def opener(request, timeout=None):
        if raises:
            raise raises
        return io.BytesIO(body)

    return opener


@pytest.mark.parametrize(
    "opener, message",
    [
        (_opener(raises=http_error(BAO_URL, 400, b'{"errors": ["a", "b"]}')), "OpenBao HTTP 400: a; b"),
        (_opener(raises=http_error(BAO_URL, 503, b"")), "OpenBao HTTP 503: error"),
        (_opener(raises=urllib.error.URLError("connection refused")), "OpenBao request failed: connection refused"),
        (_opener(raises=OSError("network down")), "OpenBao request failed: network down"),
        (_opener(body=b"[1]"), "OpenBao request failed: response is not a JSON object"),
        (_opener(body=b"<html>"), "OpenBao request failed: Expecting value: line 1 column 1 (char 0)"),
    ],
)
def test_every_failure_becomes_one_openbao_error_with_a_plain_message(opener, message):
    with pytest.raises(openbao_kv.OpenBaoError) as raised:
        openbao_kv.read(openbao_kv.OpenBao(BAO_URL, BAO_TOKEN, opener), openbao_kv.CODEX_CREDS)
    assert str(raised.value) == message
    assert raised.value.status == (int(message.split()[2].rstrip(":")) if " HTTP " in message else 0)


def test_an_address_that_is_not_a_url_is_an_openbao_error():
    with pytest.raises(openbao_kv.OpenBaoError) as raised:
        openbao_kv.read(openbao_kv.OpenBao("bao.test", BAO_TOKEN), openbao_kv.CODEX_CREDS)
    assert str(raised.value).startswith("OpenBao request failed: unknown url type")


@pytest.mark.parametrize("url", [f"{BAO_URL}/", "https://bao.testX/"])
def test_only_a_trailing_slash_is_dropped_from_the_address(url):
    fake = FakeBao(url=url)
    assert openbao_kv.merge(openbao_kv.OpenBao(url, BAO_TOKEN, fake), openbao_kv.CODEX_CREDS, {"A": "1"}) == 1


def test_a_secret_without_metadata_reads_as_version_zero():
    body = json.dumps({"data": {"data": {"A": "1"}}}).encode()
    bao = openbao_kv.OpenBao(BAO_URL, BAO_TOKEN, _opener(body=body))
    assert openbao_kv.read(bao, openbao_kv.CODEX_CREDS) == ({"A": "1"}, 0)


def test_a_write_answered_with_an_empty_body_succeeds():
    fake = FakeBao({"KEEP": "k"})
    fake.empty_writes = True
    assert openbao_kv.merge(_bao(fake), openbao_kv.CODEX_CREDS, {"A": "1"}) == 2


def test_a_refused_write_that_is_not_a_conflict_is_not_retried():
    fake = FakeBao({"KEEP": "k"}, fail_writes=True)
    with pytest.raises(openbao_kv.OpenBaoError):
        openbao_kv.merge(_bao(fake), openbao_kv.CODEX_CREDS, {"A": "1"})
    assert len(fake.writes) == 1
