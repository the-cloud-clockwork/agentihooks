import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

from uv import find_uv_bin

REPO = Path(__file__).resolve().parents[1]
WHEEL_VERSION = "0.0.0.dev4242"


def _run(cmd, **kwargs):
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600, **kwargs)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def _tagged_wheel(tmp_path):
    src = tmp_path / "src"
    ignore = shutil.ignore_patterns(
        ".git", ".venv", "mutants", ".mutation-gate", "__pycache__", ".pytest_cache", "dist", "build", "*.egg-info"
    )
    shutil.copytree(REPO, src, ignore=ignore)
    pyproject = src / "pyproject.toml"
    text = pyproject.read_text()
    current = next(line for line in text.splitlines() if line.startswith("version = "))
    pyproject.write_text(text.replace(current, f'version = "{WHEEL_VERSION}"', 1))
    _run([find_uv_bin(), "build", "--wheel", "-o", str(tmp_path / "dist"), str(src)])
    return next((tmp_path / "dist").glob("agentihooks-*.whl"))


def _venv_with(tmp_path, wheel):
    venv = tmp_path / "venv"
    _run([sys.executable, "-m", "venv", str(venv)])
    python = venv / "bin" / "python"
    site = _run([str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"]).strip()
    Path(site, "suite-deps.pth").write_text(sysconfig.get_path("purelib") + "\n")
    _run([find_uv_bin(), "pip", "install", "--python", str(python), "--no-deps", str(wheel)])
    return venv / "bin"


def test_init_from_a_wheel_leaves_that_wheel_on_the_path(tmp_path):
    bin_dir = _venv_with(tmp_path, _tagged_wheel(tmp_path))
    home = tmp_path / "home"
    home.mkdir()
    uv_dir = tmp_path / "uv"
    uv_dir.mkdir()
    (uv_dir / "uv").symlink_to(find_uv_bin())
    env = {"HOME": str(home), "PATH": f"{bin_dir}:{uv_dir}:/usr/bin:/bin", "TERM": "dumb"}

    _run([str(bin_dir / "agentihooks"), "init"], env=env, cwd=home)

    assert (
        _run([str(home / ".local" / "bin" / "agentihooks"), "--version"], env=env).strip()
        == f"agentihooks {WHEEL_VERSION}"
    )
