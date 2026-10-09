"""Record the page scripts every Chromium page a test opens ran, for the JavaScript coverage report.

Loaded with `-p tests.browser_coverage`; idle unless JS_COVERAGE_DIR names the capture folder.
"""

import hashlib
import json
import os
import uuid
from pathlib import Path

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
    WATCHED[page] = session
    return page


def flush(folder: Path, pages) -> None:
    for page in list(pages):
        session = WATCHED.pop(page, None)
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
        (folder / f"capture-browser-{uuid.uuid4().hex}.json").write_text(json.dumps({"result": result}))


def patches(folder: Path):
    from playwright.sync_api import Browser, BrowserContext, Page

    browser_page, context_page = Browser.new_page, BrowserContext.new_page
    page_close, context_close, browser_close = Page.close, BrowserContext.close, Browser.close

    def close_page(self, *args, **kwargs):
        flush(folder, [self])
        return page_close(self, *args, **kwargs)

    def close_context(self, *args, **kwargs):
        flush(folder, [page for page in WATCHED if page.context == self])
        return context_close(self, *args, **kwargs)

    def close_browser(self, *args, **kwargs):
        flush(folder, [page for page in WATCHED if page.context.browser == self])
        return browser_close(self, *args, **kwargs)

    return [
        (Browser, "new_page", lambda self, *args, **kwargs: watch(browser_page(self, *args, **kwargs))),
        (BrowserContext, "new_page", lambda self, *args, **kwargs: watch(context_page(self, *args, **kwargs))),
        (Page, "close", close_page),
        (BrowserContext, "close", close_context),
        (Browser, "close", close_browser),
    ]


def pytest_configure(config):
    folder = os.environ.get("JS_COVERAGE_DIR")
    if folder:
        for owner, name, replacement in patches(Path(folder)):
            setattr(owner, name, replacement)
