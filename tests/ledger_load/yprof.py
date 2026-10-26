import atexit
import os
import runpy
import signal
import sys

import yappi

out = os.environ["YPROF_OUT"]


def dump():
    yappi.stop()
    stats = yappi.get_func_stats()
    stats.save(out + ".pstat", type="pstat")
    with open(out, "w") as handle:
        stats.sort("tsub", "desc").print_all(
            out=handle, columns={0: ("name", 60), 1: ("ncall", 10), 2: ("tsub", 10), 3: ("ttot", 10), 4: ("tavg", 10)}
        )
        yappi.get_thread_stats().print_all(out=handle)


atexit.register(dump)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
yappi.set_clock_type("cpu")
yappi.start(builtins=True)
script = sys.argv[1]
sys.argv = sys.argv[1:]
runpy.run_path(script, run_name="__main__")
