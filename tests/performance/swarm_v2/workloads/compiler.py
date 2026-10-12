"""Compiler heavy workload: generated C units built with the given number of parallel compiler jobs, then linked."""

import argparse
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

UNITS = 24
FUNCTIONS = 120
FUNCTION = """
static double unit{unit}_f{index}(const double *v, int n) {{
    double acc = {index}.0;
    for (int i = 0; i < n; i++) {{
        for (int j = 0; j < {width}; j++) {{
            acc += v[(i * {index} + j) % n] * (double)(j + {unit}) / (double)(i + 1);
            if (acc > 1e9) acc = acc / 3.0 - j;
        }}
    }}
    return acc;
}}
"""


def generate(root: Path) -> list[Path]:
    sources = []
    for unit in range(UNITS):
        body = "".join(FUNCTION.format(unit=unit, index=i + 1, width=8 + i % 9) for i in range(FUNCTIONS))
        calls = " + ".join(f"unit{unit}_f{i + 1}(v, n)" for i in range(FUNCTIONS))
        source = root / f"unit{unit}.c"
        source.write_text(f"{body}\ndouble unit{unit}(const double *v, int n) {{ return {calls}; }}\n")
        sources.append(source)
    calls = " + ".join(f"unit{u}(v, 64)" for u in range(UNITS))
    externs = "".join(f"double unit{u}(const double *v, int n);\n" for u in range(UNITS))
    main = root / "main.c"
    main.write_text(f"{externs}int main(void) {{ double v[64] = {{1.0}}; return ({calls}) > 0 ? 0 : 1; }}\n")
    return [*sources, main]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--children", type=int, required=True)
    args = parser.parse_args()
    compiler = shutil.which("cc")
    if not compiler:
        return 3
    root = Path.cwd()

    def build(source: Path) -> int:
        return subprocess.call([compiler, "-O2", "-c", str(source), "-o", str(source.with_suffix(".o"))])

    with ThreadPoolExecutor(max_workers=max(args.children, 1)) as pool:
        failed = any(list(pool.map(build, generate(root))))
    objects = sorted(str(p) for p in root.glob("*.o"))
    return int(failed) or subprocess.call([compiler, *objects, "-o", str(root / "program")])


if __name__ == "__main__":
    sys.exit(main())
