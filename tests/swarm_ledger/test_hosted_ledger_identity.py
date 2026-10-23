"""Guard the HostedLedger class identity across the client-lookup module (#5520).

test_hosted_client_lookup.py rebinds scripts.swarm_v2.hosted_ledger while it
runs (that is its purpose). When its module-scoped fixture reloaded the module
on teardown, every later test on the same worker saw a different HostedLedger
class object, and the deployed-controller test's isinstance(...) against the
class captured at import time failed. The teardown now restores the original
namespace; this guard asserts the class identity is intact once that module
has run on this worker.
"""

import pytest

from scripts.swarm_v2 import hosted_ledger

ORIGINAL_HOSTED_LEDGER = hosted_ledger.HostedLedger

pytestmark = pytest.mark.xdist_group("hosted_client_lookup")


def test_hosted_ledger_class_identity_survives_client_lookup_module():
    assert hosted_ledger.HostedLedger is ORIGINAL_HOSTED_LEDGER
