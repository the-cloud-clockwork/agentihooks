import os
import sys
import time

_T0 = time.time()


def _pstart():
    tick = os.sysconf("SC_CLK_TCK")
    st = float(open("/proc/self/stat").read().rsplit(")", 1)[1].split()[19]) / tick
    up = float(open("/proc/uptime").read().split()[0])
    return time.time() - (up - st)


_PS = _pstart()


def _w(config, what):
    wid = getattr(config, "workerinput", {}).get("workerid", "ctl")
    sys.stderr.write(f"STAMP {time.time():.3f} {wid} {what} pstart={_PS:.3f} plugin={_T0:.3f}\n")
    sys.stderr.flush()


def pytest_configure(config):
    _w(config, "configure")


def pytest_sessionstart(session):
    _w(session.config, "sessionstart")


def pytest_collection_finish(session):
    _w(session.config, "collect_finish")
