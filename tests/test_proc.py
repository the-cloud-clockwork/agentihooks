import ast
import types
from pathlib import Path

from hooks.proc import Process, _process, _target, processes

READERS = {"processes", "_process"}
PROC_ROOT = "/proc/"


def fake_proc(root: Path, pid: int, *, comm="claude", argv=("claude",), ppid=1, start=4242, state="S") -> None:
    entry = root / str(pid)
    entry.mkdir(parents=True)
    rest = [state, str(ppid), str(pid + 1), str(pid + 2)] + ["0"] * 15 + [str(start), "0", "0"]
    (entry / "stat").write_text(f"{pid} ({comm}) " + " ".join(rest) + "\n", encoding="utf-8")
    (entry / "cmdline").write_bytes(b"\0".join(item.encode() for item in argv) + b"\0")
    (entry / "comm").write_text(comm + "\n", encoding="utf-8")


def item(comm="node", argv=()):
    return Process(1, 0, 1, 1, 1, "S", comm, tuple(argv))


def test_process_reads_identity_fields(tmp_path):
    fake_proc(tmp_path, 300, argv=("claude", "--name", "eng"), ppid=7, start=987654)
    assert _process(300, tmp_path) == Process(300, 7, 301, 302, 987654, "S", "claude", ("claude", "--name", "eng"))


def test_process_comm_with_spaces_and_parens(tmp_path):
    fake_proc(tmp_path, 301, comm="my (odd) name", argv=("x",), start=55)
    found = _process(301, tmp_path)
    assert (found.ppid, found.start_time, found.comm) == (1, 55, "my (odd) name")


def test_process_missing_or_truncated_is_none(tmp_path):
    assert _process(999, tmp_path) is None
    fake_proc(tmp_path, 302)
    (tmp_path / "302" / "stat").write_text("302 (claude) S", encoding="utf-8")
    assert _process(302, tmp_path) is None


def test_processes_skips_non_numeric_and_broken(tmp_path):
    fake_proc(tmp_path, 400)
    fake_proc(tmp_path, 401)
    (tmp_path / "self").mkdir()
    (tmp_path / "402").mkdir()
    assert sorted(processes(tmp_path)) == [400, 401]


def test_processes_missing_root_is_empty(tmp_path):
    assert processes(tmp_path / "absent") == {}


def test_target_detects_claude_and_codex():
    assert _target(item(comm="claude")) == "claude"
    assert _target(item(argv=("node", "/opt/x/claude-code/cli.js"))) == "claude"
    assert _target(item(argv=("/usr/bin/codex",))) == "codex"
    assert _target(item(comm="codex-linux-x64")) == "codex"
    assert _target(item(comm="bash", argv=("bash", "-c", "claude"))) == ""


def _names_a_process(node):
    return any(
        isinstance(part, ast.JoinedStr)
        and len(part.values) > 1
        and isinstance(part.values[0], ast.Constant)
        and part.values[0].value == PROC_ROOT
        and isinstance(part.values[1], ast.FormattedValue)
        for part in ast.walk(node)
    )


def _process_table_reads(check, process_paths):
    count = 0
    for call in ast.walk(check):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if isinstance(func, ast.Name):
            count += func.id in READERS or (func.id == "open" and bool(call.args) and _names_a_process(call.args[0]))
        elif isinstance(func, ast.Attribute) and func.attr in READERS:
            count += 1
        elif isinstance(func, ast.Attribute) and func.attr in {"exists", "is_file", "read_text", "read_bytes", "open"}:
            receiver = func.value
            count += (isinstance(receiver, ast.Name) and receiver.id in process_paths) or _names_a_process(receiver)
    return count


def _code_reads_the_process_table(code):
    if READERS & {*code.co_names, *code.co_varnames, *code.co_cellvars, *code.co_freevars}:
        return True
    return any(
        const == PROC_ROOT or (isinstance(const, types.CodeType) and _code_reads_the_process_table(const))
        for const in code.co_consts
    )


def _process_table_violations(tests):
    found = []
    for path in sorted(tests.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if not _code_reads_the_process_table(compile(source, str(path), "exec", dont_inherit=True, optimize=0)):
            continue
        for function in ast.walk(ast.parse(source)):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            process_paths = {
                target.id
                for assign in ast.walk(function)
                if isinstance(assign, ast.Assign) and _names_a_process(assign.value)
                for target in assign.targets
                if isinstance(target, ast.Name)
            }
            for statement in ast.walk(function):
                if isinstance(statement, (ast.If, ast.While)):
                    check = statement.test
                elif isinstance(statement, (ast.Assert, ast.Expr, ast.Return, ast.Assign)):
                    check = statement
                else:
                    continue
                if _process_table_reads(check, process_paths) > 1:
                    found.append(f"{path.relative_to(tests)}:{statement.lineno}")
    return sorted(set(found))


def test_no_check_reads_the_process_table_twice_in_one_statement():
    assert _process_table_violations(Path(__file__).resolve().parent) == []


def test_process_table_scan_skips_irrelevant_files_and_refuses_double_reads(tmp_path, monkeypatch):
    irrelevant = "def check(path):\n    assert path.exists() and open(path).read() and path.is_file()\n"
    (tmp_path / "irrelevant.py").write_text(irrelevant, encoding="utf-8")
    sources = {
        "processes.py": "def check():\n    assert 1 not in processes() or processes()[1].state == 'Z'\n",
        "process.py": "def check():\n    assert _process(1) == _process(1)\n",
        "path.py": 'def check(pid):\n    path = Path(f"/proc/{pid}/stat")\n    assert path.exists() and path.read_text()\n',
        "escaped.py": 'def check(pid):\n    path = Path(f"/pr\\x6fc/{pid}/stat")\n    assert path.exists() and path.read_text()\n',
        "adjacent.py": 'def check(pid):\n    path = Path(f"/pr" f"oc/{pid}/stat")\n    assert path.exists() and path.read_text()\n',
        "unicode.py": 'def check(pid):\n    assert ｏｐｅｎ(f"/pr\\x6fc/{pid}/stat") and ｏｐｅｎ(f"/pr\\x6fc/{pid}/stat")\n',
        "snapshot.py": "def check():\n    snapshot = processes()\n    assert 1 not in snapshot or snapshot[1].state == 'Z'\n",
    }
    for name, source in sources.items():
        (tmp_path / name).write_text(source, encoding="utf-8")
    parse = ast.parse

    def parse_relevant(source, *args, **kwargs):
        assert source != irrelevant, "irrelevant files must not be parsed"
        return parse(source, *args, **kwargs)

    monkeypatch.setattr(ast, "parse", parse_relevant)
    assert _process_table_violations(tmp_path) == [
        "adjacent.py:3",
        "escaped.py:3",
        "path.py:3",
        "process.py:2",
        "processes.py:2",
        "unicode.py:2",
    ]
