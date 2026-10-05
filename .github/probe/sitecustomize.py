import atexit, os, sys, time
_T0 = time.time()
def _age():
    with open("/proc/self/stat") as f:
        st = int(f.read().rsplit(")", 1)[1].split()[19])
    with open("/proc/uptime") as f:
        up = float(f.read().split()[0])
    return up - st / os.sysconf("SC_CLK_TCK")
_AGE0 = _age()
_C = {"n": 0, "s": 0.0, "top": {}, "imp": 0.0}
from importlib import _bootstrap_external as _be
_orig = _be.SourceLoader.source_to_code
def _s2c(self, data, path, *a, **k):
    t = time.perf_counter()
    try:
        return _orig(self, data, path, *a, **k)
    finally:
        d = time.perf_counter() - t
        _C["n"] += 1; _C["s"] += d
        parts = str(path).split("site-packages/")
        key = parts[1].split("/")[0] if len(parts) > 1 else "repo:" + str(path).rsplit("/", 2)[-2]
        _C["top"][key] = _C["top"].get(key, 0.0) + d
_be.SourceLoader.source_to_code = _s2c
_marks = []
def mark(tag):
    _marks.append((tag, time.time(), _C["n"], _C["s"]))
def _dump():
    role = os.environ.get("PYTEST_XDIST_WORKER") or ("warm" if os.environ.get("_PROBE_WARM") else "proc")
    out = os.environ.get("PROBE_OUT")
    if not out:
        return
    top = sorted(_C["top"].items(), key=lambda kv: -kv[1])[:8]
    with open(out, "a") as f:
        f.write(f"{os.getpid()} {role} start={_T0:.3f} age0={_AGE0:.2f} end={time.time():.3f} compiled={_C['n']} compile_s={_C['s']:.3f} "
                + " ".join(f"{t}@{w:.3f}:{n}/{s:.2f}" for t, w, n, s in _marks)
                + " top=" + ",".join(f"{k}:{v:.3f}" for k, v in top) + "\n")
atexit.register(_dump)
_real_fork = os.fork
_real_exit = os._exit
def _fork():
    pid = _real_fork()
    if pid == 0:
        global _T0, _AGE0
        _T0 = time.time(); _AGE0 = 0.0
        os.environ["_PROBE_WARM"] = "1"
        _C["n"] = 0; _C["s"] = 0.0; _C["top"] = {}
        del _marks[:]
    return pid
def _exit(code):
    try:
        _dump()
    finally:
        _real_exit(code)
os.fork = _fork
os._exit = _exit
_seen = {"pytest": None}
class _Spy:
    @staticmethod
    def find_spec(name, path=None, target=None):
        if name in ("pytest", "_pytest") and _seen["pytest"] is None:
            _seen["pytest"] = 1
            mark("pytest_import")
        return None
sys.meta_path.insert(0, _Spy)
sys.modules["_probe"] = sys.modules[__name__]
