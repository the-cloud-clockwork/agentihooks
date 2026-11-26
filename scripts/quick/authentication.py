from collections.abc import Mapping

from scripts.quick.guard import QuickError


def _named(reference: str, harness: str, environ: Mapping[str, str]) -> str:
    if harness == "claude":
        from scripts.claude_quota_balancer import RoutingError, credential_for_slug, discover_credentials

        try:
            return credential_for_slug(discover_credentials(environ), reference.removeprefix("AH_CC_TOKEN_")).account
        except RoutingError as error:
            raise QuickError("named Claude authentication reference is unavailable") from error
    from scripts.codex_router import token_accounts

    for account in token_accounts(environ):
        if reference in (account.name, account.env_name):
            return account.name
    raise QuickError("named Codex authentication reference is unavailable")


def selections(value: str, harnesses: list[str], environ: Mapping[str, str]) -> list[tuple[str, str]]:
    from scripts import operator_env

    values = value.split(",")
    if len(values) == 1:
        values *= len(harnesses)
    if len(values) != len(harnesses):
        raise QuickError(f"authentication needs one choice or {len(harnesses)} engineer choices")
    active = dict(environ)
    operator_env.fill(active)
    result: list[tuple[str, str]] = []
    for choice, harness in zip(values, harnesses, strict=True):
        if choice in ("routed", "default"):
            result.append((choice, ""))
        elif choice.startswith("named:") and choice.removeprefix("named:"):
            result.append(("named", _named(choice.removeprefix("named:"), harness, active)))
        else:
            raise QuickError("authentication must be routed, default or named:reference")
    return result
