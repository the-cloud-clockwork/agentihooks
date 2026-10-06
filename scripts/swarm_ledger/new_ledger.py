#!/usr/bin/env python3
"""Create a ledger from an agent-written content file.

Usage: new_ledger.py --content <content.json> [--plan <plan-file> | --proof] [--date YYYY-MM-DD]
                     [--size small|swarm] [--as NAME]

--size: small (default) is one session's work without a swarm; its creator, --as NAME (default
$AGENTIHOOKS_AGENT_NAME), joins it as its worker. swarm is a plan a swarm works.

content.json: {"title", "overview", "sources": [paths], "phases": [{"title", "description"}],
               "questions": [{"text"}], "followups": [{"text"}]}
Writes <LEDGER_DIR>/<slug>.html and <slug>.json. The slug is built by scripts.swarm.naming, never typed:
<plan-file-stem>-<date> for --plan, proof-<swarm code>-<task>-<n> for --proof (a swarm task session),
small-<session> for a small ledger. --slug accepts only one of those built forms.
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
import ledger_link  # noqa: E402
import ledger_size  # noqa: E402

TEMPLATE = core.TEMPLATE
LIMITS = {"overview": 200, "phase description": 100}


def built_slug(args):
    from scripts.swarm import naming

    try:
        if args.plan:
            return naming.plan_slug(args.plan, args.date)
        if args.proof:
            return naming.proof_slug(os.environ, {path.stem for path in core.LEDGER_DIR.glob("proof-*")})
        if args.slug is None and args.size == "small":
            return naming.small_slug(os.environ)
    except naming.NamingError as error:
        sys.exit(str(error))
    if naming.is_built_slug(args.slug):
        return args.slug
    sys.exit(
        "ledger names come from code: give --plan <file>, --proof from a swarm task session, or no name for a "
        "small ledger"
    )


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
    for key, fields in (
        ("phases", ("title", "description")),
        ("questions", ("text",)),
        ("followups", ("text",)),
        ("tasks", ("title", "description", "phase", "lane")),
    ):
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
    if not errors:
        from scripts.swarm_ledger import ledger_phases

        try:
            ledger_phases.validate(
                [
                    {"id": f"p{n}", **phase_fields("phases", phase)}
                    for n, phase in enumerate(content.get("phases", []), 1)
                ]
            )
        except ValueError as exc:
            errors.append(str(exc))
    for n, task in enumerate(content.get("tasks", []) if isinstance(content.get("tasks", []), list) else [], 1):
        if isinstance(task, dict) and task.get("lane", "eng") not in ("eng", "ci"):
            errors.append(f"tasks[{n}].lane must be eng or ci")
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


def phase_fields(key: str, item: dict) -> dict:
    fields = {name: item[name] for name in ("depends_on", "planning", "release") if key == "phases" and name in item}
    if isinstance(fields.get("depends_on"), list):
        fields["depends_on"] = [f"p{value}" if type(value) is int else value for value in fields["depends_on"]]
    return fields


def build_doc(content, size="small"):
    def items(key, prefix, fields):
        return [
            {"id": f"{prefix}{n}", **{f: item.get(f, "") for f in fields}, **extra(key), **phase_fields(key, item)}
            for n, item in enumerate(content.get(key, []), 1)
        ]

    def extra(key):
        base = {"comments": []}
        if key in ("phases", "followups", "tasks"):
            base["done"] = False
        if key == "questions":
            base["answers"] = []
        return base

    return {
        **({"size": size} if size else {}),
        "title": content["title"].strip(),
        "overview": content.get("overview", "").strip(),
        "sources": [os.path.abspath(os.path.expanduser(s)) for s in content.get("sources", [])],
        "phases": items("phases", "p", ("title", "description")),
        "questions": items("questions", "q", ("text",)),
        "orchestrator": content.get("orchestrator", ""),
        "notes": [],
        "chat": [],
        "followups": items("followups", "f", ("text",)),
        "tasks": [
            {**task, "lane": task["lane"] or "eng", "state": "open", "claimed_by": "", "issue_url": "", "pr_url": ""}
            for task in items("tasks", "t", ("title", "description", "phase", "lane"))
        ],
    }


def render(doc, slug, port):
    values = {
        "TITLE": html.escape(doc["title"]),
        "SLUG": slug,
        "PORT": str(int(port)),
        "TOKEN": secrets.token_urlsafe(24),
        "DATA": core.seed_text(doc, 0),
        "PAGE": core.page_version(),
        "PALETTE": core.PALETTE.read_text(encoding="utf-8"),
    }
    page = TEMPLATE.read_text(encoding="utf-8")
    return re.sub(r"__LEDGER_(TITLE|SLUG|PORT|TOKEN|DATA|PAGE|PALETTE)__", lambda m: values[m.group(1)], page)


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
        "PORT": str(int(ledger_link.address()[1])),
        "DATA": core.seed_text(doc, state["_meta"]["rev"]),
        "PAGE": core.page_version(),
        "PALETTE": core.PALETTE.read_text(encoding="utf-8"),
    }
    page = re.sub(
        r"__LEDGER_(TITLE|SLUG|PORT|TOKEN|DATA|PAGE|PALETTE)__",
        lambda m: values[m.group(1)],
        TEMPLATE.read_text(encoding="utf-8"),
    )
    core.atomic_write(html_path, page)
    return state


def upgrade(slug):
    state = upgrade_page(slug)
    print(json.dumps({"slug": slug, "html": str(core.paths(slug)[0]), "upgraded": True, "rev": state["_meta"]["rev"]}))


def create(slug, content, size="small"):
    html_path, json_path = core.paths(slug)
    if html_path.exists():
        return False
    if json_path.exists():
        sys.exit(f"{json_path} exists without its HTML; move it aside before creating a new ledger")
    errors = check(content)
    if errors:
        sys.exit("content rejected:\n  " + "\n  ".join(errors))
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    doc = build_doc(content, size)
    core.validate(doc)
    core.atomic_write(html_path, render(doc, slug, ledger_link.address()[1]))
    core.sync(slug)
    return True


def main():
    if sys.argv[1:2] == ["--upgrade"] and len(sys.argv) == 3:
        return upgrade(sys.argv[2])
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--content", required=True)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--plan")
    source.add_argument("--proof", action="store_true")
    source.add_argument("--slug")
    parser.add_argument("--date", default=datetime.date.today().isoformat())
    parser.add_argument("--size", choices=ledger_size.SIZES, default="small")
    parser.add_argument("--as", dest="name", default=os.environ.get("AGENTIHOOKS_AGENT_NAME", ""))
    args = parser.parse_args()

    slug = built_slug(args)
    html_path, json_path = core.paths(slug)
    if html_path.exists():
        print(json.dumps({"slug": slug, "html": str(html_path), "json": str(json_path), "created": False}))
        print(ledger_link.page_line(slug))
        return
    small = args.size == "small"
    if small and not ledger_size.AUTHOR_RE.match(args.name):
        sys.exit("a small ledger needs --as NAME: the session that creates it joins it as its worker")
    create(slug, json.loads(Path(args.content).read_text(encoding="utf-8")), args.size)
    out = {"slug": slug, "html": str(html_path), "json": str(json_path), "created": True, "size": args.size}
    if small:
        core.sync(slug, ops=[{"op": "join", "id": f"join-{secrets.token_hex(5)}", "by": args.name, "role": "member"}])
        out["joined"] = args.name
    print(json.dumps(out))
    print(ledger_link.page_line(slug))


if __name__ == "__main__":
    main()
