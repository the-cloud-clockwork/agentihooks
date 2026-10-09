from pathlib import Path

target = Path("scripts/ci_mutation/mutant_shards.py")
old = "return set(sorted(names)[index::total])"
text = target.read_text()
assert text.count(old) == 1
target.write_text(text.replace(old, "return set(sorted(names)[index :: total])"))
