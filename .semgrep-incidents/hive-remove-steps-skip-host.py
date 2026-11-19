from typing import Any


def step(*args: Any) -> list[str]:
    return []


# ruleid: hive-remove-steps-skip-host
STEPS = (
    ("drain", step),
    ("member", step),
    ("herdr", step),
    ("registry", step),
)

# ruleid: hive-remove-steps-skip-host
STEPS = (
    ("drain", step),
    ("herdr", step),
    ("host", step),
)

# ok: hive-remove-steps-skip-host
STEPS = (
    ("drain", step),
    ("host", step),
    ("member", step),
    ("herdr", step),
    ("registry", step),
)

# ok: hive-remove-steps-skip-host
STEPS = (
    ("drain", step),
    ("member", step),
)
