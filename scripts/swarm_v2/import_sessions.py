"""Backfill historical Claude and Codex sessions from explicitly approved roots into the session catalog."""

import argparse
import json
from pathlib import Path

from hooks.memory.api.ingest import Grant
from hooks.memory.catalog import Catalog
from hooks.memory.importer import ImportLedger, ImportPlan, SourceRoot, import_sessions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.swarm_v2.import_sessions", description=__doc__)
    parser.add_argument("--catalog", required=True, help="session catalog SQLite file")
    parser.add_argument("--imports", required=True, help="import ledger SQLite file")
    parser.add_argument("--corpus", required=True, help="corpus every root is approved for")
    parser.add_argument("--principal", required=True)
    parser.add_argument("--project", action="append", default=[], help="granted project id")
    parser.add_argument("--root", action="append", default=[], help="PROVIDER:INSTALLATION:FOLDER")
    parser.add_argument("--alias", action="append", default=[], help="ABSOLUTE_FOLDER=PROJECT")
    parser.add_argument("--expected", action="append", default=[], help="PROVIDER:INSTALLATION:RELATIVE_PATH")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--retire", metavar="EXPORT", help="write a full export there, then remove the import projections"
    )
    return parser


def _run(args: argparse.Namespace, plan: ImportPlan, catalog: Catalog, imports: ImportLedger) -> tuple[dict, bool]:
    if args.retire:
        report = imports.retire(catalog, args.corpus, Path(args.retire))
        return report, report["state"] == "retired"
    grant = Grant(args.principal, args.corpus, frozenset(args.project))
    report = import_sessions(catalog, imports, grant, plan, dry_run=args.dry_run)
    refused = any(source["outcome"] == "refused" for source in report.get("sources", []))
    return report, report["state"] == "completed" and not refused


def _split(parser: argparse.ArgumentParser, value: str, name: str) -> list[str]:
    parts = value.split(":", 2)
    if len(parts) != 3 or not all(parts):
        parser.error(f"{name} must be PROVIDER:INSTALLATION:PATH")
    return parts


def _aliases(parser: argparse.ArgumentParser, values: list[str]) -> dict[str, str]:
    aliases = {}
    for value in values:
        folder, separator, project = value.partition("=")
        if not (separator and folder and project):
            parser.error("--alias must be FOLDER=PROJECT")
        aliases[folder] = project
    return aliases


def _plan(parser: argparse.ArgumentParser, args: argparse.Namespace) -> ImportPlan:
    expected: dict[tuple[str, str], list[str]] = {}
    roots = [_split(parser, value, "--root") for value in args.root]
    for value in args.expected:
        provider, installation, relative = _split(parser, value, "--expected")
        expected.setdefault((provider, installation), []).append(relative)
    unmatched = set(expected) - {(provider, installation) for provider, installation, _ in roots}
    if unmatched:
        parser.error("--expected names a root that no --root declares")
    return ImportPlan(
        tuple(
            SourceRoot(
                provider, installation, Path(folder), args.corpus, tuple(expected.get((provider, installation), []))
            )
            for provider, installation, folder in roots
        ),
        _aliases(parser, args.alias),
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    plan = _plan(parser, args)
    catalog = Catalog(args.catalog)
    try:
        imports = ImportLedger(args.imports)
        try:
            report, succeeded = _run(args, plan, catalog, imports)
        finally:
            imports.close()
    finally:
        catalog.close()
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if succeeded else 2


if __name__ == "__main__":
    raise SystemExit(main())
