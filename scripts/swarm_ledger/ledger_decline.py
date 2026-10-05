"""agentihooks ledger decline: the operator declined a swarm or small ledger for this session.

The ledger decision hook reads this call from the session's tool events and stays silent for the session.
"""

import json


def main():
    print(json.dumps({"declined": True}))
    return 0
