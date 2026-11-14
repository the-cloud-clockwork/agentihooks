def _slot_room(task: dict[str, str], harness: str, account: str) -> bool:
    return bool(task and harness and account)


class Unchecked:
    def spawn(self, saved: dict[str, str], task: dict[str, str], agent: str) -> list[str]:
        # ruleid: hive-saved-account-route-unchecked
        return ["--route", saved["account"]] if saved.get("account") else []


class Checked:
    def spawn(self, saved: dict[str, str], task: dict[str, str], agent: str) -> list[str]:
        # ok: hive-saved-account-route-unchecked
        return ["--route", saved["account"]] if _slot_room(task, agent, saved.get("account", "")) else []


def launch(saved: dict[str, str]) -> list[str]:
    # ok: hive-saved-account-route-unchecked
    return ["--route", saved["account"]] if saved.get("account") else []
