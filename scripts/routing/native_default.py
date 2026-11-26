from collections.abc import Mapping


def prepare(agent: str, environ: Mapping[str, str]) -> dict[str, str]:
    return dict(environ)
