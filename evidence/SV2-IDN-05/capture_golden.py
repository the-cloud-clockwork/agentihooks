import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

fixture = json.loads(Path(sys.argv[1]).read_text())
with pytest.MonkeyPatch.context() as mp, tempfile.TemporaryDirectory() as root:
    home = Path(root) / fixture["home"]
    mp.setattr("hooks.config.AGENTIHOOKS_HOME", home)
    mp.setattr("hooks.common.LOG_FILE", str(Path(root) / "hooks.log"))
    mp.setattr("hooks.config.BRAIN_URL", "")
    mp.setattr("hooks.config.BRAIN_SOURCE_TYPE", "none")
    mp.delenv("AGENTIHOOKS_SWARM", raising=False)
    mp.delenv("BRAIN_PROJECT_SCOPE", raising=False)
    from hooks.context import project_cache
    from hooks.context.brain_adapter import BrainEntry
    from hooks.context.brain_writer_hook import _marker_request
    from hooks.context.project_identity import ProjectIdentity
    from hooks.context.project_memory import ProjectMemory
    from hooks.context.project_sessions import record_session

    sid = fixture["session_id"]
    identity = ProjectIdentity(**fixture["identity"])
    with patch("hooks.context.project_sessions.enabled", return_value=False):
        record_session(sid, identity)
    project_cache.store_feed([BrainEntry("hot-arcs", "Arcs", "golden")])
    with patch("hooks._async.fork_and_call") as fork:
        empty = project_cache.project_context(sid)
    with patch(
        "hooks.context.project_memory.VaultProjectSource.fetch",
        return_value=ProjectMemory(identity.project, lessons=["golden lesson"]),
    ):
        project_cache.refresh_project_cache(*fork.call_args.args[1:])
    with patch("hooks._async.fork_and_call"):
        full = project_cache.project_context(sid)
    marker = {**fixture["marker"], "attrs": {}, "scope": None}
    print(
        json.dumps(
            {"empty_context": empty, "context": full, "legacy_marker_key": _marker_request(marker, sid)[1]}, indent=2
        )
    )
