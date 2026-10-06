from scripts.ci_mutation.__main__ import main


def test_cli_uses_requested_revisions_budget_output_and_exit_code(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.delenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", raising=False)

    def discover(root, base, head):
        calls.append((root, base, head))
        return {"hooks/sample.py": {2}}

    def gate(root, changes, output, budget):
        calls.append((root, changes, output, budget))
        return {"failed": True}

    (tmp_path / "hooks").mkdir()
    (tmp_path / "scripts").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("scripts.ci_mutation.__main__.discover_changes", discover)
    monkeypatch.setattr("scripts.ci_mutation.__main__.run_gate", gate)
    monkeypatch.setattr(
        "sys.argv", ["gate", "--base", "base", "--head", "head", "--budget", "13", "--output", "evidence"]
    )
    assert main() == 1
    assert calls == [(tmp_path, "base", "head"), (tmp_path, {"hooks/sample.py": {2}}, tmp_path / "evidence", 13)]
    assert "Changed Python files: 1" in capsys.readouterr().out
    assert __import__("os").environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"


def test_cli_defaults_and_success(tmp_path, monkeypatch):
    (tmp_path / "hooks").mkdir()
    (tmp_path / "scripts").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["gate"])

    def discover(root, base, head):
        assert (root, base, head) == (tmp_path, "origin/dev", "HEAD")
        return {}

    def gate(root, changes, output, budget):
        assert (root, changes, output, budget) == (tmp_path, {}, tmp_path / ".mutation-gate", 1080)
        return {"failed": False}

    monkeypatch.setattr("scripts.ci_mutation.__main__.discover_changes", discover)
    monkeypatch.setattr("scripts.ci_mutation.__main__.run_gate", gate)
    assert main() == 0
