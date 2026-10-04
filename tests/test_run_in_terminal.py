import subprocess

import pytest

from scripts import herdr_host, run_in_terminal


@pytest.fixture
def herdr(monkeypatch):
    calls = []
    monkeypatch.setattr(herdr_host, "binary", lambda: "/bin/herdr")
    monkeypatch.setattr(herdr_host, "ensure_server", lambda environ: False)
    monkeypatch.setattr(
        herdr_host,
        "open_pane",
        lambda directory, title, env, placement, workspace, environ: (
            calls.append(("open", directory, title, placement, workspace))
            or herdr_host.Placement("w1", "w1:t2", "w1:p3")
        ),
    )
    monkeypatch.setattr(herdr_host, "_cli", lambda args, environ: calls.append(tuple(args)) or {})
    monkeypatch.setattr(run_in_terminal.subprocess, "run", lambda *a, **k: pytest.fail("native terminal opened"))
    return calls


def test_a_command_runs_in_a_new_herdr_tab(herdr, tmp_path, capsys):
    rc = run_in_terminal.main(
        ["--dir", str(tmp_path), "--title", "tests", "--", "npm", "test"], {"HOME": str(tmp_path)}
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert herdr[0] == ("open", tmp_path, "tests", "tab", "")
    assert ("pane", "run", "w1:p3", "npm test") in herdr
    assert "host=herdr" in out and "pane_id=w1:p3" in out


def test_without_a_command_the_tab_is_an_interactive_shell(herdr, tmp_path):
    assert run_in_terminal.main(["--dir", str(tmp_path)], {"HOME": str(tmp_path)}) == 0
    assert herdr[0][2] == tmp_path.name
    assert not any(call[:2] == ("pane", "run") for call in herdr[1:])


def test_the_native_host_runs_the_terminal_script(monkeypatch, tmp_path, capsys):
    ran = []
    monkeypatch.setattr(herdr_host, "binary", lambda: None)
    monkeypatch.setattr(
        run_in_terminal.subprocess, "run", lambda argv, **k: ran.append(argv) or subprocess.CompletedProcess(argv, 0)
    )
    rc = run_in_terminal.main(["--dir", str(tmp_path), "--", "make"], {"HOME": str(tmp_path)})
    assert rc == 0
    assert ran[0][0].endswith("run-in-terminal/scripts/02_run_terminal.sh")
    assert ran[0][1:] == [str(tmp_path), tmp_path.name, "make"]
    assert "host=native" in capsys.readouterr().out


def test_an_automatic_herdr_failure_falls_back_to_the_native_terminal(monkeypatch, tmp_path, capsys):
    ran = []
    monkeypatch.setattr(herdr_host, "binary", lambda: "/bin/herdr")

    def down(environ):
        raise herdr_host.HerdrError("server gone")

    monkeypatch.setattr(herdr_host, "ensure_server", down)
    monkeypatch.setattr(
        run_in_terminal.subprocess, "run", lambda argv, **k: ran.append(argv) or subprocess.CompletedProcess(argv, 0)
    )
    assert run_in_terminal.main(["--dir", str(tmp_path)], {"HOME": str(tmp_path)}) == 0
    out = capsys.readouterr().out
    assert len(ran) == 1 and "herdr_error=server gone" in out and "host=native" in out
