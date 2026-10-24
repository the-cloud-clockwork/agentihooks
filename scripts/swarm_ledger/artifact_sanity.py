"""Formatting checks for the ledger artifact viewer, measured in a headless browser at the operator's viewport."""

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

SHELL = Path(__file__).resolve().parent / "shell.html"
PAGE_URL = "http://127.0.0.1:9/artifact-sanity"
VIEWPORT = {"width": 1920, "height": 1080}
FIRST_STATE = """async () => {
          const main = document.querySelector("script[type=module][src$='/js/main.js']").src;
          await (await import(new URL("sync.js", main))).loaded;
          return true;
        }"""
BASELINE_CH = 90
WIDTH_FACTOR = 2
TOKEN_MAX = 40
MIN_COLUMN_CH = 8
TYPES = {
    ".md": "text/markdown",
    ".json": "application/json",
    ".svg": "image/svg+xml",
}

FENCE = re.compile(r"^\s*(```|~~~)")
HEADING = re.compile(r"^#{1,6}\s")
LIST = re.compile(r"^\s*([-*+]|\d+[.)])\s+")
TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}")
RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
BREAK = re.compile(r"^(#{1,6}\s|\s*```|\s*~~~|>)")

MEASURE = """
(viewer) => {
  const body = viewer.querySelector(".art-body");
  const doc = body.querySelector(".art-doc");
  const inside = (el) => el.getBoundingClientRect().right <= body.getBoundingClientRect().right + 1;
  const close = viewer.querySelector(".image-close").getBoundingClientRect();
  const out = {
    kind: doc ? "markdown" : body.querySelector(".json-tree") ? "json" : body.querySelector("img") ? "image" : "none",
    page_overflow: document.documentElement.scrollWidth > window.innerWidth,
    body_overflow: body.scrollWidth > body.clientWidth + 1,
    close_visible: close.width > 0 && close.left >= 0 && close.right <= window.innerWidth && close.top >= 0,
    json_folds: body.querySelectorAll(".json-tree details").length,
  };
  const img = body.querySelector("img");
  if (img) out.image = { loaded: img.complete && img.naturalWidth > 0, inside: inside(img) };
  if (!doc) return out;
  const probe = document.createElement("span");
  probe.textContent = "0".repeat(100);
  probe.style.cssText = "position:absolute;visibility:hidden;white-space:pre";
  doc.append(probe);
  out.ch = probe.getBoundingClientRect().width / 100;
  probe.remove();
  out.width = doc.getBoundingClientRect().width;
  out.font_px = parseFloat(getComputedStyle(doc).fontSize);
  out.page_font_px = parseFloat(getComputedStyle(document.body).fontSize);
  out.counts = {
    h: doc.querySelectorAll("h1, h2, h3, h4, h5, h6").length,
    li: doc.querySelectorAll("li").length,
    table: doc.querySelectorAll("table").length,
    pre: doc.querySelectorAll("pre").length,
  };
  out.boxes_inside = [...doc.querySelectorAll("pre, table")].every(inside);
  out.clipped = [...doc.querySelectorAll("*")].filter((el) => {
    const x = getComputedStyle(el).overflowX;
    return (x === "hidden" || x === "clip") && el.scrollWidth > el.clientWidth + 1;
  }).map((el) => el.tagName.toLowerCase());
  const broken = [];
  const walker = document.createTreeWalker(doc, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    for (const m of node.data.matchAll(/\\S+/g)) {
      if (m[0].length > TOKEN_MAX) continue;
      const range = document.createRange();
      range.setStart(node, m.index);
      range.setEnd(node, m.index + m[0].length);
      const tops = new Set([...range.getClientRects()].filter((r) => r.width > 0).map((r) => Math.round(r.top)));
      if (tops.size > 1) broken.push(m[0]);
    }
  }
  out.broken_tokens = broken;
  out.tiny_cells = [...doc.querySelectorAll("td, th")].filter((cell) => {
    const style = getComputedStyle(cell);
    const inner = cell.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    return cell.textContent.trim().length > MIN_COLUMN_CH && inner < MIN_COLUMN_CH * out.ch;
  }).map((cell) => cell.textContent.trim().slice(0, TOKEN_MAX));
  return out;
}
"""


def expected_blocks(text: str) -> Counter:
    lines = re.split(r"\r\n?|\n", text)
    counts = Counter()
    end = 0
    for i in range(len(lines)):
        if i >= end:
            end = _block(lines, i, counts)
    return counts


def _block(lines, i, counts):
    line = lines[i]
    if fence := FENCE.match(line):
        counts["pre"] += 1
        return _run(lines, i + 1, lambda row: not row.strip().startswith(fence[1])) + 1
    if HEADING.match(line):
        counts["h"] += 1
        return i
    if "|" in line and i + 1 < len(lines) and TABLE_RULE.match(lines[i + 1]):
        counts["table"] += 1
        return _run(lines, i + 2, lambda row: "|" in row)
    if LIST.match(line):
        end = _run(lines, i, LIST.match)
        counts["li"] += end - i
        return end
    if line.startswith(">"):
        end = _run(lines, i, lambda row: row.startswith(">"))
        counts.update(expected_blocks("\n".join(re.sub(r"^>\s?", "", row) for row in lines[i:end])))
        return end
    if RULE.match(line) or not line.strip():
        return i
    return _run(lines, i + 1, lambda row: row.strip() and not BREAK.match(row) and not LIST.match(row))


def _run(lines, i, keep):
    return next((j for j in range(i, len(lines)) if not keep(lines[j])), len(lines))


def check(measure: dict, expected: Counter | None = None) -> list[str]:
    failures = []
    if measure["page_overflow"]:
        failures.append("the page scrolls sideways")
    if measure["body_overflow"]:
        failures.append("the viewer body scrolls sideways")
    if not measure["close_visible"]:
        failures.append("the viewer close control is clipped")
    kind = measure["kind"]
    if kind == "markdown":
        failures += _markdown(measure, expected or Counter())
    elif kind == "json" and not measure["json_folds"]:
        failures.append("the JSON viewer shows no folds")
    elif kind == "image" and not (measure["image"]["loaded"] and measure["image"]["inside"]):
        failures.append("the image did not load inside the viewer")
    elif kind == "none":
        failures.append("the viewer rendered nothing")
    return failures


def _markdown(measure, expected):
    failures = []
    floor = WIDTH_FACTOR * BASELINE_CH * measure["ch"]
    if measure["width"] < floor - 1:
        failures.append(
            f"reading width {measure['width']:.0f}px is under {WIDTH_FACTOR} x {BASELINE_CH}ch ({floor:.0f}px)"
        )
    if measure["font_px"] != measure["page_font_px"]:
        failures.append(f"font size {measure['font_px']}px differs from the page {measure['page_font_px']}px")
    for block in ("h", "li", "table", "pre"):
        if measure["counts"][block] != expected[block]:
            failures.append(f"{block} rendered {measure['counts'][block]}, source has {expected[block]}")
    if not measure["boxes_inside"]:
        failures.append("a table or code block reaches past the viewer")
    if measure["clipped"]:
        failures.append(f"clipped text in {', '.join(sorted(set(measure['clipped'])))}")
    if measure["broken_tokens"]:
        failures.append(f"{len(measure['broken_tokens'])} words broken mid word, first {measure['broken_tokens'][0]}")
    if measure["tiny_cells"]:
        failures.append(
            f"{len(measure['tiny_cells'])} table cells under {MIN_COLUMN_CH}ch, first {measure['tiny_cells'][0]}"
        )
    return failures


def ledger_doc(files: list[Path]) -> dict:
    rows = [
        {"id": f"art-{n}", "title": path.name, "file": {"id": path.name, "type": TYPES[path.suffix]}}
        for n, path in enumerate(files)
    ]
    return {"title": "Artifact sanity", "artifacts": rows, "_meta": {"rev": 1}}


def page_html() -> str:
    values = {"TOKEN": "", "PAGE": "0" * 12, "SLUG": "artifact-sanity", "PORT": "9", "TITLE": "Artifact sanity"}
    return re.sub(r"__LEDGER_(TOKEN|PAGE|SLUG|PORT|TITLE)__", lambda m: values[m.group(1)], SHELL.read_text())


def assets() -> dict[str, Path]:
    if str(SHELL.parent) not in sys.path:
        sys.path.insert(0, str(SHELL.parent))
    from scripts.swarm_ledger import ledger_core

    return ledger_core.static_assets()


def _asset(route, served):
    path = served.get(urlsplit(route.request.url).path.split("/", 3)[3])
    if path is None:
        return route.fulfill(status=404, body="no such asset")
    kind = "text/css" if path.suffix == ".css" else "text/javascript"
    return route.fulfill(body=path.read_text(), content_type=f"{kind}; charset=utf-8")


def _events(route, doc):
    data = json.dumps({"ledger": doc, "swarm": None})
    route.fulfill(body=f"id: c0\nevent: snapshot\ndata: {data}\n\n", content_type="text/event-stream")


def run(browser, files: list[Path]) -> dict[str, list[str]]:
    by_name = {path.name: path for path in files}
    doc, served = ledger_doc(files), assets()
    tab = browser.new_page(viewport=VIEWPORT)
    try:
        tab.route(
            "**/artifacts/**",
            lambda route: route.fulfill(
                body=by_name[route.request.url.rsplit("/", 1)[1]].read_bytes(),
                content_type=TYPES[Path(route.request.url).suffix],
            ),
        )
        html = page_html()
        tab.route(PAGE_URL, lambda route: route.fulfill(body=html, content_type="text/html; charset=utf-8"))
        tab.route("**/static/*/**", lambda route: _asset(route, served))
        tab.route("**/api/v1/ledgers/*/events", lambda route: _events(route, doc))
        tab.goto(PAGE_URL)
        tab.wait_for_function(FIRST_STATE)
        return {path.name: _view(tab, path) for path in files}
    finally:
        tab.close()


def _view(tab, path):
    if tab.locator("#art-panel").is_hidden():
        tab.locator("#art-fab").click()
    tab.locator("#art-list .art-title").get_by_text(path.name, exact=True).click()
    viewer = tab.locator("dialog.image-viewer[open]")
    viewer.locator(".art-body > :not(.hint)").first.wait_for()
    tab.wait_for_function("v => [...v.querySelectorAll('img')].every(i => i.complete)", arg=viewer.element_handle())
    script = MEASURE.replace("TOKEN_MAX", str(TOKEN_MAX)).replace("MIN_COLUMN_CH", str(MIN_COLUMN_CH))
    measure = viewer.evaluate(script)
    viewer.locator(".image-close").click()
    expected = expected_blocks(path.read_text()) if path.suffix == ".md" else None
    return check(measure, expected)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args(argv)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            report = run(browser, args.files)
        finally:
            browser.close()
    print(json.dumps(report))
    return 1 if any(report.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
