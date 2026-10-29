import pytest

from tests.workflow_commands import carrying_commands, refuse_commands

pytestmark = pytest.mark.unit

COMMANDS = [
    "error",
    "warning",
    "notice",
    "debug",
    "group",
    "endgroup",
    "add-mask",
    "stop-commands",
    "echo",
    "set-output",
    "save-state",
    "add-path",
    "set-env",
    "add-matcher",
    "remove-matcher",
]


def test_a_node_id_carrying_the_error_command_fails_collection():
    nodeid = 'tests/test_x.py::test_y[echo "::error::boom"]'
    with pytest.raises(pytest.UsageError, match="1 test node ids carry a GitHub workflow command") as raised:
        refuse_commands(["tests/test_x.py::test_plain", nodeid])
    assert nodeid in str(raised.value)


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("ending", [" file=a.py", "::"], ids=["with_parameters", "bare"])
def test_every_command_form_is_found(command, ending):
    assert carrying_commands([f"tests/test_x.py::test_y[::{command}{ending}]"]) != []


def test_ordinary_node_ids_pass():
    nodeids = [
        "tests/test_x.py::test_error_is_reported",
        "tests/test_x.py::TestGroup::test_warning",
        "tests/test_x.py::errors::test_y",
        "tests/test_x.py::test_y[error]",
    ]
    assert carrying_commands(nodeids) == []
    refuse_commands(nodeids)
