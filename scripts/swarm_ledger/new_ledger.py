#!/usr/bin/env python3
"""Create a ledger from an agent-written content file.

Usage: new_ledger.py --content <content.json> (--plan <plan-file> | --slug <slug>) [--date YYYY-MM-DD]

content.json: {"title", "overview", "sources": [paths], "phases": [{"title", "description"}],
               "questions": [{"text"}], "followups": [{"text"}]}
Writes <LEDGER_DIR>/<slug>.html and <slug>.json; slug = <plan-file-stem>-<date>.
Idempotent: an existing ledger is left untouched and its paths are printed.

Usage: new_ledger.py --upgrade <slug>
Re-renders an existing ledger's page from the current template, keeping its token and
its document (the JSON's, folded through a sync first).
"""

import argparse
import datetime
import html
import json
import os
import re
import secrets
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger_core as core  # noqa: E402

TEMPLATE = core.TEMPLATE
LIMITS = {"overview": 200, "phase description": 100}


def slugify(plan, date):
    stem = re.sub(r"[^a-z0-9]+", "-", Path(plan).stem.lower()).strip("-") or "plan"
    return f"{stem}-{date}"


def words(text):
    return len(str(text).split())


def check_types(content):
    if not isinstance(content, dict):
        return ["content must be a JSON object"]
    errors = [f"{k} must be a string" for k in ("title", "overview") if not isinstance(content.get(k, ""), str)]
    if not isinstance(content.get("sources", []), list) or not all(
        isinstance(s, str) for s in content.get("sources", [])
    ):
        errors.append("sources must be a list of path strings")
    for key, fields in (("phases", ("title", "description")), ("questions", ("text",)), ("followups", ("text",))):
        items = content.get(key, [])
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            errors.append(f"{key} must be a list of objects")
            continue
        errors += [
            f"{key}[{n}].{f} must be a string"
            for n, i in enumerate(items, 1)
            for f in fields
            if not isinstance(i.get(f, ""), str)
        ]
    return errors


def check(content):
    errors = check_types(content)
    if errors:
        return errors
    if not str(content.get("title", "")).strip():
        errors.append("title is empty")
    if words(content.get("overview", "")) > LIMITS["overview"]:
        errors.append(f"overview has {words(content['overview'])} words, limit {LIMITS['overview']}")
    for n, phase in enumerate(content.get("phases", []), 1):
        if words(phase.get("description", "")) > LIMITS["phase description"]:
            errors.append(f"phase {n} description has {words(phase['description'])} words, limit 100")
    missing = [s for s in content.get("sources", []) if not Path(os.path.expanduser(s)).exists()]
    errors += [f"source not found: {s}" for s in missing]
    if not content.get("phases"):
        errors.append("no phases")
    return errors


def build_doc(content):
    def items(key, prefix, fields):
        return [
            {"id": f"{prefix}{n}", **{f: item.get(f, "") for f in fields}, **extra(key)}
            for n, item in enumerate(content.get(key, []), 1)
        ]

    def extra(key):
        base = {"comments": []}
        if key in ("phases", "followups"):
            base["done"] = False
        if key == "questions":
            base["answers"] = []
        return base

    return {
        "title": content["title"].strip(),
        "overview": content.get("overview", "").strip(),
        "sources": [os.path.abspath(os.path.expanduser(s)) for s in content.get("sources", [])],
        "phases": items("phases", "p", ("title", "description")),
        "questions": items("questions", "q", ("text",)),
        "orchestrator": content.get("orchestrator", ""),
        "notes": [],
        "chat": [],
        "followups": items("followups", "f", ("text",)),
    }


def render(doc, slug, port):
    values = {
        "TITLE": html.escape(doc["title"]),
        "SLUG": slug,
        "PORT": str(int(port)),
        "TOKEN": secrets.token_urlsafe(24),
        "DATA": core.seed_text(doc, 0),
        "PAGE": core.page_version(),
    }
    page = TEMPLATE.read_text(encoding="utf-8")
    return re.sub(r"__LEDGER_(TITLE|SLUG|PORT|TOKEN|DATA|PAGE)__", lambda m: values[m.group(1)], page)


def upgrade_page(slug):
    html_path, _ = core.paths(slug)
    token = core.read_token(html_path.read_text(encoding="utf-8"))
    if not token:
        sys.exit(f"{html_path} has no ledger token")
    state, _ = core.sync(slug)
    doc = {k: v for k, v in state.items() if k != "_meta"}
    values = {
        "TITLE": html.escape(doc["title"]),
        "SLUG": slug,
        "TOKEN": token,
        "PORT": str(int(os.environ.get("LEDGER_PORT", "8765"))),
        "DATA": core.seed_text(doc, state["_meta"]["rev"]),
        "PAGE": core.page_version(),
    }
    page = re.sub(
        r"__LEDGER_(TITLE|SLUG|PORT|TOKEN|DATA|PAGE)__",
        lambda m: values[m.group(1)],
        TEMPLATE.read_text(encoding="utf-8"),
    )
    core.atomic_write(html_path, page)
    return state


def upgrade(slug):
    state = upgrade_page(slug)
    print(json.dumps({"slug": slug, "html": str(core.paths(slug)[0]), "upgraded": True, "rev": state["_meta"]["rev"]}))


def main():
    if sys.argv[1:2] == ["--upgrade"] and len(sys.argv) == 3:
        return upgrade(sys.argv[2])
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--content", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--plan")
    source.add_argument("--slug")
    parser.add_argument("--date", default=datetime.date.today().isoformat())
    args = parser.parse_args()

    slug = args.slug or slugify(args.plan, args.date)
    html_path, json_path = core.paths(slug)
    if html_path.exists():
        print(json.dumps({"slug": slug, "html": str(html_path), "json": str(json_path), "created": False}))
        return
    if json_path.exists():
        sys.exit(f"{json_path} exists without its HTML; move it aside before creating a new ledger")
    content = json.loads(Path(args.content).read_text(encoding="utf-8"))
    errors = check(content)
    if errors:
        sys.exit("content rejected:\n  " + "\n  ".join(errors))
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    doc = build_doc(content)
    core.validate(doc)
    core.atomic_write(html_path, render(doc, slug, os.environ.get("LEDGER_PORT", "8765")))
    core.sync(slug)
    print(json.dumps({"slug": slug, "html": str(html_path), "json": str(json_path), "created": True}))


if __name__ == "__main__":
    main()
