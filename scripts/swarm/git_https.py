"""Retry one swarm git call over https with the gh credential helper when GitHub refuses ssh auth."""

import os
import subprocess
from collections.abc import Callable, Mapping

REFUSED = "Permission denied (publickey"
SETTINGS = (
    ("url.https://github.com/.insteadOf", "git@github.com:"),
    ("url.https://github.com/.insteadOf", "ssh://git@github.com/"),
    ("url.https://github.com/.insteadOf", "git+ssh://git@github.com/"),
    ("credential.https://github.com.helper", ""),
    ("credential.https://github.com.helper", "!gh auth git-credential"),
)

Runner = Callable[..., subprocess.CompletedProcess[str]]


def environment(base: Mapping[str, str]) -> dict[str, str]:
    """The base environment with the https settings appended after the GIT_CONFIG entries it already holds."""
    env = dict(base)
    held = int(env.get("GIT_CONFIG_COUNT") or "0")
    for index, (key, value) in enumerate(SETTINGS, start=held):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    env["GIT_CONFIG_COUNT"] = str(held + len(SETTINGS))
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def run(
    argv: list[str],
    runner: Runner | None = None,
    *,
    cwd: str | os.PathLike[str] | None = None,
    timeout: float | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run argv; a call refused over ssh publickey runs once more over https for that call only."""
    call = runner or subprocess.run
    options = {key: value for key, value in (("cwd", cwd), ("timeout", timeout)) if value is not None}
    first = options if env is None else {**options, "env": env}
    done = call(argv, capture_output=True, text=True, **first)
    if done.returncode == 0 or REFUSED not in done.stderr:
        return done
    return call(argv, capture_output=True, text=True, env=environment(os.environ if env is None else env), **options)
