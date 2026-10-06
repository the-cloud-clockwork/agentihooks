import argparse
import json
import subprocess
import sysconfig
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_ROOT = Path(__file__).parent.parent
MAX_TRACED = 4
GITHUB_TITLE_LIMIT = 256
# `python -m detect_test_pollution` hands its pytest children `-p __main__` instead of its own plugin.
DETECT_TEST_POLLUTION = Path(sysconfig.get_path("scripts")) / "detect-test-pollution"
POLLUTER = "-> the polluting test is: "
NOTES = {
    "-> test failed! (output printed above)": "it fails when run alone",
    "-> expected failure -- but it passed?": "it passes when rerun after the same tests",
}


def run_order(report_log: Path) -> tuple[list[str], list[str]]:
    reports = [json.loads(line) for line in report_log.read_text().splitlines()]
    tests = [report for report in reports if report.get("$report_type") == "TestReport"]
    order = list(dict.fromkeys(report["nodeid"] for report in tests))
    failed = list(dict.fromkeys(report["nodeid"] for report in tests if report["outcome"] == "failed"))
    return order, failed


def find_polluter(victim: str, before: list[str], cwd: Path) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        testids = Path(tmp) / "testids.txt"
        testids.write_text("".join(f"{nodeid}\n" for nodeid in [*before, victim]))
        command = [DETECT_TEST_POLLUTION, "--failing-test", victim, "--testids-file", testids]
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
    verdict = ([line for line in result.stdout.splitlines() if line.startswith("-> ")] or [""])[-1]
    if result.returncode == 0 and verdict.startswith(POLLUTER):
        return {"victim": victim, "polluter": verdict.removeprefix(POLLUTER)}
    return {"victim": victim, "polluter": None, "note": NOTES.get(verdict, "no single earlier test makes it fail")}


def leaking_pairs(report_log: Path, cwd: Path) -> tuple[list[dict], int]:
    order, failed = run_order(report_log)
    traced = failed[:MAX_TRACED]
    with ThreadPoolExecutor(MAX_TRACED) as pool:
        findings = list(pool.map(lambda victim: find_polluter(victim, order[: order.index(victim)], cwd), traced))
    return findings, len(failed) - len(traced)


def summary(findings: list[dict], untraced: int) -> str:
    rows = [
        f"| `{finding['polluter']}` | `{finding['victim']}` |"
        if finding["polluter"]
        else f"| {finding['note']} | `{finding['victim']}` |"
        for finding in findings
    ]
    lines = ["### Leaks between tests", "", "| Earlier test that leaks | Test it breaks |", "| --- | --- |", *rows]
    if untraced:
        lines += ["", f"{untraced} more failing tests were not traced."]
    return "\n".join(lines)


def _gh(args: list[str]) -> str:
    return subprocess.run(["gh", *args], cwd=_ROOT, check=True, capture_output=True, text=True).stdout


def followup(finding: dict, run_url: str) -> tuple[str, str]:
    victim, polluter = finding["victim"], finding["polluter"]
    if polluter:
        title = f"Test leak: {polluter} breaks {victim}"
        cause = f"`{victim}` fails after `{polluter}` ran earlier in the same process."
        reproduce = f"python -m pytest -n 0 {polluter} {victim}"
    else:
        title = f"Test fails in one process: {victim}"
        cause = f"`{victim}` failed and no single earlier test explains it: {finding['note']}."
        reproduce = f"python -m pytest -n 0 {victim}"
    body = f"The whole suite in one process failed.\n\n{cause}\n\nReproduce: `{reproduce}`\n\nRun: {run_url}\n"
    return title[:GITHUB_TITLE_LIMIT], body


def open_followups(findings: list[dict], run_url: str, gh=_gh) -> None:
    listing = gh(["issue", "list", "--state", "open", "--limit", "1000", "--json", "number,title"])
    open_issues = {issue["title"]: issue["number"] for issue in json.loads(listing)}
    for finding in findings:
        title, body = followup(finding, run_url)
        if title in open_issues:
            gh(["issue", "comment", str(open_issues[title]), "--body", body])
        else:
            gh(["issue", "create", "--title", title, "--body", body])


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    pairs = commands.add_parser("pairs", help="name the earlier test that makes each failing test fail")
    pairs.add_argument("report_log", type=Path)
    pairs.add_argument("findings", type=Path)
    followups = commands.add_parser("followup", help="open or update one issue per finding")
    followups.add_argument("findings", type=Path)
    followups.add_argument("run_url")
    args = parser.parse_args(argv)
    if args.command == "pairs":
        findings, untraced = leaking_pairs(args.report_log, Path.cwd())
        args.findings.write_text(json.dumps(findings, indent=2) + "\n")
        print(summary(findings, untraced))
    else:
        open_followups(json.loads(args.findings.read_text()), args.run_url)


if __name__ == "__main__":
    main()
