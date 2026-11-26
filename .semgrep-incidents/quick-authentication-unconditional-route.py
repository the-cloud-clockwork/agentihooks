def argv(swarm, seat, prompt_file):
    # ruleid: quick-authentication-unconditional-route
    return ["--agent", seat.harness, "--routed-only"]


def argv(swarm, seat, prompt_file):
    policy = ["--routed-only"] if seat.auth == "routed" else ["--auth", seat.auth]
    # ok: quick-authentication-unconditional-route
    return ["--agent", seat.harness, *policy]
