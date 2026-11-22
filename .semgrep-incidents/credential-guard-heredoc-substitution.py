import re
from typing import NamedTuple


class Verdict(NamedTuple):
    block: str | None = None


ALLOW = Verdict()
HEREDOC_TOKEN = re.compile(r"<<AHHEREDOC(\d+)([^\n]*)")


def runs_input_script(stage: str) -> bool:
    return stage.strip().startswith("bash")


def body_verdict(body: str) -> Verdict:
    return Verdict(block=body) if ".env" in body else ALLOW


def skips_ordinary_bodies(segment: str, bodies: list[str]) -> Verdict:
    # ruleid: credential-guard-heredoc-substitution
    if not any(runs_input_script(stage) for stage in segment.split("|")):
        return ALLOW
    for token in HEREDOC_TOKEN.finditer(segment):
        v = body_verdict(bodies[int(token.group(1))])
        if v.block:
            return v
    return ALLOW


def reaches_ordinary_bodies(segment: str, bodies: list[str]) -> Verdict:
    # ok: credential-guard-heredoc-substitution
    runs_script = any(runs_input_script(stage) for stage in segment.split("|"))
    for token in HEREDOC_TOKEN.finditer(segment):
        body = bodies[int(token.group(1))]
        v = body_verdict(body if runs_script else body.replace("\\$", ""))
        if v.block:
            return v
    return ALLOW


def checks_no_heredoc(stage: str) -> Verdict:
    # ok: credential-guard-heredoc-substitution
    if not any(runs_input_script(part) for part in stage.split("|")):
        return ALLOW
    return body_verdict(stage)
