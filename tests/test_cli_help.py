import pytest

from scripts import install


def run_cli(monkeypatch, capsys, *args):
    monkeypatch.setattr("sys.argv", ["agentihooks", *args])
    with pytest.raises(SystemExit) as exc:
        install.main()
    output = capsys.readouterr()
    return exc.value.code, output.out, output.err


def test_help_lists_commands_without_brace_list(monkeypatch, capsys):
    code, out, err = run_cli(monkeypatch, capsys, "-h")
    assert code == 0
    assert err == ""
    assert out.splitlines()[0].startswith("usage: agentihooks ")
    assert "COMMAND ..." in out
    assert "{version,update" not in out
    assert "commands:\n" in out
    assert "    version" in out
    assert "Print version" in out
    assert "    init" in out
    assert "Initialize agentihooks" in out


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("zzzzz",), "unknown command zzzzz"),
        (("versoin",), "unknown command versoin; did you mean version?"),
        (("--zzzzz",), "unknown option --zzzzz"),
        (("--zzzzz", "value"), "unknown option --zzzzz"),
        (("bundle", "--zzzzz"), "unknown option --zzzzz"),
        (("init", "--zzzzz"), "unknown option --zzzzz"),
        (("bundle", "zzzzz"), "unknown command zzzzz"),
        (("bundle", "lnik"), "unknown command lnik; did you mean link?"),
    ],
)
def test_parser_errors_are_one_line(monkeypatch, capsys, args, message):
    code, out, err = run_cli(monkeypatch, capsys, *args)
    assert code == 2
    assert out == ""
    assert err == f"{message}, run agentihooks -h and try again\n"


@pytest.mark.parametrize("command", ["claude", "codex"])
def test_routed_agent_flags_are_passed_through(monkeypatch, command):
    from scripts import codex_router

    received = []
    monkeypatch.setattr("sys.argv", ["agentihooks", command, "--zzzzz", "value"])
    monkeypatch.setattr(install, "cmd_claude", received.append)
    monkeypatch.setattr(install, "_load_claude_runtime_env", lambda: None)
    monkeypatch.setattr(codex_router, "main", lambda args: received.append(args) or 0)
    if command == "codex":
        with pytest.raises(SystemExit) as exc:
            install.main()
        assert exc.value.code == 0
    else:
        install.main()
    assert received == [["--zzzzz", "value"]]


def test_shared_parser_preserves_arguments_and_reports_missing_values(capsys):
    from scripts.cli_parser import ArgumentParser

    parser = ArgumentParser(prog="agentihooks sample")
    parser.add_argument("--mode", choices=["one", "two"])
    parser.add_argument("name")
    assert vars(parser.parse_args(["--mode", "one", "value"])) == {"mode": "one", "name": "value"}
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--mode"])
    assert exc.value.code == 2
    assert capsys.readouterr().err == "argument --mode: expected one argument, run agentihooks -h and try again\n"


@pytest.mark.parametrize("result", [(None, "--zzzzz", None), [(None, "--zzzzz", None, None)]])
def test_unknown_options_support_both_argparse_return_shapes(monkeypatch, capsys, result):
    import argparse

    from scripts.cli_parser import ArgumentParser

    monkeypatch.setattr(argparse.ArgumentParser, "_parse_optional", lambda self, token: result)
    with pytest.raises(SystemExit) as exc:
        ArgumentParser()._parse_optional("--zzzzz")
    assert exc.value.code == 2
    assert capsys.readouterr().err == "unknown option --zzzzz, run agentihooks -h and try again\n"


def test_unknown_option_suggests_nearby_flag(monkeypatch, capsys):
    code, out, err = run_cli(monkeypatch, capsys, "--versoin")
    assert code == 2
    assert out == ""
    assert err == "unknown option --versoin; did you mean --version?, run agentihooks -h and try again\n"


def test_nested_parser_keeps_valid_flags_abbreviations_and_negative_values(capsys):
    from scripts.cli_parser import ArgumentParser

    parser = ArgumentParser(prog="agentihooks")
    sub = parser.add_subparsers(dest="command")
    leaf = sub.add_parser("sample", help="Sample command")
    leaf.add_argument("--number", type=int)
    assert vars(parser.parse_args(["sample", "--num=-3"])) == {"command": "sample", "number": -3}
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["sample", "--zzzzz=value"])
    assert exc.value.code == 2
    assert capsys.readouterr().err == "unknown option --zzzzz, run agentihooks -h and try again\n"


def test_plain_parser_errors_have_a_single_retry_hint(monkeypatch, capsys):
    code, out, err = run_cli(monkeypatch, capsys, "version", "extra")
    assert code == 2
    assert out == ""
    assert err == "unknown argument extra, run agentihooks -h and try again\n"


def test_unknown_flag_when_abbreviations_are_disabled(capsys):
    from scripts.cli_parser import ArgumentParser

    parser = ArgumentParser(prog="agentihooks", allow_abbrev=False)
    parser.add_argument("--number")
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--num"])
    assert exc.value.code == 2
    assert capsys.readouterr().err == (
        "unknown option --num; did you mean --number?, run agentihooks -h and try again\n"
    )
