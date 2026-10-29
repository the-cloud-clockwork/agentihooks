"""Record the page scripts every Chromium page a test opens ran, for the JavaScript coverage report.

Loaded with `-p tests.browser_coverage`; idle unless JS_COVERAGE_DIR names the capture folder, except that it keeps
xdist workers from erasing the other workers' Python coverage data.
"""

import functools
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

WATCHED = {}


def store(folder: Path, text: str) -> str:
    source = hashlib.sha256(text.encode()).hexdigest()
    stored = folder / "sources" / f"{source}.js"
    if not stored.exists():
        stored.parent.mkdir(parents=True, exist_ok=True)
        partial = stored.with_name(f"{stored.name}.{uuid.uuid4().hex}")
        partial.write_text(text, encoding="utf-8", newline="")
        partial.replace(stored)
    return source


def watch(page):
    session = page.context.new_cdp_session(page)
    session.send("Profiler.enable")
    session.send("Profiler.startPreciseCoverage", {"callCount": True, "detailed": True})
    WATCHED[page] = session, os.environ.get("PYTEST_CURRENT_TEST", "")
    return page


def flush(folder: Path, pages) -> None:
    for page in list(pages):
        session, test = WATCHED.pop(page, (None, ""))
        if session is None or page.is_closed():
            continue
        coverage = session.send("Profiler.takePreciseCoverage")["result"]
        session.send("Debugger.enable")
        result = [
            {
                "source": store(
                    folder, session.send("Debugger.getScriptSource", {"scriptId": entry["scriptId"]})["scriptSource"]
                ),
                "functions": entry["functions"],
            }
            for entry in coverage
            if entry["url"].startswith("http")
        ]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"capture-browser-{uuid.uuid4().hex}.json").write_text(json.dumps({"result": result, "test": test}))


def patches(folder: Path):
    from playwright.sync_api import Browser, BrowserContext, Page

    browser_page, context_page = Browser.new_page, BrowserContext.new_page
    page_close, context_close, browser_close = Page.close, BrowserContext.close, Browser.close

    def close_page(self, *args, **kwargs):
        try:
            flush(folder, [self])
        finally:
            page_close(self, *args, **kwargs)

    def close_context(self, *args, **kwargs):
        try:
            flush(folder, [page for page in WATCHED if page.context == self])
        finally:
            context_close(self, *args, **kwargs)

    def close_browser(self, *args, **kwargs):
        try:
            flush(folder, [page for page in WATCHED if page.context.browser == self])
        finally:
            browser_close(self, *args, **kwargs)

    return [
        (Browser, "new_page", lambda self, *args, **kwargs: watch(browser_page(self, *args, **kwargs))),
        (BrowserContext, "new_page", lambda self, *args, **kwargs: watch(context_page(self, *args, **kwargs))),
        (Page, "close", close_page),
        (BrowserContext, "close", close_context),
        (Browser, "close", close_browser),
    ]


def failed_launch(report) -> None:
    if report.skipped and "no chromium" in str(report.longrepr):
        report.outcome = "failed"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    if os.environ.get("JS_COVERAGE_DIR"):
        failed_launch(outcome.get_result())


@functools.cache
def drives_browser(root: Path, test: str) -> bool:
    from scripts.ci_mutation.browser import needs_browser

    return needs_browser(root, [test])


def track_greenlets(config, path: Path) -> None:
    controller = getattr(config.pluginmanager.getplugin("_cov"), "cov_controller", None)
    if controller is None or not controller.started:
        return
    import greenlet

    # Greenlet tracking runs a Python lookup on every call and return, so only test files that drive Playwright get it.
    browser = drives_browser(config.rootpath, path.relative_to(config.rootpath).as_posix())
    # Only this thread's tracer changes: restarting coverage would stop tracing threads that are already running.
    # Each item starts at the same depth with no measured frame live, so each mode resumes a stack that still matches.
    sys.gettrace().concur_id_func = greenlet.getcurrent if browser else None


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    track_greenlets(item.config, item.path)


@pytest.hookimpl(wrapper=True)
def pytest_load_initial_conftests(early_config, parser, args):
    # xdist starts each worker with the controller's -n, so pytest-cov starts a controller there that erases every
    # .coverage.* file as it starts; a worker that replaces a crashed one would delete the live workers' data.
    if os.environ.get("PYTEST_XDIST_WORKER"):
        early_config.known_args_namespace.cov_append = True
    return (yield)


def pytest_configure(config):
    folder = os.environ.get("JS_COVERAGE_DIR")
    if folder:
        for owner, name, replacement in patches(Path(folder)):
            setattr(owner, name, replacement)
