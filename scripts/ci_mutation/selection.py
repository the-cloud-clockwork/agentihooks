import json
import sys
from pathlib import Path


def run_selected(selection: Path) -> None:
    import mutmut
    from mutmut.__main__ import cli

    changes = json.loads(selection.read_text())
    # mutmut 3.6.0 consumes this line map before generation, independently of test coverage.
    mutmut._covered_lines = {str((Path("mutants") / path).absolute()): set(lines) for path, lines in changes.items()}
    for name in ("scripts.ci_mutation", "scripts"):
        sys.modules.pop(name)
    cli(["run", "--max-children", "1"])


if __name__ == "__main__":
    run_selected(Path(sys.argv[1]))
