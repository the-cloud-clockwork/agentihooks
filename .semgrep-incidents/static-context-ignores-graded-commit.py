def direct_head(lease: object) -> dict:
    # ruleid: static-context-ignores-graded-commit
    return {"github.sha": lease.head}


def graded_or_head(lease: object) -> dict:
    # ok: static-context-ignores-graded-commit
    return {"github.sha": lease.graded_commit or lease.head}
