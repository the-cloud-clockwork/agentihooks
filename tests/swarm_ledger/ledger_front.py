"""The public ledger host for tests: reads answered by the ledger server, writes by the ledger API service, as the
public host routes them by method."""

from scripts.swarm_ledger import api_service, ledger_server


class Front(api_service.WriteHandler):
    """One test handler answering GET as the ledger server and POST, PUT and PATCH as the ledger API service."""

    do_GET = ledger_server.Handler.do_GET
