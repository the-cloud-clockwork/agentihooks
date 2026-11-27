# ruleid: delivery-injector-bound-at-import
from hooks.common import inject_context


def retained_at_import() -> None:
    inject_context("delivery requirements", kind="enforcement")


def resolved_when_called() -> None:
    # ok: delivery-injector-bound-at-import
    from hooks.common import inject_context

    inject_context("delivery requirements", kind="enforcement")
