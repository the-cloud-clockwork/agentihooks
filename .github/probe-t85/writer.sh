python3 - <<'PY'
import json
import os
from pathlib import Path
from scripts.ci_mutation.clearances import write_clearance
root = Path.cwd()
records = json.loads(Path(os.environ['RUNNER_TEMP'], 'rulings.json').read_text())
for record in records:
    write_clearance(root, record['key'], {'reader': 'Standards reader', 'reason': record['reason']})
print(len(records))
PY
