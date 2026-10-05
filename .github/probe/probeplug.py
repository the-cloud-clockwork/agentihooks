import _probe
def pytest_configure(config):
    _probe.mark("configure")
def pytest_collection_finish(session):
    _probe.mark("collected")
def pytest_runtestloop(session):
    _probe.mark("loop")
