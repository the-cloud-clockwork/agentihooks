#!/usr/bin/env python3
"""Write a drafted workflow into the folder scripts.workflows.writer chooses."""

import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[5]))
runpy.run_module("scripts.workflows.writer", run_name="__main__")
