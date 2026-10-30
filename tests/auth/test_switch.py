import subprocess
import sys
from pathlib import Path

from scripts.auth import switch

ROOT = Path(__file__).resolve().parents[2]


def test_the_legacy_switch_comes_from_the_environment():
    assert switch.legacy({}) is True
    assert switch.legacy({"AGENTIHOOKS_AUTH_LEGACY": "on"}) is True
    assert switch.legacy({"AGENTIHOOKS_AUTH_LEGACY": "off"}) is False


def test_hive_auth_imports_without_the_token_library():
    probe = "import sys; sys.modules['jwt'] = None; import scripts.hive.auth"
    result = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
