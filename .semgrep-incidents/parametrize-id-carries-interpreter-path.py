import sys

import pytest


# ruleid: parametrize-id-carries-interpreter-path
@pytest.mark.parametrize("command", [f"cd {sys.prefix} && {sys.prefix}/bin/python -m hooks", "python -m hooks"])
def test_formatted_value(command: str) -> None:
    assert command


# ruleid: parametrize-id-carries-interpreter-path
@pytest.mark.parametrize(
    ("command", "offending"),
    [
        ("python -m hooks --config=file:///etc/x", "/etc/x"),
        (f"{sys.prefix}/$X/etc/shadow -m hooks", f"{sys.prefix}/$X/etc/shadow"),
    ],
)
def test_formatted_value_in_a_case(command: str, offending: str) -> None:
    assert offending in command


# ruleid: parametrize-id-carries-interpreter-path
@pytest.mark.parametrize("interpreter", ["/usr/bin/python3", sys.executable])
def test_bare_interpreter(interpreter: str) -> None:
    assert interpreter


# ruleid: parametrize-id-carries-interpreter-path
@pytest.mark.parametrize(("root", "name"), [(sys.base_prefix, "base"), (f"{sys.exec_prefix}/lib", "exec")])
def test_prefix_in_a_case(root: str, name: str) -> None:
    assert root and name


# ok: parametrize-id-carries-interpreter-path
@pytest.mark.parametrize("command", ["cd {prefix} && {prefix}/bin/python -m hooks", "{prefix}/./bin/python"])
def test_fixed_token(command: str) -> None:
    assert command.replace("{prefix}", sys.prefix)


# ok: parametrize-id-carries-interpreter-path
@pytest.mark.parametrize("command", [f"{sys.prefix}/bin/python"], ids=["interpreter"])
def test_fixed_ids(command: str) -> None:
    assert command


# ok: parametrize-id-carries-interpreter-path
@pytest.mark.parametrize(("argv", "code"), [(["/nonexistent/claude"], 1), ([sys.executable, "-c", ""], 0)])
def test_interpreter_inside_a_list_value(argv: list[str], code: int) -> None:
    assert argv and code >= 0
