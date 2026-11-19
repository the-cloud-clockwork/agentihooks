"""Swarm create runs the store's refusals before it marks the ledger as a swarm ledger."""

import pytest

from scripts.swarm import cli
from tests.swarm.test_cli import env as env


def test_a_create_the_store_refuses_leaves_the_ledger_unmarked(env: tuple, capsys: pytest.CaptureFixture) -> None:
    _, ledger, _ = env
    assert cli.main(["sw", "create", "--repo", "/repo"]) == 0
    ledger.swarm_sized.clear()
    capsys.readouterr()
    assert cli.main(["sw", "create", "--repo", "/repo"]) == 1
    assert capsys.readouterr().err == "swarm: swarm sw already exists\n"
    assert ledger.swarm_sized == []


def test_create_refusal_names_what_create_would_refuse_before_a_ledger_is_marked(env: tuple) -> None:
    swarms, ledger, _ = env
    assert cli.create_refusal(swarms, ["sw", "create", "--repo", "/repo"]) == ""
    assert cli.create_refusal(swarms, ["2026-q4", "create", "--repo", "/repo"]) == (
        "a swarm id is lowercase letters, digits and dashes, starting with a letter, at most 48 long"
    )
    assert ledger.swarm_sized == [] and swarms.slugs() == []
    assert cli.main(["sw", "create", "--repo", "/repo"]) == 0
    assert cli.create_refusal(swarms, ["sw", "create", "--repo", "/repo"]) == "swarm sw already exists"
