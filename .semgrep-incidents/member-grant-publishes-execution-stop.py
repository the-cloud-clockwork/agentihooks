MEMBER_DENY = ("swarm.*.execution.stop",)
MEMBER_PUBLISH = ("swarm.*.execution.>",)


def user_claims() -> dict[str, object]:
    return {"type": "user"}


def member_claims(user: str, swarms: tuple[str, ...] | None = None) -> dict[str, object]:
    if swarms is None:
        return {
            **user_claims(),
            # ruleid: member-grant-publishes-execution-stop
            "pub": {"allow": [*MEMBER_PUBLISH]},
            "sub": {"allow": [f"_INBOX.{user}.>"]},
        }
    publish = [f"swarm.{swarm}.>" for swarm in swarms]
    return {
        # ruleid: member-grant-publishes-execution-stop
        "pub": {"allow": publish, "deny": ["dead.>"]},
        "sub": {"allow": [f"_INBOX.{user}.>"]},
    }


def scoped_member_claims(user: str, swarms: tuple[str, ...]) -> dict[str, object]:
    publish = [f"swarm.{swarm}.>" for swarm in swarms]
    return {
        "pub": {"allow": publish, "deny": list(MEMBER_DENY)} if publish else {"deny": [">"]},
        "sub": {"allow": [f"_INBOX.{user}.>"]},
    }


def member_claims_denied(user: str) -> dict[str, object]:
    return {
        # ok: member-grant-publishes-execution-stop
        "pub": {"allow": [*MEMBER_PUBLISH], "deny": list(MEMBER_DENY)},
        "sub": {"allow": [f"_INBOX.{user}.>"]},
    }
