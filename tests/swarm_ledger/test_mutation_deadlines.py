from types import SimpleNamespace

import pytest
from playwright import sync_api

from tests import mutation_trace
from tests.swarm_ledger import test_client_conflict_retry as conflict_retry
from tests.swarm_ledger import test_ledger_authority as authority


@pytest.mark.parametrize("mutant, expected", [("", 5), ("stats", 35), ("scripts.sample.x_value__mutmut_1", 35)])
def test_http_transport_compensates_only_mutation_instrumentation(monkeypatch, mutant, expected):
    monkeypatch.setenv("MUTANT_UNDER_TEST", mutant)
    observed = []

    class Connection:
        def __init__(self, host, port, *, timeout):
            observed.append(timeout)

        def request(self, *args):
            pass

        def getresponse(self):
            return self

        status = 200

        def read(self):
            return b"{}"

        def getheader(self, name):
            return "application/json"

        def close(self):
            pass

    monkeypatch.setattr(authority.http.client, "HTTPConnection", Connection)
    assert authority.send({"port": 1234, "host": "localhost"}, "POST", "/operations", b"{}") == (
        200,
        b"{}",
        "application/json",
    )
    assert observed == [expected]


@pytest.mark.parametrize("mutant, expected", [("", 10), ("stats", 70), ("scripts.sample.x_value__mutmut_1", 70)])
def test_concurrent_add_writers_compensate_only_mutation_instrumentation(monkeypatch, mutant, expected):
    monkeypatch.setenv("MUTANT_UNDER_TEST", mutant)
    assert conflict_retry.Crowded("http://127.0.0.1:1234", {}, None, []).timeout == expected


@pytest.mark.parametrize(
    "mutant, expected",
    [
        ("", 1500),
        ("stats", 1500 * mutation_trace.MUTATION_TRACE_FACTOR),
        ("scripts.sample.x_value__mutmut_1", 1500 * mutation_trace.MUTATION_TRACE_FACTOR),
    ],
)
@pytest.mark.parametrize("owner", [sync_api.Page, sync_api.BrowserContext])
@pytest.mark.parametrize("setter", ["set_default_timeout", "set_default_navigation_timeout"])
def test_browser_deadlines_compensate_only_mutation_instrumentation(monkeypatch, mutant, expected, owner, setter):
    monkeypatch.setenv("MUTANT_UNDER_TEST", mutant)
    observed = []
    tab = SimpleNamespace(_impl_obj=SimpleNamespace(**{setter: lambda timeout: observed.append(timeout)}))
    getattr(owner, setter)(tab, 1500)
    assert observed == [expected]


@pytest.mark.parametrize("owner", [sync_api.Page, sync_api.BrowserContext])
@pytest.mark.parametrize("setter", ["set_default_timeout", "set_default_navigation_timeout"])
def test_tracing_browser_deadlines_again_keeps_one_factor(monkeypatch, owner, setter):
    monkeypatch.setenv("MUTANT_UNDER_TEST", "stats")
    monkeypatch.setattr(owner, setter, getattr(owner, setter))
    mutation_trace.trace_browser_deadlines(sync_api)
    observed = []
    tab = SimpleNamespace(_impl_obj=SimpleNamespace(**{setter: lambda timeout: observed.append(timeout)}))
    getattr(owner, setter)(tab, 1500)
    assert observed == [1500 * mutation_trace.MUTATION_TRACE_FACTOR]
