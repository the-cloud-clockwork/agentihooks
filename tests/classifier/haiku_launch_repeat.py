import pytest

from tests.classifier import test_haiku_launch as launch

earlier_agent = launch.earlier_agent


@launch.needs_proc
@pytest.mark.xdist_group("haiku-launch-repeat")
@pytest.mark.parametrize("attempt", range(50))
@pytest.mark.parametrize("delayed", [False, True])
def test_repeated_first_attempt(monkeypatch, tmp_path, earlier_agent, attempt, delayed):
    launch.test_shell_startup_agent_is_stopped_before_the_cli(monkeypatch, tmp_path, earlier_agent, delayed)
