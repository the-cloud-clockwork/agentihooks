#!/usr/bin/env python3
import runpy
from pathlib import Path

here = Path(__file__).resolve()
guard = runpy.run_path(str(here.parents[3] / "_guards" / "no_code_edits.py"))
raise SystemExit(guard["main"](here.parents[2].name))
