import pytest

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
