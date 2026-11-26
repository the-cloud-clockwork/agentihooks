def plan_steps(me: str, led: str, name: str, phase: str) -> list[str]:
    return [me, led, name, phase]


def review_steps(me: str, led: str, name: str, phase: str) -> list[str]:
    return [phase, name, led, me]


# ruleid: review-kind-launch-steps
STEPS = {"plan": plan_steps, "code": plan_steps}

# ok: review-kind-launch-steps
STEPS = {"plan": plan_steps, "code": plan_steps, "review": review_steps}
