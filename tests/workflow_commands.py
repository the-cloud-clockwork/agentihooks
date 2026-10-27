"""Refuse test node ids that carry a GitHub workflow command, since the runner acts on one printed in the job log."""

import re

import pytest

COMMAND = re.compile(
    r"::(?:error|warning|notice|debug|group|endgroup|add-mask|stop-commands|echo|set-output|save-state|add-path"
    r"|set-env|add-matcher|remove-matcher)(?=[\s:,])"
)


def carrying_commands(nodeids: list[str]) -> list[str]:
    return [nodeid for nodeid in nodeids if COMMAND.search(nodeid)]


def refuse_commands(nodeids: list[str]) -> None:
    if found := carrying_commands(nodeids):
        listed = "\n".join(found)
        raise pytest.UsageError(
            f"{len(found)} test node ids carry a GitHub workflow command; give them plain parametrize ids:\n{listed}"
        )
