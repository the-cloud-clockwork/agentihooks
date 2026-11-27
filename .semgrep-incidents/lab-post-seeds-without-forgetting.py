from typing import Any

Moved = list[tuple[str, str, str | None]]


def seeds_only(state: Any, poster: Any, moved: Moved) -> None:
    for check, status, conclusion in moved:
        # ruleid: lab-post-seeds-without-forgetting
        if check in state.runs:
            poster.seed(state.head, check, state.run, state.runs[check], str(state.posted[check][0]), False)
        state.runs[check] = poster.post(state.head, check, status, conclusion, state.output, False)


def seeds_and_names_the_new_check(state: Any, poster: Any, moved: Moved, actions: list[str]) -> None:
    for check, status, conclusion in moved:
        # ruleid: lab-post-seeds-without-forgetting
        if check in state.runs:
            poster.seed(state.head, check, state.run, state.runs[check], str(state.posted[check][0]), False)
        else:
            actions.append(f"first post of {check} on {state.head}")
        state.runs[check] = poster.post(state.head, check, status, conclusion, state.output, False)


def seeds_or_forgets(state: Any, poster: Any, moved: Moved) -> None:
    for check, status, conclusion in moved:
        # ok: lab-post-seeds-without-forgetting
        if check in state.runs:
            poster.seed(state.head, check, state.run, state.runs[check], str(state.posted[check][0]), False)
        else:
            poster.forget(state.head, check, state.run, False)
        state.runs[check] = poster.post(state.head, check, status, conclusion, state.output, False)
