import importlib
import importlib.util
import json
import io
import os
import socket
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

# Every xdist worker of a shard rewrites each file it collects, while stored durations split between workers; measured.
SECONDS_PER_SOURCE_BYTE = 3e-6


def discover_test_files(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in (root / "tests").rglob("test_*.py"))


def source_sizes(root: Path, files: list[str]) -> dict[str, int]:
    return {path: (root / path).stat().st_size for path in files}


def assign_files(durations: dict[str, float], files: list[str], shards: int, sizes: dict[str, int]) -> list[list[str]]:
    seconds = {path: sizes.get(path, 0) * SECONDS_PER_SOURCE_BYTE for path in files}
    for nodeid, duration in durations.items():
        path = nodeid.split("::", 1)[0]
        if path in seconds:
            seconds[path] += duration
    loads = [0.0] * shards
    groups: list[list[str]] = [[] for _ in range(shards)]
    for path in sorted(files, key=lambda f: (-seconds[f], f)):
        lightest = loads.index(min(loads))
        loads[lightest] += seconds[path]
        groups[lightest].append(path)
    return groups


def slowest_first(nodeids: list[str], durations: dict[str, float], floor: float) -> list[str]:
    seconds = {nodeid: durations.get(nodeid.split("@", 1)[0], 0.0) for nodeid in nodeids}
    slow = sorted((nodeid for nodeid in nodeids if seconds[nodeid] >= floor), key=lambda nodeid: -seconds[nodeid])
    return slow + [nodeid for nodeid in nodeids if seconds[nodeid] < floor]


def shard_modules(root: Path, spec: str) -> list[str]:
    index, shards = (int(part) for part in spec.split("/"))
    durations = json.loads((root / ".test_durations").read_text())
    files = discover_test_files(root)
    files = assign_files(durations, files, shards, source_sizes(root, files))[index - 1]
    return [path.removesuffix(".py").replace("/", ".") for path in sorted(files)]


def import_rewritten(name: str) -> None:
    from _pytest.assertion.rewrite import PYC_TAIL, _read_pyc, _rewrite_test, _write_pyc, get_cache_dir, try_makedirs

    spec = importlib.util.find_spec(name)
    source = Path(spec.origin)
    pyc = get_cache_dir(source) / (source.name[:-3] + PYC_TAIL)
    code = _read_pyc(source, pyc)
    if code is None:
        stat, code = _rewrite_test(source, None)
        if try_makedirs(pyc.parent):
            _write_pyc(SimpleNamespace(trace=lambda message: None), code, stat, pyc)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    exec(code, module.__dict__)


def warm_imports(modules: list[str], workers: int, load=importlib.import_module) -> list[int]:
    pids = []
    variant = os.environ.get("WARM_VARIANT", "base")
    if variant == "half":
        workers = max(1, workers // 2)
    for start in range(workers):
        pid = os.fork()
        if pid == 0:
            try:
                if variant == "nice":
                    os.nice(19)
                for module in modules[start::workers]:
                    try:
                        load(module)
                    except BaseException:
                        pass
            finally:
                os._exit(0)
        pids.append(pid)
    return pids


def _read_line(fd: int) -> bytes:
    line = b""
    while not line.endswith(b"\n"):
        chunk = os.read(fd, 1)
        if not chunk:
            break
        line += chunk
    return line


forked_sys_path: list[str] = []


def _run_worker(conn: socket.socket, fds: list[int]) -> None:
    forked_sys_path[:] = sys.path
    try:
        for target, fd in enumerate(fds):
            os.dup2(fd, target)
            os.close(fd)
        sys.stdin = io.TextIOWrapper(io.FileIO(0, closefd=False))
        sys.stdout = io.TextIOWrapper(io.FileIO(1, "w", closefd=False), write_through=True)
        sys.stderr = io.TextIOWrapper(io.FileIO(2, "w", closefd=False), write_through=True)
        exec(eval(_read_line(0)), {"__name__": "__main__"})
    finally:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        finally:
            os._exit(0)


def serve_workers(path: str, modules: list[str], workers: int) -> int:
    pid = os.fork()
    if pid:
        return pid
    try:
        for warm in warm_imports(modules, workers):
            os.waitpid(warm, 0)
        for module in modules:
            try:
                importlib.import_module(module)
            except BaseException:
                pass
        server = socket.socket(socket.AF_UNIX)
        server.bind(path + ".tmp")
        server.listen()
        os.replace(path + ".tmp", path)
        while True:
            conn, _ = server.accept()
            _, fds, _, _ = socket.recv_fds(conn, 1, 3)
            if os.fork() == 0:
                server.close()
                _run_worker(conn, fds)
            conn.close()
            for fd in fds:
                os.close(fd)
            while True:
                try:
                    if os.waitpid(-1, os.WNOHANG)[0] == 0:
                        break
                except ChildProcessError:
                    break
    finally:
        os._exit(0)


def setup_nodes_in_parallel(manager, putevent) -> list:
    manager.config.hook.pytest_xdist_setupnodes(config=manager.config, specs=manager.specs)
    with ThreadPoolExecutor(len(manager.specs)) as pool:
        return list(pool.map(lambda spec: manager.setup_node(spec, putevent), manager.specs))


if __name__ == "__main__":
    import pytest  # noqa: F401
    import tests.conftest  # noqa: F401

    warm_imports(shard_modules(Path(__file__).parent.parent, sys.argv[1]), os.cpu_count() or 1, import_rewritten)
