import pytest

from scripts.ci_lab.remote import checkout

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("lab", "origin", "served"),
    [
        (
            "http://lab.example/other-owner/agentihooks.git",
            "git@github.com:the-cloud-clockwork/agentihooks.git",
            False,
        ),
        (
            "http://lab.example/the-cloud-clockwork/agentihooks.git",
            "git@github.com:the-cloud-clockwork/agentihooks.git",
            True,
        ),
        (
            "git@lab.example:other-owner/agentihooks.git",
            "https://github.com/the-cloud-clockwork/agentihooks.git",
            False,
        ),
        (
            "git@lab.example:the-cloud-clockwork/agentihooks.git",
            "https://github.com/the-cloud-clockwork/agentihooks.git",
            True,
        ),
        (
            "ssh://git@lab.example/other-owner/agentihooks.git",
            "ssh://git@github.com/the-cloud-clockwork/agentihooks.git",
            False,
        ),
        (
            "ssh://git@lab.example/The-Cloud-Clockwork/AgentiHooks.git/\n",
            "https://github.com/the-cloud-clockwork/agentihooks.git",
            True,
        ),
        (
            "http://lab.example/the-cloud-clockwork/agentihooks.git",
            "http://github.com/agentihooks.git",
            False,
        ),
        (
            "http://lab.example/agentihooks.git",
            "git@github.com:the-cloud-clockwork/agentihooks.git",
            True,
        ),
        (
            "/srv/git/agentihooks.git",
            "git@github.com:the-cloud-clockwork/agentihooks.git",
            True,
        ),
        (
            "file:///srv/git/agentihooks.git",
            "git@github.com:the-cloud-clockwork/agentihooks.git",
            True,
        ),
        (
            "http://lab.example/the-cloud-clockwork/other.git",
            "git@github.com:the-cloud-clockwork/agentihooks.git",
            False,
        ),
        ("", "", False),
    ],
)
@pytest.mark.feature("lab-remote-repository-owner")
def test_lab_remote_matches_repository_owner(lab: str, origin: str, served: bool) -> None:
    assert checkout.serves(lab, origin) is served
