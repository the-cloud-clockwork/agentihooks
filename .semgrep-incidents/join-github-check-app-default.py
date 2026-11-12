import os


def endpoint() -> str:
    # ruleid: join-github-check-app-default
    return "installation/repositories" if os.environ.get("HIVE_GITHUB_AUTH_MODE", "app") == "app" else "user"


def member_endpoint() -> str:
    default = "app" if os.environ.get("AGENTIHOOKS_HIVE_ID") else "user"
    # ok: join-github-check-app-default
    return "installation/repositories" if os.environ.get("HIVE_GITHUB_AUTH_MODE", default) == "app" else "user"


def token_mode() -> str:
    # ok: join-github-check-app-default
    return os.environ.get("HIVE_GITHUB_AUTH_MODE", "app")
