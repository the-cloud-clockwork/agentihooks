"""Give each test one coverage context, named `<node id>|run`, for the whole of its setup, call and teardown.

Loaded with `-p tests.cov_contexts` in place of `--cov-context=test`, which switches three times per test, and each
switch writes the data gathered since the last one to the coverage database. Every reader drops the phase suffix.
Idle while pytest-cov is not measuring.
"""

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    controller = getattr(item.config.pluginmanager.getplugin("_cov"), "cov_controller", None)
    if controller is not None and controller.started:
        controller.cov.switch_context(f"{item.nodeid}|run")
