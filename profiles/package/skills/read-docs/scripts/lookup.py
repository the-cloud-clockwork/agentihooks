#!/usr/bin/env python3
import argparse
import json
import re
from urllib.error import URLError
from urllib.parse import urljoin
from urllib.request import urlopen

SITE = "https://the-cloud-clockwork.github.io/agentihooks/"
INDEX = SITE + "assets/js/search-data.json"
STOP_WORDS = set(
    "a an and are can do does for from how i in is it ledger me mean means my of on setting settings section sections swarm the this to what when where which why work works gate gates control controls agentihooks".split()
)


def terms(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower())) - STOP_WORDS


def load_index() -> dict:
    with urlopen(INDEX, timeout=20) as response:
        index = json.load(response)
    if not isinstance(index, dict) or not all(
        isinstance(row, dict) and all(isinstance(row.get(key), str) for key in ("doc", "title", "content", "url"))
        for row in index.values()
    ):
        raise ValueError("Published docs index must contain page, heading, content and URL strings")
    return index


def reference(row: dict) -> dict:
    return {"page": row["doc"], "heading": row["title"], "url": urljoin(SITE, row["url"])}


def candidates(index: dict, question: str) -> list[dict]:
    wanted = terms(question)
    ranked = []
    for row in index.values():
        ref = reference(row)
        if not ref["url"].startswith(SITE + "docs/") or row["title"].lower() == "table of contents":
            continue
        heading = wanted & terms(row["title"])
        page = wanted & terms(row["doc"])
        matched = wanted & terms(row["content"] + " " + row["title"] + " " + row["doc"])
        if matched:
            ref.update(matched_terms=sorted(matched), missing_terms=sorted(wanted - matched))
            ranked.append((len(matched), len(heading), len(page), ref))
    ranked.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]["url"]))
    return [item[3] for item in ranked[:3]]


def section(index: dict, url: str) -> dict | None:
    for row in index.values():
        ref = reference(row)
        if ref["url"] == url and url.startswith(SITE + "docs/") and row["content"].strip():
            return {**ref, "text": row["content"].strip()}
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Find or read sections of the published AgentiHooks docs")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--question")
    mode.add_argument("--section")
    args = parser.parse_args(argv)
    try:
        index = load_index()
    except (URLError, OSError, ValueError) as error:
        print(
            json.dumps(
                {
                    "status": "unavailable",
                    "source": INDEX,
                    "error": str(error),
                    "next_step": "Check the published docs site and retry when it is reachable; do not infer an answer.",
                }
            )
        )
        return 2
    result = candidates(index, args.question) if args.question is not None else section(index, args.section)
    if not result:
        print(
            json.dumps(
                {
                    "status": "missing",
                    "source": INDEX,
                    "next_step": "Say the published docs lack this answer and propose a docs follow up; never invent an explanation.",
                }
            )
        )
    else:
        key = "candidates" if args.question is not None else "section"
        print(json.dumps({"status": key, "source": INDEX, key: result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
