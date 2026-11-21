import argparse
import json
import re
from pathlib import Path

from markdown_it import MarkdownIt

from scripts.gates.recon import problem

SECTIONS = (
    "Assumptions",
    "What could break",
    "What will not break",
    "Features that may disconnect",
    "Dependencies",
    "Resources",
    "Why the fix",
    "Why the feature",
    "Notes for the reviewer",
)


def _visible(text: str) -> str:
    return re.sub(r"<!--.*?(?:-->|\Z)", "", text, flags=re.DOTALL)


def _headings(text: str) -> list[tuple[int, str, int, int]]:
    offsets = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    tokens = MarkdownIt("commonmark").parse(text)
    return [
        (int(token.tag[1:]), tokens[index + 1].content.strip().casefold(), offsets[token.map[0]], offsets[token.map[1]])
        for index, token in enumerate(tokens)
        if token.type == "heading_open" and token.map is not None
    ]


def _blocks(text: str, level: int) -> list[tuple[str, int, int, str]]:
    headings = [heading for heading in _headings(text) if heading[0] <= level]
    return [
        (name.casefold(), start, end, text[content:end])
        for (depth, name, start, content), end in zip(headings, [row[2] for row in headings[1:]] + [len(text)])
        if depth == level
    ]


def missing_sections(text: str, level: int = 2) -> list[str]:
    contents = {name: content for name, _, _, content in _blocks(text, level)}
    return [name for name in SECTIONS if not _visible(contents.get(name.casefold(), "")).strip()]


def fill(recon: str, body: str = "") -> str:
    invalid = problem(recon)
    if invalid:
        raise ValueError(f"Invalid recon: {invalid}")
    missing = missing_sections(recon, 3)
    if missing:
        raise ValueError(f"Recon review details missing or empty: {', '.join(missing)}")
    contents = {name: content.strip() for name, _, _, content in _blocks(recon, 3)}
    retained: list[str] = []
    names = {section.casefold() for section in SECTIONS}
    cursor = 0
    for name, start, end, _ in _blocks(body, 2):
        if name in names:
            retained.append(body[cursor:start])
            cursor = end
    retained.append(body[cursor:])
    original = "".join(retained).rstrip()
    rendered = "\n\n".join(f"## {name}\n\n{contents[name.casefold()]}" for name in SECTIONS)
    return f"{original}\n\n{rendered}\n" if original else f"{rendered}\n"


def _body_from_event(path: Path) -> str:
    body: object = json.loads(path.read_text(encoding="utf-8"))["pull_request"]["body"]
    if body is None:
        return ""
    if not isinstance(body, str):
        raise ValueError("pull_request.body must be a string or null")
    return body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check")
    source = check.add_mutually_exclusive_group(required=True)
    source.add_argument("--event", type=Path)
    source.add_argument("--body", type=Path)
    filling = commands.add_parser("fill")
    filling.add_argument("--recon", type=Path, required=True)
    filling.add_argument("--body", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "fill":
            original = args.body.read_text(encoding="utf-8") if args.body.exists() else ""
            rendered = fill(args.recon.read_text(encoding="utf-8"), original)
            args.body.write_text(rendered, encoding="utf-8")
            print(rendered, end="")
            return 0
        body = _body_from_event(args.event) if args.event else args.body.read_text(encoding="utf-8")
        missing = missing_sections(body)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Pull request body error: {error}")
        return 1
    for name in missing:
        print(f"Missing or empty section: {name}")
    if missing:
        return 1
    print("Pull request body sections passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
