import json
import os
import urllib.error
import urllib.request

from scripts.gates import Who
from scripts.swarm.store import AgentRecord, RedisStore, SwarmError
from scripts.swarm_v2 import operator_auth


def decide(store: RedisStore, slug: str, agent: AgentRecord, choice: str, who: Who) -> dict:
    from scripts.swarm_ledger import ledger
    from scripts.swarm_v2.control_service import page_credential

    address = store.config(slug).api_url
    if not address:
        raise SwarmError("the Anton controller API address is unavailable")
    if who.pinned:
        token = os.environ.get("AGENTIHOOKS_LEDGER_AGENT_TOKEN") or ledger.launch_token(slug, who.name)
    else:
        token = operator_auth.credential(slug, page_credential, who)
    if not token:
        raise SwarmError("the restore decision needs a master or operator credential")
    request = urllib.request.Request(
        f"{address.rstrip('/')}/v1/executions/{agent.execution_id}/restore-decision",
        json.dumps({"agent": who.name, "choice": choice}).encode(),
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = json.loads(error.read())
        raise SwarmError(f"the Anton restore decision was refused: {detail['message']}") from None
    except OSError as error:
        raise SwarmError(f"the Anton controller could not be reached: {type(error).__name__}") from None
