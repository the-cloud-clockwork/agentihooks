#!/usr/bin/env python3
"""Credential-read guard — blocks a read whose OUTPUT would be a secret value.

Runs on every target. Reading IS the exposure: a value that reaches the transcript is
published and has to be rotated, so the guard blocks the read rather than redacting the
output. It covers what ``permissions.deny`` provably cannot: shell sourcing, environment
dumps, interpreter reads, container/remote reads, value-rendering platform commands, and
emitting a secret-named variable.

Decisions are operand-aware: a sensitive path blocks only when a reader consumes it — as a
positional operand, a ``<`` redirect target, an option value, a ``git`` revision path, or
inside an interpreter one-liner. Naming the file (``find -name .env``) is not a read.
A recursive search over a tree holding a credential file gets exclusions injected under
``allow_rewrite``; otherwise the guard blocks. Shell rc names stay out of the exclusions:
the harness reads an option value as a file operand and would trip its deny rule.

``evaluate(payload, ...)`` returns a ``Verdict``; ``decide(payload)`` is the block-only view
used by the standalone launcher. The module has no intra-package imports so it also runs by
file path as the bundle's second, independent PreToolUse process.
"""

import fnmatch
import json
import os
import re
import shlex
import sys
from collections.abc import Mapping
from typing import NamedTuple

SAFE_SUFFIXES = (".example", ".sample", ".template", ".dist", ".tpl")

SHELL_RC = {
    ".bashrc",
    ".bash_profile",
    ".bash_login",
    ".profile",
    ".zshrc",
    ".zprofile",
    ".zshenv",
    ".bash_history",
    ".zsh_history",
}

CREDENTIAL_FILES = {".netrc", ".npmrc", ".pypirc", ".git-credentials", ".pgpass", "launch-grant"}

KIND_DOTENV = "dotenv"
KIND_SHELLRC = "shellrc"
KIND_CREDENTIAL = "credential"
KIND_ENVIRONMENT = "environment"
KIND_RECURSIVE = "recursive"


def sensitive_kind(token):
    """Classify a path-ish token, or return None when it is not sensitive."""
    if not token:
        return None
    token = token.strip().strip("'\"")
    base = token.rstrip("/").split("/")[-1]
    if not base or base.endswith(SAFE_SUFFIXES):
        return None
    if base == ".env" or base.startswith(".env.") or (base.endswith(".env") and len(base) > 4):
        return KIND_DOTENV
    if base in SHELL_RC:
        return KIND_SHELLRC
    if base in CREDENTIAL_FILES:
        return KIND_CREDENTIAL
    if base == "credentials" and ".aws" in token:
        return KIND_CREDENTIAL
    if base == "environ" and token.startswith("/") and PROC_DIR.match("/" + token.lstrip("/")):
        return KIND_ENVIRONMENT
    return None


SENSITIVE_EXEMPLARS = (".env", ".env.local", "stack.env", "credentials") + tuple(SHELL_RC) + tuple(CREDENTIAL_FILES)

GLOB_CHARS = re.compile(r"[*?\[]")


def glob_could_match_sensitive(pattern):
    pattern = pattern.strip().strip("'\"")
    if not GLOB_CHARS.search(pattern):
        return False
    base = pattern.rstrip("/").split("/")[-1]
    return any(fnmatch.fnmatchcase(ex, base) for ex in SENSITIVE_EXEMPLARS)


SECRETISH_NAME = re.compile(
    r"(?i)(PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|APIKEY|CREDENTIAL"
    r"|PRIVATE_?KEY|ACCESS_?KEY|SESSION_?KEY|AUTH)"
)

QUOTING = re.compile(r"[\"'\\]")
PROC_DIR = re.compile(r"/proc\b")
SHELL_WORD_BREAK = re.compile(r"[\s;|&()<>]+")
CD_OPERAND = re.compile(r"\b(?:cd|pushd)(?:\s+-\S*)*\s+(\S+)")
BARE_ENVIRON = re.compile(r"(?<![\w.])environ\b")


READER_VERBS = {
    "cat",
    "head",
    "tail",
    "sed",
    "less",
    "more",
    "bat",
    "strings",
    "xxd",
    "od",
    "nl",
    "tac",
    "awk",
    "grep",
    "egrep",
    "fgrep",
    "rg",
    "ack",
    "jq",
    "yq",
    "cp",
    "tar",
    "dd",
    "base64",
    "gpg",
    "openssl",
    "vi",
    "vim",
    "nano",
    "view",
    "git",
    "dotenv",
    "scp",
    "rsync",
    "sftp",
    "diff",
    "cmp",
}

SOURCERS = {"source", "."}
INTERPRETERS = {"python", "python2", "python3", "node", "ruby", "perl", "php", "sh", "bash", "zsh", "dash"}
SHELLS = frozenset({"sh", "bash", "zsh", "dash"})
AWKS = frozenset({"awk", "gawk", "mawk", "nawk"})
ENVIRON_VALUE = re.compile(r"\bENVIRON\s*\[")
GREP_FAMILY = {"grep", "egrep", "fgrep", "rg", "ack"}
PAGERS = {"cat", "head", "tail", "less", "more", "bat", "tac", "nl"}
WRAPPERS = {"docker", "podman", "nerdctl", "kubectl", "ssh"}
GH_TOKEN_SUBCOMMANDS = {"status", "token"}
GIT_READ_SUBCOMMANDS = {"diff", "show", "grep", "log", "blame", "cat-file", "difftool"}
PATTERN_FLAGS = {"-e", "--regexp"}
FIND_NAME_FLAGS = {
    "-name",
    "-iname",
    "-path",
    "-ipath",
    "-wholename",
    "-iwholename",
    "-regex",
    "-iregex",
    "-lname",
    "-ilname",
}
FIND_EXEC_FLAGS = {"-exec", "-execdir", "-ok", "-okdir"}
EXCLUSION_OPTS = re.compile(r"^--?(?:exclude|exclude-dir|exclude-from|glob|iglob|ignore|ignore-file|g)$")
GIT_VALUE_FLAGS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}
ACCESS_ENV = r"(?:(?:agentihooks\s+access|-m\s+scripts\.access_env)\s+env\b|access_env\.(?:main|values)\s*\()"

NON_CONTENT_GREP = re.compile(r"(?:^|\s)-[A-Za-z]*[clLq]")
RECURSIVE_GREP_FLAG = re.compile(r"(?:^|\s)(?:-[A-Za-z]*[rR][A-Za-z]*|--recursive|--dereference-recursive)(?=\s|$)")
NO_IGNORE_FLAG = re.compile(r"(?:^|\s)(?:-[A-Za-z]*u[A-Za-z]*|--no-ignore\S*)(?=\s|$)")

SENSITIVE_FRAGMENT = re.compile(
    r"[\w./~-]*(?:"
    r"\.env(?:\.[\w-]+)*"
    r"|[\w-]+\.env"
    r"|\.bashrc|\.bash_profile|\.bash_login|\.profile"
    r"|\.zshrc|\.zprofile|\.zshenv|\.bash_history|\.zsh_history"
    r"|\.netrc|\.npmrc|\.pypirc|\.git-credentials|\.pgpass|launch-grant"
    r"|\.aws/credentials"
    r")(?![\w-])"
)

GREP_VERBS = ("grep", "egrep", "fgrep")
GREP_ARG_FLAGS = set("efABCmdD")
NAME_STAGE = (
    re.compile(r"^\s*cut\s+.*-f\s*1(\s|$)"),
    re.compile(r"^\s*sed\s+.*s/=\.\*//"),
    re.compile(r"^\s*awk\s+.*-F.*print\s+\$1"),
)
NEUTRAL_STAGE = re.compile(r"^\s*(sort|uniq|column|tr)\b")


HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1([^\n]*)\n(.*?)^\s*\2\s*$", re.S | re.M)
HEREDOC_TOKEN = re.compile(r"<<AHHEREDOC(\d+)([^\n]*)")
SCRIPT_RUNNERS = SHELLS | SOURCERS | {"eval", "su", "ksh", "mksh", "ash", "fish", "csh", "tcsh"}
LAUNCHERS = {
    "sudo",
    "doas",
    "env",
    "command",
    "exec",
    "time",
    "nohup",
    "nice",
    "timeout",
    "xargs",
    "stdbuf",
    "setsid",
    "busybox",
}
SEGMENT_DELIM = re.compile(r"&&|\|\||;|\n")
STAGE_DELIM = re.compile(r"(?<!\|)\|(?!\|)")
SUBSTITUTION = re.compile(r"\$\(([^()]*)\)|`([^`]*)`|<\(([^()]*)\)")
BODY_SUBSTITUTION = re.compile(r"\\.|(\$\([^()]*\)|`[^`]*`)", re.S)
VERB_PREFIX = re.compile(
    r"^(\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*(?:sudo\s+(?:-\S+\s+)*|command\s+)?(?:\S*/)?(grep|egrep|fgrep|rg))(?=\s)"
)
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def split_outside_quotes(delim, text):
    """Like ``re.split`` with a capturing group, but a delimiter inside quotes is data."""
    parts, start, i, quote = [], 0, 0, None
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "\\":
            i += 1
        else:
            m = delim.match(text, i)
            if m:
                parts.extend((text[start:i], m.group(0)))
                start = i = m.end()
                continue
        i += 1
    parts.append(text[start:])
    return parts


def strip_heredocs(cmd: str) -> str:
    """Drop heredoc bodies — they are data fed to a command, not commands; the delimiter line stays."""
    return HEREDOC.sub(lambda m: "<<HEREDOC" + m.group(3), cmd)


def mask_heredocs(cmd: str) -> tuple[str, list[re.Match[str]]]:
    """``strip_heredocs`` that can be undone: (masked command, original heredocs)."""
    kept: list[re.Match[str]] = []

    def _keep(match: re.Match[str]) -> str:
        kept.append(match)
        return f"<<AHHEREDOC{len(kept) - 1}{match.group(3)}"

    return HEREDOC.sub(_keep, cmd), kept


def unmask_heredocs(cmd: str, kept: list[re.Match[str]]) -> str:
    """The masked command with each heredoc's opener and body put back around its delimiter line."""

    def _restore(token: re.Match[str]) -> str:
        heredoc = kept[int(token.group(1))]
        line_start, line_end = heredoc.span(3)
        return heredoc.string[heredoc.start() : line_start] + token.group(2) + heredoc.string[line_end : heredoc.end()]

    return HEREDOC_TOKEN.sub(_restore, cmd)


def split_commands(cmd):
    return [c for c in re.split(r"&&|\|\||;|\n", cmd) if c.strip()]


def split_stages(command):
    return [s for s in re.split(r"(?<!\|)\|(?!\|)", command) if s.strip()]


def tokens_of(text):
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def verb_index(tokens):
    for i, tok in enumerate(tokens):
        if tok in ("sudo", "command", "env") and len(tokens) > i + 1:
            continue
        if ASSIGNMENT.match(tok) and len(tokens) > i + 1:
            continue
        if tok.startswith("-") and i > 0 and tokens[i - 1] == "sudo":
            continue
        return i
    return len(tokens)


def verb_of(tokens):
    i = verb_index(tokens)
    return tokens[i].split("/")[-1] if i < len(tokens) else ""


PREAMBLE = (
    "A value read here enters the transcript, which is indexed and permanent — "
    "an exposed credential has to be rotated, not deleted."
)

EXCLUDE_BASENAMES = [".env", ".env.*", "*.env"] + sorted(CREDENTIAL_FILES)
GREP_EXCLUDES = " ".join("--exclude=" + shlex.quote(b) for b in EXCLUDE_BASENAMES)
RG_EXCLUDES = " ".join("-g " + shlex.quote("!" + b) for b in EXCLUDE_BASENAMES)

ENV_NAMES_SAFE = (
    "awk 'BEGIN{for(k in ENVIRON) if(k ~ /^[A-Za-z_][A-Za-z0-9_]*$/) print k; else n++; "
    'print "malformed names: " n+0}\''
)

ALTERNATIVE = {
    KIND_DOTENV: (
        "To learn what a project configures, read its .env.example / .env.sample.\n"
        'To test whether one variable is set:  test -n "${VAR:-}" && echo set || echo unset'
    ),
    KIND_SHELLRC: (
        "Shell rc files carry the operator's exported credentials. Ask the operator what "
        "is configured, or check size only:  wc -l ~/.bashrc"
    ),
    KIND_CREDENTIAL: (
        "This file exists to hold credentials. Reference the value through its environment "
        "variable or its secret store; never open the file."
    ),
    KIND_ENVIRONMENT: (
        "A variable name that is not a valid identifier can hold a secret, and a value that "
        "spans several lines prints its later lines as if they were names, even through an "
        "identifier filter. Only two listings pass. Valid names and only a count of malformed "
        "ones:\n  " + ENV_NAMES_SAFE + "\n"
        "A count (wc, or grep -c):  env | wc -l\n"
        'To test one variable:  test -n "${VAR:-}" && echo set || echo unset\n'
        'To use a value, reference the variable ("$VAR") so the shell expands it at '
        "execution instead of printing it."
    ),
    KIND_RECURSIVE: (
        "Exclude credential files from the search:\n  grep " + GREP_EXCLUDES + " …\n  rg " + RG_EXCLUDES + " …"
    ),
}

MAX_KEYS = 200
MAX_BYTES = 64 * 1024
KEY_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def dotenv_keys(path):
    """Key names only. Values never leave this process."""
    try:
        with open(os.path.expanduser(path), "r", encoding="utf-8", errors="replace") as fh:
            blob = fh.read(MAX_BYTES)
    except OSError:
        return []
    names = []
    for line in blob.splitlines():
        m = KEY_LINE.match(line)
        if m and m.group(1) not in names:
            names.append(m.group(1))
            if len(names) >= MAX_KEYS:
                break
    return names


def block(what, kind, path=None):
    lines = ["BLOCKED: {}.".format(what), PREAMBLE, ""]
    if kind == KIND_DOTENV and path:
        keys = dotenv_keys(path)
        if keys:
            lines.append("Keys defined (names only): " + ", ".join(keys))
    lines.append(ALTERNATIVE.get(kind, ALTERNATIVE[KIND_CREDENTIAL]))
    return "\n".join(lines)


class Verdict(NamedTuple):
    block: "str | None" = None
    rewrite: "dict | None" = None
    note: "str | None" = None


ALLOW = Verdict()


def read_block(target, kind):
    return Verdict(block=block("reading {} via shell".format(target), kind, target if kind == KIND_DOTENV else None))


def decide_read(tool_input, ctx=None):
    path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    kind = sensitive_kind(path)
    if kind:
        return Verdict(block=block("reading {}".format(path), kind, path))
    return ALLOW


def decide_grep(tool_input, ctx=None):
    for field in ("path", "glob", "pattern"):
        value = tool_input.get(field) or ""
        kind = sensitive_kind(value)
        if kind:
            return Verdict(
                block=block("searching the contents of {}".format(value), kind, value if field == "path" else None)
            )
    return ALLOW


def counts_only(stage):
    toks = tokens_of(stage)
    verb = verb_of(toks)
    if verb == "wc":
        return not any(t.startswith("--files0-from") for t in toks)
    if verb not in GREP_VERBS:
        return False
    skip = False
    for tok in toks[verb_index(toks) + 1 :]:
        if skip:
            skip = False
            continue
        if tok == "--":
            return False
        if tok.startswith("--"):
            skip = "=" not in tok
            continue
        if not tok.startswith("-"):
            continue
        for i, ch in enumerate(tok[1:]):
            if ch == "c":
                return True
            if ch in GREP_ARG_FLAGS:
                skip = i == len(tok) - 2
                break
    return False


def env_dump_verdict(command):
    stages = split_stages(command)
    if not stages:
        return None
    toks = tokens_of(stages[0])
    if not toks:
        return None
    verb = verb_of(toks)
    args = [t for t in toks[1:] if not t.startswith("-")]
    flags = [t for t in toks[1:] if t.startswith("-")]

    if verb == "printenv" and args:
        for name in args:
            if SECRETISH_NAME.search(name):
                return block("printing the value of {}".format(name), KIND_ENVIRONMENT)
        return None

    dump = (
        (verb == "env" and not args and not any(f.startswith("--") for f in flags))
        or (verb == "printenv" and not args)
        or (verb == "export" and (not toks[1:] or toks[1:] == ["-p"]))
        or (verb == "declare" and "-x" in flags and not args)
        or (verb == "set" and not toks[1:])
    )
    if not dump:
        return None

    for stage in stages[1:]:
        if counts_only(stage):
            return None
        if not any(p.search(stage) for p in NAME_STAGE) and not NEUTRAL_STAGE.search(stage):
            break
    return block(
        "dumping the whole environment ({})".format(verb),
        KIND_ENVIRONMENT,
    )


def proc_environ_verdict(command, cwd):
    flat = QUOTING.sub("", command)
    entered = [op.lstrip("/").split("/")[0] for op in CD_OPERAND.findall(flat)]
    in_proc = bool(PROC_DIR.match(cwd)) or any(fnmatch.fnmatchcase("proc", top) for top in entered)
    words = SHELL_WORD_BREAK.split(flat)
    named = any(path_reads_environ(w) for w in words) or (in_proc and any(leaf_names_environ(w) for w in words))
    if named or ((in_proc or PROC_DIR.search(flat)) and BARE_ENVIRON.search(flat)):
        return block("reading a process environment file under /proc", KIND_ENVIRONMENT)
    return None


def path_reads_environ(word):
    *dirs, leaf = word.split("/")
    if any(fnmatch.fnmatchcase("proc", d) for d in dirs[:-1]):
        return leaf_names_environ(leaf)
    return any("$" in d for d in dirs) and fnmatch.fnmatchcase("environ", leaf)


def leaf_names_environ(leaf):
    return "$" in leaf or "`" in leaf or fnmatch.fnmatchcase("environ", leaf)


def echo_secret_verdict(command):
    if not re.search(r"\b(echo|printf)\b", command):
        return None
    for name in re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", command):
        if SECRETISH_NAME.search(name):
            return block(
                "printing ${} to stdout".format(name),
                KIND_ENVIRONMENT,
            )
    return None


def awk_environ_verdict(command: str) -> str | None:
    """Block reason when an awk stage of the command reads a value out of ENVIRON."""
    for stage in split_outside_quotes(STAGE_DELIM, command)[::2]:
        if verb_of(unwrapped(tokens_of(stage))) in AWKS and ENVIRON_VALUE.search(stage):
            return block("printing environment values (awk ENVIRON[...])", KIND_ENVIRONMENT)
    return None


KUBECTL_DESCRIBE = re.compile(r"\bkubectl\b[^|]*?(?<!\S)describe(?!\S)([^|<>]*)")
DESCRIBE_VALUE_FLAGS = frozenset(
    "-f -k -l -n -s -v --chunk-size --filename --kustomize --selector --as --as-group --as-uid --as-user-extra "
    "--cache-dir --certificate-authority --client-certificate --client-key --cluster --context --kubeconfig --kuberc "
    "--log-backtrace-at --log-dir --log-file --log-file-max-size --log-flush-frequency --namespace --profile "
    "--profile-output --request-timeout --server --stderrthreshold --tls-server-name --user --username --v --vmodule "
    "--password --token".split()
)
DESCRIBE_BOOLEAN_SHORTHANDS = "ar"
UNSHOWN_TYPE = re.compile(r"[$`{}*?\[]")
SECRET_RESOURCES = {"secret", "secrets"}
SECRET_KEY_LISTING = (
    "\nkubectl get secret <name> --template='{{range $k,$v:=.data}}{{$k}} {{end}}' "
    "lists the key names without the values."
)


def describe_words(rest: str) -> list[str]:
    """Words of a kubectl describe argument list as the shell passes them, quotes, escapes and parentheses removed."""
    return re.sub(r"[\"'\\()]", "", rest).split()


def describe_operands(words: list[str]) -> list[str]:
    """Positional words of a kubectl describe; only a value flag written without = consumes the next word."""
    taken = {
        index + 1
        for index, word in enumerate(words)
        if word.startswith("-") and "-" + word[1:].lstrip(DESCRIBE_BOOLEAN_SHORTHANDS) in DESCRIBE_VALUE_FLAGS
    }
    return [word for index, word in enumerate(words) if index not in taken and not word.startswith("-")]


def hidden_type(operands: list[str]) -> bool:
    """True when a describe names no resource type, or leaves one for the shell to expand."""
    if not operands:
        return True
    typed = operands if any("/" in word for word in operands) else operands[:1]
    return any(UNSHOWN_TYPE.search(word.split("/")[0]) for word in typed)


def names_secret(word: str) -> bool:
    """True when a describe word names the Secret type: plain, plural, group qualified, in a list or as TYPE/NAME."""
    return any(part.split("/")[0].split(".")[0] in SECRET_RESOURCES for part in word.split(","))


def describe_may_show_secret(words: list[str]) -> bool:
    """True when a describe names Secrets or no literal resource type, as with a file flag or a shell expansion."""
    return any(names_secret(word) for word in words) or hidden_type(describe_operands(words))


def joined_continuations(low: str) -> str:
    """The lowercased segment with each backslash newline removed, as the shell joins a continued line."""
    return low.replace("\\\n", "")


def describes_secrets(low: str) -> bool:
    """True when any kubectl describe in the lowercased segment, line continuations joined, may print a Secret."""
    return any(
        describe_may_show_secret(describe_words(found.group(1)))
        for found in KUBECTL_DESCRIBE.finditer(joined_continuations(low))
    )


def renderer_verdict(command: str) -> str | None:
    low = command.lower()
    if re.search(r"\bdocker[- ]compose\b.*\bconfig\b", low) and "--no-interpolate" not in low:
        return (
            block(
                "rendering the compose file with interpolated values (docker compose config)",
                KIND_ENVIRONMENT,
            )
            + "\nUse --no-interpolate to see the file without resolved secrets."
        )
    if re.search(r"\bdocker\s+inspect\b", low) and not re.search(r"(--format|\s-f\s)", low):
        return (
            block(
                "dumping container config including its Env array (docker inspect)",
                KIND_ENVIRONMENT,
            )
            + "\nSelect fields instead:  docker inspect --format '{{.State.Status}}' <container>"
        )
    if gets_secret_values(low):
        return (
            block(
                "printing Secret data (kubectl get secret -o yaml/json)",
                KIND_ENVIRONMENT,
            )
            + SECRET_KEY_LISTING
        )
    if describes_secrets(low):
        return (
            block(
                "describing a Secret or a resource the command does not name "
                "(kubectl describe prints the token of a service account token Secret)",
                KIND_ENVIRONMENT,
            )
            + SECRET_KEY_LISTING
        )
    if len(re.findall(ACCESS_ENV, low)) > len(re.findall(rf'\beval\s+"\$\([^()]*{ACCESS_ENV}\s*\)"', low)):
        return (
            block("printing the Cloudflare Access service token (agentihooks access env)", KIND_ENVIRONMENT)
            + '\nEvaluate it instead:  eval "$(agentihooks access env)", then test each variable by name.'
        )
    return None


KUBECTL_WORDS = re.compile(r"\bkubectl\b")
KUBECTL_END = re.compile(r"[|<>;&]")
SHELL_GROUPING = re.compile(r"[()`]")
VALUE_FLAGS = {"--output": "output", "--template": "template", "--raw": "raw"}
SHORT_OUTPUT = re.compile(r"-[arwh]*o=?(.*)")
RAW_SECRET = re.compile(r"/secrets(?:[/?]|$)|\$")
GET_VALUE_FLAGS = {
    "-n",
    "--namespace",
    "-l",
    "--selector",
    "-f",
    "--filename",
    "-k",
    "--kustomize",
    "-o",
    "--output",
    "--template",
    "--raw",
    "--sort-by",
    "--field-selector",
    "--label-columns",
    "--chunk-size",
    "--subresource",
    "--context",
    "--cluster",
    "--user",
    "--kubeconfig",
    "-s",
    "--server",
    "--token",
    "--as",
    "--as-group",
    "--request-timeout",
    "-v",
    "--cache-dir",
    "--client-key",
    "--client-certificate",
    "--certificate-authority",
    "--tls-server-name",
    "--username",
    "--password",
    "--as-uid",
}
VALUELESS_OUTPUTS = {"", "name", "wide"}
GO_TEMPLATE_OUTPUTS = {"go-template", "template"}
KEY_LISTING = re.compile(
    r"\{\{-?\s*range\s+\$(?P<key>\w+)\s*,\s*\$(?P<value>\w+)\s*:=\s*\.data\s*-?\}\}"
    r"(?:[^{}]|\{\{-?\s*(?:\$(?P=key)|\"(?:[^\"\\{}$]|\\.)*\")\s*-?\}\})*"
    r"\{\{-?\s*end\s*-?\}\}"
)


def flag_values(words: list[str]) -> dict[str, list[str]]:
    """Every output, template and raw flag value of kubectl words: a separate word, after = or attached to -o."""
    values: dict[str, list[str]] = {"output": [], "template": [], "raw": []}
    for word, following in zip(words, [*words[1:], ""]):
        flag, equals, attached = word.partition("=")
        short = SHORT_OUTPUT.fullmatch(word)
        if flag in VALUE_FLAGS:
            values[VALUE_FLAGS[flag]].append(attached if equals else following)
        elif short:
            values["output"].append(short.group(1) or following)
    return values


def lists_keys_only(template: str) -> bool:
    """True when a go template ranges over .data with a key and a value variable and prints only the key."""
    found = KEY_LISTING.fullmatch(template)
    return found is not None and found["key"] != found["value"]


def prints_field_values(outputs: list[str], templates: list[str]) -> bool:
    """True when an output or template value can print a field: all but table, name, wide and the key listing."""
    if not outputs and any(templates):
        outputs = ["go-template"]
    shown = [output.partition("=") for output in outputs if output not in VALUELESS_OUTPUTS]
    bodies = [inline for _, _, inline in shown if inline] + templates
    return bool(shown) and (
        any(name not in GO_TEMPLATE_OUTPUTS for name, _, _ in shown)
        or not bodies
        or not all(lists_keys_only(body) for body in bodies)
    )


def get_operands(words: list[str]) -> list[tuple[str, bool]]:
    """Positional words of a kubectl get, each with whether an unlisted flag right before it may take it as a value."""
    operands: list[tuple[str, bool]] = []
    claimable = False
    remaining = iter(words)
    for word in remaining:
        if not word.startswith("-"):
            operands.append((word, claimable))
        elif word in GET_VALUE_FLAGS:
            next(remaining, None)
        claimable = word.startswith("-") and word not in GET_VALUE_FLAGS and "=" not in word
    return operands


def type_operands(operands: list[tuple[str, bool]]) -> list[str]:
    """Operands that may hold the resource type: all in TYPE/NAME form, else each up to the first no flag can take."""
    words = [word for word, _ in operands]
    claimed = [claimable for _, claimable in operands]
    if any("/" in word for word in words) or False not in claimed:
        return words
    return words[: claimed.index(False) + 1]


def types_secret(rest: list[str]) -> bool:
    """True when the words after a kubectl get name no operand, or the Secret type or a shell filled type in one."""
    types = type_operands(get_operands(rest))
    return not types or any(names_secret(word) or UNSHOWN_TYPE.search(word.split("/")[0]) for word in types)


def get_verb(words: list[str]) -> int | None:
    """Index of the first get word that no listed value flag right before it takes as its value."""
    taken = {index + 1 for index, word in enumerate(words) if word in GET_VALUE_FLAGS}
    return next((index for index, word in enumerate(words) if word == "get" and index not in taken), None)


def prints_secret_values(words: list[str]) -> bool:
    """True when kubectl words get a Secret, or a type left to the shell, with an output that can print a data value."""
    verb = get_verb(words)
    if verb is None:
        return False
    values = flag_values(words)
    if any(RAW_SECRET.search(raw) for raw in values["raw"]):
        return True
    return types_secret(words[verb + 1 :]) and prints_field_values(values["output"], values["template"])


def gets_secret_values(low: str) -> bool:
    """True when any kubectl get in the lowercased segment, continued lines joined, may print a Secret value."""
    joined = joined_continuations(low)
    return any(
        prints_secret_values(
            tokens_of(SHELL_GROUPING.sub(" ", split_outside_quotes(KUBECTL_END, joined[found.end() :])[0]))
        )
        for found in KUBECTL_WORDS.finditer(joined)
    )


def prints_gh_token(toks):
    wrapped = verb_of(toks) in WRAPPERS
    for i, tok in enumerate(toks):
        nested = " " in tok and (wrapped or toks[i - 1 : i] == ["-c"])
        if nested and prints_gh_token(tokens_of(tok)):
            return True
        if tok.split("/")[-1] == "gh":
            rest = toks[i + 1 :]
            args = [t for t in rest if not t.startswith("-")]
            leading = 2 if rest and rest[0].startswith("-") else 1
            if any(a == "auth" and b in GH_TOKEN_SUBCOMMANDS for a, b in zip(args[:leading], args[1:])):
                return True
    return False


def gh_token_verdict(command):
    inner = [next(g for g in m.groups() if g is not None) for m in SUBSTITUTION.finditer(command)]
    if any(prints_gh_token(tokens_of(text)) for text in split_stages(command) + inner):
        return (
            block("printing the GitHub token (gh auth status or gh auth token)", KIND_ENVIRONMENT)
            + '\nTo test whether the token is set:  test -n "${GH_TOKEN:-}" && echo set || echo unset'
            + "\nTo test whether it works:  gh api rate_limit --jq .rate.remaining"
        )
    return None


class Context:
    def __init__(self, cwd="", recursive=False, allow_rewrite=False):
        self.cwd = cwd
        self.recursive = recursive
        self.allow_rewrite = allow_rewrite
        self.find_filters = None

    def resolve(self, path):
        path = os.path.expanduser(path.strip().strip("'\""))
        if os.path.isabs(path):
            return path
        return os.path.join(self.cwd, path) if self.cwd else ""

    def track_cd(self, segment):
        toks = tokens_of(segment)
        if toks and verb_of(toks) == "cd":
            args = [t for t in toks[verb_index(toks) + 1 :] if not t.startswith("-")]
            self.cwd = self.resolve(args[0]) if args else os.path.expanduser("~")


def first_sensitive(candidates):
    for tok in candidates:
        kind = sensitive_kind(tok)
        if kind:
            return tok, kind
    return None, None


def redirect_targets(toks):
    out = []
    for i, tok in enumerate(toks):
        if tok in ("<", "0<") and i + 1 < len(toks):
            out.append(toks[i + 1])
        elif re.match(r"^0?<(?![<(])", tok) and len(tok) > 1:
            out.append(tok.lstrip("0<"))
    return out


def operands_of(toks, verb, pattern_first=False):
    """Tokens a reader consumes: positionals, option values, ``-fVALUE``, ``@file``."""
    start = verb_index(toks) + 1
    body = toks[start:]
    if verb == "git":
        body = git_operand_tokens(body)
        if body is None:
            return []
    out = []
    pattern_seen = not pattern_first or any(t in PATTERN_FLAGS for t in body)
    skip_next = False
    for i, tok in enumerate(body):
        if skip_next:
            skip_next = False
            continue
        if tok == "--":
            continue
        if tok in ("<", "0<", ">", ">>", "2>", "2>>", "&>"):
            skip_next = True
            continue
        if tok in PATTERN_FLAGS:
            skip_next = True
            continue
        if EXCLUSION_OPTS.match(tok):
            skip_next = True
            continue
        if tok.startswith("--"):
            name, eq, value = tok.partition("=")
            if eq and not EXCLUSION_OPTS.match(name):
                out.append(value)
            continue
        if tok.startswith("-") and len(tok) > 1:
            if len(tok) > 2 and not tok.startswith("-g") and sensitive_kind(tok[2:]):
                out.append(tok[2:])
            continue
        if tok.startswith("@") and len(tok) > 1:
            out.append(tok[1:])
            continue
        if tok.startswith("!") or re.match(r"^\d*[<>]", tok):
            continue
        if not pattern_seen:
            pattern_seen = True
            continue
        out.append(tok.split(":", 1)[1] if verb == "git" and ":" in tok and not tok.startswith("/") else tok)
    return out


def git_operand_tokens(body):
    i = 0
    while i < len(body):
        tok = body[i]
        if tok in GIT_VALUE_FLAGS:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        if tok not in GIT_READ_SUBCOMMANDS:
            return None
        rest = body[i + 1 :]
        if tok == "grep":
            rest = ["-e"] + rest if not any(t in PATTERN_FLAGS for t in rest) else rest
        return rest
    return None


def inner_command(toks):
    """For container/remote wrappers, the tokens from the first reader-ish verb on."""
    for i, tok in enumerate(toks[1:], 1):
        base = tok.split("/")[-1]
        if base in READER_VERBS or base in SOURCERS or base in INTERPRETERS:
            return toks[i:]
    return None


def unwrapped(toks: list[str]) -> list[str]:
    """The tokens of the command a container or remote wrapper runs, or the tokens themselves."""
    if verb_of(toks) in WRAPPERS:
        return inner_command(toks) or []
    return toks


def shell_scripts(toks: list[str]) -> list[str]:
    """The scripts a shell stage runs with -c, directly or as a wrapper's command; empty for any other stage."""
    toks = unwrapped(toks)
    if verb_of(toks) not in SHELLS:
        return []
    i = verb_index(toks)
    return [
        word
        for flag, word in zip(toks[i + 1 :], toks[i + 2 :])
        if flag.startswith("-") and not flag.startswith("--") and "c" in flag
    ]


def find_filters(toks):
    filters = []
    for i, tok in enumerate(toks):
        if tok in FIND_NAME_FLAGS and i + 1 < len(toks):
            filters.append(toks[i + 1])
    return filters


def find_roots(toks):
    roots = []
    for tok in toks[verb_index(toks) + 1 :]:
        if tok.startswith("-") or tok in ("(", "!", "\\(", "\\)", ")"):
            break
        roots.append(tok)
    return roots or ["."]


def filters_verdict(filters):
    """None = a filter names or could match a credential file; True = safe; False = no filter."""
    if not filters:
        return False
    tok, kind = first_sensitive(filters)
    if kind or any(glob_could_match_sensitive(f) for f in filters):
        return None
    return True


def recursive_verdict(kind_hint, roots, ctx, no_ignore=False):
    """Walk *roots* for credential files; block when one is present."""
    if not ctx.recursive:
        return ALLOW
    for root in roots:
        resolved = ctx.resolve(root)
        if not resolved or not os.path.isdir(resolved):
            continue
        found, capped = tree_has_sensitive(resolved)
        if found:
            return Verdict(block=block("searching a tree that holds {}".format(found), KIND_RECURSIVE))
        if capped and no_ignore:
            return Verdict(
                block=block("searching a tree too large to verify with ignore files disabled", KIND_RECURSIVE)
            )
    return ALLOW


SCAN_PRUNE = {".git", ".hg", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"}
SCAN_MAX_DEPTH = 6
SCAN_MAX_ENTRIES = 20000
HOME_ROOTS = re.compile(r"^(/|/home|/home/[^/]+|/root)/?$")


def tree_has_sensitive(root):
    seen = 0
    base_depth = root.rstrip("/").count("/")
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in SCAN_PRUNE]
        if dirpath.count("/") - base_depth >= SCAN_MAX_DEPTH:
            dirnames[:] = []
        for name in filenames:
            seen += 1
            if sensitive_kind(name) or (name == "credentials" and dirpath.endswith(".aws")):
                return os.path.join(dirpath, name), False
            if seen >= SCAN_MAX_ENTRIES:
                return None, True
    return None, False


def is_home_root(path):
    expanded = os.path.expanduser(path.strip().strip("'\""))
    return path.strip("'\"") in ("~", "$HOME", "${HOME}") or bool(HOME_ROOTS.match(expanded))


def rewrite_stage(stage_text, verb):
    excludes = RG_EXCLUDES if verb == "rg" else GREP_EXCLUDES
    return VERB_PREFIX.sub(lambda m: m.group(1) + " " + excludes, stage_text, count=1)


def stage_verdict(stage_text, ctx):
    for m in SUBSTITUTION.finditer(stage_text):
        inner = next(g for g in m.groups() if g is not None).strip()
        if inner.startswith("<"):
            tok, kind = first_sensitive([inner[1:].strip()])
            if kind:
                return read_block(tok, kind)
        else:
            v = segment_verdict(inner, Context(ctx.cwd, ctx.recursive, False))
            if v.block:
                return v

    toks = tokens_of(stage_text)
    if not toks:
        return ALLOW
    verb = verb_of(toks)

    if verb in WRAPPERS:
        inner = inner_command(toks)
        if inner is None:
            return ALLOW
        toks, verb = inner, inner[0].split("/")[-1]

    tok, kind = first_sensitive(redirect_targets(toks))
    if kind and verb != "wc":
        return read_block(tok, kind)

    if verb in SOURCERS:
        tok, kind = first_sensitive(t for t in toks[verb_index(toks) + 1 :] if not t.startswith("-"))
        return read_block(tok, kind) if kind else ALLOW

    if verb == "find":
        ctx.find_filters = find_filters(toks)
        for i, t in enumerate(toks[:-1]):
            if t in FIND_EXEC_FLAGS and toks[i + 1].split("/")[-1] in READER_VERBS:
                return find_fed_verdict(ctx, find_roots(toks))
        return ALLOW

    if verb == "xargs":
        rest = [t for t in toks[verb_index(toks) + 1 :] if not t.startswith("-")]
        if rest and rest[0].split("/")[-1] in READER_VERBS:
            return find_fed_verdict(ctx, ["."]) if ctx.find_filters is not None else ALLOW
        return ALLOW

    if verb in INTERPRETERS:
        tok, kind = first_sensitive(operands_of(toks, verb))
        if not kind:
            tok, kind = first_sensitive(SENSITIVE_FRAGMENT.findall(stage_text))
        return read_block(tok, kind) if kind else ALLOW

    if verb not in READER_VERBS:
        return ALLOW
    if verb in GREP_FAMILY and NON_CONTENT_GREP.search(stage_text):
        return ALLOW

    operands = operands_of(toks, verb, pattern_first=verb in GREP_FAMILY)
    tok, kind = first_sensitive(operands)
    if kind:
        return read_block(tok, kind)

    if verb in PAGERS:
        for op in operands:
            if glob_could_match_sensitive(op):
                return read_block(op, KIND_DOTENV)
        return ALLOW

    if verb in GREP_FAMILY:
        recursive = verb in ("rg", "ack") or bool(RECURSIVE_GREP_FLAG.search(stage_text))
        if not recursive:
            return ALLOW
        for op in operands:
            if is_home_root(op):
                return Verdict(block=block("searching the whole home directory ({})".format(op), KIND_RECURSIVE))
        targets = [op for op in operands if not os.path.isfile(ctx.resolve(op) or "")] if ctx.cwd else operands
        if operands and not targets:
            return ALLOW
        if ctx.allow_rewrite and verb != "ack":
            return Verdict(
                rewrite=rewrite_stage(stage_text, verb), note="credential files excluded from recursive search"
            )
        return recursive_verdict(
            KIND_RECURSIVE, targets or ["."], ctx, no_ignore=bool(NO_IGNORE_FLAG.search(stage_text))
        )

    return ALLOW


def find_fed_verdict(ctx, roots):
    safe = filters_verdict(ctx.find_filters or [])
    if safe is None:
        tok, kind = first_sensitive(ctx.find_filters)
        return read_block(tok or ctx.find_filters[0], kind or KIND_DOTENV)
    if safe:
        return ALLOW
    return recursive_verdict(KIND_RECURSIVE, roots, ctx)


def script_verdict(stage_text: str, ctx: Context) -> Verdict:
    """The stage's verdict once each -c script a shell in it runs is checked as a command first."""
    for script in shell_scripts(tokens_of(stage_text)):
        v = decide_bash({"command": script}, Context(ctx.cwd, ctx.recursive))
        if v.block:
            return v
    return stage_verdict(stage_text, ctx)


def runs_input_script(stage_text: str) -> bool:
    """Whether the stage, its wrapped or launched command or a substitution in it runs a shell, source or eval."""
    for m in SUBSTITUTION.finditer(stage_text):
        if runs_input_script(next(g for g in m.groups() if g is not None)):
            return True
    toks = tokens_of(stage_text)
    if verb_of(toks) in WRAPPERS:
        toks = inner_command([word for tok in toks for word in tok.split()]) or []
    words = [tok.split("/")[-1] for tok in toks if not ASSIGNMENT.match(tok)]
    if words and words[0] in LAUNCHERS:
        return any(word in SCRIPT_RUNNERS for word in words)
    return verb_of(toks) in SCRIPT_RUNNERS


def heredoc_verdict(segment: str, heredocs: list[re.Match[str]], ctx: Context) -> Verdict:
    """The verdict on each heredoc body of the segment, checked as a command once a stage of it runs a shell."""
    runs_script = any(runs_input_script(stage) for stage in split_outside_quotes(STAGE_DELIM, segment)[::2])
    for token in HEREDOC_TOKEN.finditer(segment):
        index = int(token.group(1))
        if index >= len(heredocs):
            continue
        v = heredoc_body_verdict(heredocs[index], runs_script, ctx)
        if v.block:
            return v
    return ALLOW


def heredoc_body_verdict(heredoc: re.Match[str], runs_script: bool, ctx: Context) -> Verdict:
    """A body a shell runs is judged as a command; an unquoted body's command substitutions are judged as stages."""
    if runs_script:
        return decide_bash({"command": heredoc.group(4)}, Context(ctx.cwd, ctx.recursive))
    if heredoc.group(1):
        return ALLOW
    for m in BODY_SUBSTITUTION.finditer(heredoc.group(4)):
        if m.group(1):
            v = stage_verdict(m.group(1), Context(ctx.cwd, ctx.recursive, False))
            if v.block:
                return v
    return ALLOW


def segment_verdict(segment: str, ctx: Context) -> Verdict:
    ctx.find_filters = None
    parts = split_outside_quotes(STAGE_DELIM, segment)
    changed = False
    for i in range(0, len(parts), 2):
        if not parts[i].strip():
            continue
        v = script_verdict(parts[i], ctx)
        if v.block:
            return v
        if v.rewrite:
            parts[i] = v.rewrite
            changed = True
    return Verdict(rewrite="".join(parts)) if changed else ALLOW


def decide_bash(tool_input: Mapping[str, object], ctx: Context | None = None) -> Verdict:
    command = tool_input.get("command") or ""
    if not command:
        return ALLOW
    ctx = ctx or Context()
    reason = proc_environ_verdict(command, ctx.cwd)
    if reason:
        return Verdict(block=reason)
    command, heredocs = mask_heredocs(command)
    parts = split_outside_quotes(SEGMENT_DELIM, command)
    changed = False
    for i in range(0, len(parts), 2):
        seg = parts[i]
        if not seg.strip():
            continue
        for check in (env_dump_verdict, echo_secret_verdict, awk_environ_verdict, renderer_verdict, gh_token_verdict):
            reason = check(seg)
            if reason:
                return Verdict(block=reason)
        v = heredoc_verdict(seg, heredocs, ctx)
        if not v.block:
            v = segment_verdict(seg, ctx)
        if v.block:
            return v
        if v.rewrite:
            parts[i] = v.rewrite
            changed = True
        ctx.track_cd(seg)
    if changed:
        return Verdict(
            rewrite={"command": unmask_heredocs("".join(parts), heredocs)},
            note="credential files excluded from recursive search",
        )
    return ALLOW


DISPATCH = {
    "Read": decide_read,
    "NotebookRead": decide_read,
    "Grep": decide_grep,
    "Bash": decide_bash,
}


def evaluate(payload: Mapping[str, object], *, recursive: bool = True, allow_rewrite: bool = False) -> Verdict:
    handler = DISPATCH.get(payload.get("tool_name"))
    if handler is None:
        return ALLOW
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return ALLOW
    ctx = Context(cwd=str(payload.get("cwd") or ""), recursive=recursive, allow_rewrite=allow_rewrite)
    return handler(tool_input, ctx)


def decide(payload):
    """Block reason or None. Explicit reads only — no tree walk, no rewrite."""
    return evaluate(payload, recursive=False).block


def near_credential(tool_input):
    return bool(SENSITIVE_FRAGMENT.search(json.dumps(tool_input, default=str)))


def enabled():
    return os.environ.get("CREDENTIAL_GUARD_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}


def main(argv: list[str] | None = None) -> int:
    """Run the guard on a stdin payload; an internal error allows the call unless a credential path is in play."""
    if not enabled():
        return 0
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return 0
        reason = decide(payload)
    except Exception as exc:  # NOSONAR
        if SENSITIVE_FRAGMENT.search(raw):
            print(
                "BLOCKED: credential guard internal error near a credential path — refusing ({}).".format(exc),
                file=sys.stderr,
            )
            return 2
        return 0
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
