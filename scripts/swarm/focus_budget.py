from scripts.swarm import freeze, hierarchy


def ranks(doc: dict) -> dict[str, int]:
    targets = [row["target"] for row in doc.get("freezes", []) if row["verb"] == "focus"]
    graph = hierarchy.project(doc)[0]
    return {
        task["id"]: next(
            (
                index
                for index, target in enumerate(targets)
                if freeze.covers(target, task, freeze.ancestry(task, graph))
            ),
            len(targets),
        )
        for task in doc["tasks"]
    }


def select(doc: dict, ready: list, lane: str, capacity: int, weight: int) -> list:
    order = ranks(doc)
    unfocused = sum(row["verb"] == "focus" for row in doc.get("freezes", []))

    def focused(task):
        return order[task["id"]] < unfocused

    holding = sum(
        task.get("lane") == lane and task.get("state") in ("claimed", "pr") and focused(task) for task in doc["tasks"]
    )
    slots = max(0, capacity * weight // 100 - holding)
    hot = sorted((task for task in ready if focused(task)), key=lambda task: order[task["id"]])
    return hot[:slots] + [task for task in ready if not focused(task)]
