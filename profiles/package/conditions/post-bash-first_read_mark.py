import json
import sys

from scripts.gates.first_read import mark

mark(json.load(sys.stdin))
