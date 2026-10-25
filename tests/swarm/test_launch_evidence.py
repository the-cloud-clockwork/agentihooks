import json
import subprocess
from types import SimpleNamespace

import pytest

from scripts.swarm import runtime
from scripts.swarm.tick import SpawnError


@pytest.mark.parametrize("failure", ["timeout", "capacity"])
def test_failed_codex_launch_retains_redacted_evidence_before_termination(tmp_path, monkeypatch, failure):
    report = tmp_path / "validation.json"
    reason = "Selected model is at capacity" if failure == "capacity" else "no canary within 90s"
    source = {"profile": "engineer", "harness": "codex", "state": "pending", "rendered": {"canary": "expected"}}
    report.write_text(json.dumps(source))
    output = f"profile_report={report}\npane_id=pane-test\nprofile_validation=failed\n"
    credential = "AKIA" + "X" * 16
    terminated = []
    monkeypatch.setattr(runtime, "_opened_pane", lambda name, since: "pane-test")

    def run(argv, **kwargs):
        if "terminate-agent" in argv:
            records = list((tmp_path / "sw" / "launch-evidence").glob("*.json"))
            assert len(records) == 1
            text = records[0].read_text()
            assert credential not in text
            retained = json.loads(text)
            assert retained["failure"] == failure
            assert retained["validation"] == source
            assert retained["agent"] == "engineer-test"
            assert retained["task"] == "t1"
            assert retained["harness"] == "codex"
            assert retained["pane_id"] == "pane-test"
            assert "startup failed" in retained["screen"]
            assert "REDACTED" in retained["screen"]
            assert reason in retained["error"]
            report.unlink()
            terminated.append(argv)
            return SimpleNamespace(returncode=0)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 90, output=output.encode(), stderr=reason.encode())
        return SimpleNamespace(returncode=3, stdout=output, stderr=reason)

    def herdr(argv):
        assert argv == ["pane", "read", "pane-test", "--source", "recent", "--lines", "80", "--format", "text"]
        return {"text": f"startup failed: {credential}"}

    launcher = runtime.HerdrRuntime(home=tmp_path, run=run, herdr=herdr)
    config = SimpleNamespace(slug="sw", autonomy="delegate", lanes={}, compact_limit=0)
    with pytest.raises(SpawnError):
        launcher._launch(config, "eng", "t1", "engineer-test", ["init-agent", "--agent", "codex", "--dir", str(tmp_path)])
    assert len(terminated) == 1
    assert list((tmp_path / "sw" / "launch-evidence").glob("*.json"))
