import pytest

from scripts.swarm import session_model
from scripts.swarm.store import AgentRecord


def record(model, effort, source):
    return AgentRecord(
        "engineer@a1b2c3-0001",
        "eng",
        "t1",
        started_at=1_000,
        model=model,
        effort=effort,
        model_source=source,
        model_confidence=0.9,
    )


@pytest.mark.parametrize(
    "recorded, reported, expected",
    [
        (
            ("opus", "high", "lane-default"),
            ("claude-sonnet-5-5", "low", 2_000),
            ("claude-sonnet-5-5", "low", "session", None),
        ),
        (("opus", "high", "luna"), ("claude-opus-5-5", "high", 2_000), ("opus", "high", "luna", 0.9)),
        (("opus", "high", "luna"), ("claude-opus-5-5[1m]", "high", 2_000), ("opus", "high", "luna", 0.9)),
        (("haiku", "low", "luna"), ("claude-haiku-4-5-20251001", "low", 2_000), ("haiku", "low", "luna", 0.9)),
        (("opus", "high", "luna"), ("claude-opus-5-5", "max", 2_000), ("claude-opus-5-5", "max", "session", None)),
        (
            ("claude-opus-5-5", "max", "session"),
            ("claude-opus-5-5", "max", 2_000),
            ("claude-opus-5-5", "max", "session", 0.9),
        ),
        (
            ("claude-opus-5-5", "high", "session"),
            ("claude-opus-4-6", "high", 2_000),
            ("claude-opus-4-6", "high", "session", None),
        ),
        (
            ("gpt-6.1-sol", "high", "lane-default"),
            ("gpt-6.1-sol", "", 2_000),
            ("gpt-6.1-sol", "high", "lane-default", 0.9),
        ),
        (
            ("gpt-6.1-sol", "high", "lane-default"),
            ("gpt-6.1-luna", "", 2_000),
            ("gpt-6.1-luna", "high", "session", None),
        ),
        (("sonnet", "low", "luna"), ("claude-opus-5-5", "high", 999), ("sonnet", "low", "luna", 0.9)),
        (("sonnet", "low", "luna"), ("claude-opus-5-5", "high", 1_000), ("claude-opus-5-5", "high", "session", None)),
        (("", "", ""), ("claude-opus-5-5", "high", 2_000), ("claude-opus-5-5", "high", "session", None)),
    ],
)
def test_a_report_replaces_the_recorded_model_only_when_the_session_runs_another(recorded, reported, expected):
    model, effort, at = reported
    agent = session_model.apply(record(*recorded), {"model": model, "effort": effort, "at": at})
    assert (agent.model, agent.effort, agent.model_source, agent.model_confidence) == expected


def test_an_agent_without_a_report_keeps_its_record():
    agent = record("opus", "high", "luna")
    assert session_model.apply(agent, None) == agent
