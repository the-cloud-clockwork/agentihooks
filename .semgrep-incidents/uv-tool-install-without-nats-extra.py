PACKAGE = "agentihooks"


def editable_without_extra(uv: str) -> list[str]:
    # ruleid: uv-tool-install-without-nats-extra
    return [uv, "tool", "install", "--editable", "--force", "."]


def release_without_extra() -> list[str]:
    # ruleid: uv-tool-install-without-nats-extra
    return ["uv", "tool", "install", "--force", PACKAGE]


def release_with_another_extra() -> list[str]:
    # ruleid: uv-tool-install-without-nats-extra
    return ["uv", "tool", "install", "--force", "agentihooks[otel]"]


def editable_with_extra(uv: str) -> list[str]:
    # ok: uv-tool-install-without-nats-extra
    return [uv, "tool", "install", "--editable", "--force", ".[nats]"]


def release_with_extra() -> list[str]:
    # ok: uv-tool-install-without-nats-extra
    return ["uv", "tool", "install", "--force", f"{PACKAGE}[nats]"]


def release_with_all_extras() -> list[str]:
    # ok: uv-tool-install-without-nats-extra
    return ["uv", "tool", "install", "--force", "agentihooks[all]"]


def uninstall(uv: str) -> list[str]:
    # ok: uv-tool-install-without-nats-extra
    return [uv, "tool", "uninstall", PACKAGE]
