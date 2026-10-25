import os

MUTATION_TRACE_FACTOR = 7


def traced(deadline):
    return deadline * (MUTATION_TRACE_FACTOR if os.environ.get("MUTANT_UNDER_TEST") else 1)


def trace_browser_deadlines(api):
    for owner in (api.Page, api.BrowserContext):
        for name in ("set_default_timeout", "set_default_navigation_timeout"):
            setter = getattr(owner, name)
            setter = getattr(setter, "untraced", setter)
            setattr(owner, name, _traced_setter(setter))


def _traced_setter(setter):
    def traced_setter(self, timeout):
        return setter(self, traced(timeout))

    traced_setter.untraced = setter
    return traced_setter
