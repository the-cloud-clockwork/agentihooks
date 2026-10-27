import argparse
import json
import re


def check(profile: str, name: str, args: list[str]) -> list[str]:
    if profile != "master" or re.fullmatch(r"master 1 · \S+", name) is None:
        return args
    kept = []
    flags = iter(args)
    for flag in flags:
        if flag == "--route":
            next(flags, None)
        elif flag != "--agentihooks-fallback-bare" and not flag.startswith("--route="):
            kept.append(flag)
    return ["--route", "claude-ai", *kept]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("args", nargs=argparse.REMAINDER)
    parsed = parser.parse_args(argv)
    flags = parsed.args[1:] if parsed.args[:1] == ["--"] else parsed.args
    print(json.dumps(check(parsed.profile, parsed.name, flags)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
