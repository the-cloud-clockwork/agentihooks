import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))
import ledger_core  # noqa: E402


def derived_paths(path):
    return {
        "ledger_server": {"PIDFILE": path / ".server.pid", "LOGFILE": path / ".server.log"},
        "ledger_hook": {"LEDGER_DIR": path, "SESSIONS": path / ".sessions"},
    }


@pytest.fixture(scope="module", autouse=True)
def ledger_dir(tmp_path_factory):
    path = tmp_path_factory.mktemp("ledger")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("LEDGER_DIR", str(path))
        patch.setattr(ledger_core, "LEDGER_DIR", path)
        for module, values in derived_paths(path).items():
            if module in sys.modules:
                for name, value in values.items():
                    patch.setattr(sys.modules[module], name, value)
        yield path
