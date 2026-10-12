import json
from contextlib import contextmanager
from dataclasses import replace

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]


@pytest.mark.parametrize("capture_at,status", [(39, "complete"), (40, "incomplete")])
def test_production_remote_capture_completes_within_its_pod_grace(tmp_path, monkeypatch, capture_at, status):
    from scripts.swarm_v2 import deployed, supervision_protocol, supervision_runtime
    from scripts.swarm_v2.auth_context import Registration
    from scripts.swarm_v2.broadcast_bridge import grant_path
    from scripts.swarm_v2.checkpoints import capture, runtime
    from scripts.swarm_v2.supervision import Launch
    from tests.test_swarm_v2_checkpoints_capture import portable
    from tests.test_swarm_v2_checkpoints_runtime import artifact_endpoint
    from tests.test_swarm_v2_deployed_runtime import SLUG, Pods, _store, _workers

    attempt, _, environ, newest = portable.__wrapped__(tmp_path, monkeypatch)
    from pathlib import Path

    old_home = Path(environ["CLAUDE_CONFIG_DIR"])
    home = attempt / "homes" / "claude"
    home.parent.mkdir(parents=True)
    newest_relative = newest.relative_to(old_home)
    old_home.rename(home)
    environ["CLAUDE_CONFIG_DIR"] = str(home)
    newest = home / newest_relative
    original = json.loads((attempt / "launch.json").read_text())
    registration = replace(
        Registration(**original["registration"]), execution_id="exe-" + "1" * 32, grant_id="lgr-" + "2" * 32
    )
    execution = json.loads((attempt / "execution.json").read_text())
    execution["attempt"] = registration.execution_id
    (attempt / "execution.json").write_text(json.dumps(execution))
    pods = Pods("swarm-pod-proof")
    monkeypatch.setattr(deployed, "pod_api", lambda *_: pods)
    settings = _workers(tmp_path, AGENTIHOOKS_CHECKPOINT_BUCKET="fixture-checkpoints")
    workers = deployed.Workers.from_environ(settings, SLUG)
    supervision = workers.grants(settings, SLUG, lambda _: registration, _store()).supervision
    assert supervision.budgets.checkpoint == 40
    assert supervision.budgets.quiesce + supervision.budgets.kill * 2 + supervision.budgets.checkpoint < 60

    with contextmanager(artifact_endpoint.__wrapped__)(tmp_path) as (endpoint, _, _, authority):
        spec = json.loads(supervision.record(registration, "engineer"))
        spec.update(control_url=endpoint["control_url"], image_digest=original["image_digest"])
        authority.authorize.return_value = registration
        authority.current.return_value.execution_id = registration.execution_id
        grant_path(attempt).parent.mkdir(exist_ok=True)
        grant_path(attempt).write_text("grant")
        (attempt / "registration.json").write_text(json.dumps(spec["authority"]))
        (attempt / "launch.json").write_text(json.dumps(spec))
        environ.pop(runtime.ROOT)
        environ[runtime.REMOTE] = "true"
        owner = supervision_runtime.Supervisor(Launch.load(attempt, attempt / "launch.json"))
        owner.environment.update(environ)
        owner.root.mkdir(parents=True)
        monkeypatch.setattr(owner, "quiesce", lambda: True)
        monkeypatch.setattr(supervision_runtime.trees, "cleanup", lambda *_: None)
        elapsed = [0]
        monkeypatch.setattr(supervision_runtime.time, "monotonic", lambda: elapsed[0])

        def export():
            elapsed[0] += 1
            if elapsed[0] == capture_at:
                acknowledgement = capture.publish(attempt, owner.scope, environ, newest)
                supervision_protocol.write(owner.root / "exporter.checkpoint.json", {**owner.scope, **acknowledgement})

        monkeypatch.setattr(owner, "wait", export)
        result = owner.drain("termination")
        assert result["checkpoint_status"] == status
        assert elapsed[0] == capture_at
        assert result["launch_record"]["checkpoint_acknowledgement"]["status"] == status
        if status == "complete":
            assert result["checkpoint_id"] == f"ckp-{owner.scope['incarnation']}"
            assert runtime.validate_export(attempt, result["checkpoint_id"], environ) is True
        else:
            assert result["checkpoint_id"] is None
