def direct_head(lease: object) -> dict:
    # ruleid: static-context-ignores-graded-commit
    return {"github.sha": lease.head}


def graded_or_head(lease: object) -> dict:
    # ok: static-context-ignores-graded-commit
    return {"github.sha": lease.graded_commit or lease.head}


def shared_from_head(trees: object, lease: object) -> object:
    # ruleid: static-context-ignores-graded-commit
    return trees.shared(lease.head)


def shared_from_reading(trees: object, lease: object) -> object:
    reading = lease.graded_commit or lease.head
    # ok: static-context-ignores-graded-commit
    return trees.shared(reading)


def owned_from_head(trees: object, lease: object) -> object:
    # ruleid: static-context-ignores-graded-commit
    return trees.owned(lease.head, lease.base)


def owned_from_reading(trees: object, lease: object) -> object:
    reading = lease.graded_commit or lease.head
    # ok: static-context-ignores-graded-commit
    return trees.owned(reading, lease.base)
