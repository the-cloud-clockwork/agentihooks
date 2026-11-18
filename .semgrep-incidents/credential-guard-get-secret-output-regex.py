import re
import shlex


def leaked_secret_check(low: str) -> bool:
    # ruleid: credential-guard-get-secret-output-regex
    return bool(re.search(r"\bkubectl\b.*\bget\s+secret", low)) and bool(
        # ruleid: credential-guard-get-secret-output-regex
        re.search(r"-o\s*(yaml|json)", low)
    )


# ruleid: credential-guard-get-secret-output-regex
SHORT_OUTPUT = re.compile(r"-o\s+(?:yaml|json)")

# ok: credential-guard-get-secret-output-regex
KUBECTL_WORDS = re.compile(r"\bkubectl\b([^|<>;&]*)")


def read_secret_check(low: str) -> bool:
    for found in KUBECTL_WORDS.finditer(low):
        words = shlex.split(found.group(1))
        # ok: credential-guard-get-secret-output-regex
        if "get" in words and re.search(r"^secrets?$", " ".join(words)):
            return True
    return False
