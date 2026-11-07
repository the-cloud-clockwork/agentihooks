import hmac
from collections.abc import Callable

from scripts.gates.base import Who
from scripts.swarm_ledger.ledger_authority import agent_token
from scripts.swarm_v2.runtime.commands import Principal, Role

OPERATOR = Principal("operator", Role.OPERATOR)


def authenticator(page_credential: Callable[[str], str | None]) -> Callable[[str, str], Principal | None]:
    def authenticate(slug: str, credential: str) -> Principal | None:
        expected = page_credential(slug)
        if not isinstance(expected, str) or not isinstance(credential, str) or not credential:
            return None
        return OPERATOR if hmac.compare_digest(credential.encode(), expected.encode()) else None

    return authenticate


def credential(slug: str, page_credential: Callable[[str], str | None], who: Who) -> str:
    if who.name or who.swarm:
        return ""
    return page_credential(slug) or ""


def agent_credential(slug: str, page_credential: Callable[[str], str | None], name: str) -> str:
    """The credential a host holding the page credential derives for one named agent, or '' without either."""
    expected = page_credential(slug)
    if not isinstance(expected, str) or not expected or not name:
        return ""
    return agent_token(expected, slug, name)


def agent_authenticator(page_credential: Callable[[str], str | None]) -> Callable[[str, str, str], bool]:
    def bound(slug: str, credential: str, name: str) -> bool:
        expected = agent_credential(slug, page_credential, name)
        if not expected or not isinstance(credential, str) or not credential:
            return False
        return hmac.compare_digest(credential.encode(), expected.encode())

    return bound
