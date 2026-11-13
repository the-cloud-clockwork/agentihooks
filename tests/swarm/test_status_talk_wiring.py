from typing import Any

import pytest

from scripts.swarm import cli
from scripts.swarm.health import corpus
from tests.swarm.test_cli import env as env
from tests.swarm.test_cli import run


@pytest.mark.feature("swarm-health-corpus")
def test_status_hands_the_detectors_talk_counts_as_the_corpus_replays_them(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[Any, ...]] = []
    monkeypatch.setattr(cli.health, "findings", lambda *args: seen.append(args) or [])
    run("sw", "create", "--repo", "/repo")
    run("sw", "status")
    assert [isinstance(args[6], dict) for args in seen] == [True]


@pytest.mark.feature("swarm-health-corpus")
def test_the_replay_reads_the_repo_corpus_when_no_directory_is_given() -> None:
    assert corpus.main([]) == 0
