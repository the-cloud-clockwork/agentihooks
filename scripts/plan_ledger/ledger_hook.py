import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).resolve().parents[1] / "swarm_ledger" / "ledger_hook.py"), run_name="__main__")
