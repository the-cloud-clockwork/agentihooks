import pytest

from scripts import openbao_kv
from tests.codex_fakes import BAO_TOKEN, BAO_URL, FakeBao


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
