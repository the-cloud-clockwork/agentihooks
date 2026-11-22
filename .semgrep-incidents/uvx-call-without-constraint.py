AUDIT = "pip-audit==2.10.1"
LOCK = ".github/ci-tools/audit.lock"


def audit_with_one_exclusion(requirements: str) -> list[str]:
    # ruleid: uvx-call-without-constraint
    return ["uvx", "--with", "platformdirs!=4.14.8", AUDIT, "-r", requirements, "--no-deps", "--format", "json"]


def tool_with_no_options() -> tuple[str, ...]:
    # ruleid: uvx-call-without-constraint
    return ("uvx", "ruff==0.15.12", "check", ".")


def audit_under_its_lock(requirements: str) -> list[str]:
    # ok: uvx-call-without-constraint
    return ["uvx", "--constraints", LOCK, AUDIT, "-r", requirements, "--no-deps", "--format", "json"]


def tool_under_the_short_flag() -> tuple[str, ...]:
    # ok: uvx-call-without-constraint
    return ("uvx", "-c", ".github/ci-tools/lint-tools.lock", "ruff==0.15.12", "check", ".")


def audit_under_the_joined_flag() -> list[str]:
    # ok: uvx-call-without-constraint
    return ["uvx", "--constraints=.github/ci-tools/audit.lock", AUDIT]


def audit_under_the_singular_alias() -> list[str]:
    # ok: uvx-call-without-constraint
    return ["uvx", "--constraint", LOCK, AUDIT]
