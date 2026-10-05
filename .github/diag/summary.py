import collections
import re
import sys

tops = collections.Counter()
for line in open(sys.argv[1]):
    if line.startswith("DIAG"):
        print(line.rstrip())
        continue
    m = re.match(r"import time:\s+(\d+) \|\s+(\d+) \|( *)(\S+)", line)
    if m:
        tops[m[4].split(".")[0]] += int(m[1])
print("total import self ms", sum(tops.values()) / 1000)
for k, v in tops.most_common(25):
    print(f"{v / 1000:8.1f} {k}")
