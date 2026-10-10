from dataclasses import dataclass

MODE = "AGENTIHOOKS_ACCOUNT_RECONCILE"


@dataclass(frozen=True)
class Finding:
    kind: str
    action: str
    account: str = ""
    holder: str = ""
    execution_id: str = ""
    generation: int = 0
    evidence: str = ""


class AccountReconciler:
    def __init__(self, store, slug, clock=None, environ=None) -> None:
        self.store, self.slug = store, slug
