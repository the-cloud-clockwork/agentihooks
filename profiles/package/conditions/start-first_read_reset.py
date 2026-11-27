import json
import sys

from scripts.gates.first_read import reset

reset(json.load(sys.stdin))
