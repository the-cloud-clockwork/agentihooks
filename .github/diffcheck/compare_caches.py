import json
import sys
from pathlib import Path

first, second = (Path(value) for value in sys.argv[1:3])
names = sorted(path.name for path in first.iterdir())
assert names == sorted(path.name for path in second.iterdir()), "the two caches hold different files"
differ = 0
for name in names:
    one, two = (first / name).read_bytes(), (second / name).read_bytes()
    if one != two and (not name.endswith(".json") or json.loads(one) != json.loads(two)):
        differ += 1
print(f"cache files {len(names)}, differing by value {differ}")
raise SystemExit(1 if differ else 0)
