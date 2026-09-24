#!/usr/bin/env python3
"""Credential-read guard — blocks a read whose OUTPUT would be a secret value.

Runs on every target (it was Claude-only when wired through the settings
``hooks`` array, and Claude's ``permissions.deny`` is a Claude key too).

Reading IS the exposure: a value that reaches the transcript is published and
has to be rotated, not deleted. The guard therefore blocks the read rather than
redacting the output. It covers what ``permissions.deny`` provably cannot:
shell sourcing, environment dumps, interpreter reads, container/remote reads,
value-rendering platform commands, and emitting a secret-named variable.

Decisions are operand-aware: a sensitive path blocks only when a reader
actually consumes it — as a positional operand, a ``<`` redirect target, an
option value, a ``git`` revision path, or inside an interpreter one-liner.
Naming the file (``find -name .env``, ``grep '\\.env' src.py``) is not a read.

A recursive search (``grep -r``, ``rg``) over a tree that holds a credential
file would print its lines. Under ``allow_rewrite`` the guard returns the same
command with exclusions injected; otherwise it walks the tree and blocks when a
credential file is present.

``evaluate(payload, ...)`` returns a ``Verdict``; ``decide(payload)`` is the
block-only view used by the standalone launcher and the test table. Both read
``tool_name``/``tool_input``, which hooks.targets.normalizer already fills for
codex and copilot. The module has no intra-package imports so it also runs by
file path as the bundle's second, independent PreToolUse process.
"""

import fnmatch
import json
import os
import re
import shlex
import sys
from typing import NamedTuple

# --- what counts as sensitive -------------------------------------------------

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

CREDENTIAL_FILES = {".netrc", ".npmrc", ".pypirc", ".git-credentials", ".pgpass"}

KIND_DOTENV = "dotenv"
KIND_SHELLRC = "shellrc"
KIND_CREDENTIAL = "credential"
KIND_ENVIRONMENT = "environment"  # a live value, not a file on disk
KIND_RECURSIVE = "recursive"  # a tree that holds one of the above


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
    return None


# One representative basename per rule in sensitive_kind, for glob matching.
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

# --- bash lexing --------------------------------------------------------------

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
GREP_FAMILY = {"grep", "egrep", "fgrep", "rg", "ack"}
PAGERS = {"cat", "head", "tail", "less", "more", "bat", "tac", "nl"}
WRAPPERS = {"docker", "podman", "nerdctl", "kubectl", "ssh"}
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
# Options whose value names what to SKIP; the value is never a read.
EXCLUSION_OPTS = re.compile(r"^--?(?:exclude|exclude-dir|exclude-from|glob|iglob|ignore|ignore-file|g)$")
GIT_VALUE_FLAGS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}

# grep/rg flags that emit counts or filenames rather than matching lines
NON_CONTENT_GREP = re.compile(r"(?:^|\s)-[A-Za-z]*[clLq]")
RECURSIVE_GREP_FLAG = re.compile(r"(?:^|\s)(?:-[A-Za-z]*[rR][A-Za-z]*|--recursive|--dereference-recursive)(?=\s|$)")
NO_IGNORE_FLAG = re.compile(r"(?:^|\s)(?:-[A-Za-z]*u[A-Za-z]*|--no-ignore\S*)(?=\s|$)")

# A sensitive path can hide inside a larger token — python3 -c "open('.env')".
SENSITIVE_FRAGMENT = re.compile(
    r"[\w./~-]*(?:"
    r"\.env(?:\.[\w-]+)*"
    r"|[\w-]+\.env"
    r"|\.bashrc|\.bash_profile|\.bash_login|\.profile"
    r"|\.zshrc|\.zprofile|\.zshenv|\.bash_history|\.zsh_history"
    r"|\.netrc|\.npmrc|\.pypirc|\.git-credentials|\.pgpass"
    r"|\.aws/credentials"
    r")"
)

STRIPPER_STAGE = (
    re.compile(r"^\s*wc(\s|$)"),
    re.compile(r"^\s*grep\s+.*-[A-Za-z]*[clLq]"),
    re.compile(r"^\s*cut\s+.*-f\s*1(\s|$)"),
    re.compile(r"^\s*sed\s+.*s/=\.\*//"),
    re.compile(r"^\s*awk\s+.*-F.*print\s+\$1"),
    re.compile(r"^\s*compgen\b"),
)
NEUTRAL_STAGE = re.compile(r"^\s*(sort|uniq|column|tr)\b")


HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1.*?^\s*\2\s*$", re.S | re.M)
SEGMENT_DELIM = re.compile(r"&&|\|\||;|\n")
STAGE_DELIM = re.compile(r"(?<!\|)\|(?!\|)")
SUBSTITUTION = re.compile(r"\$\(([^()]*)\)|`([^`]*)`|<\(([^()]*)\)")
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


def strip_heredocs(cmd):
    """Drop heredoc bodies — they are data fed to a command, not commands."""
    return HEREDOC.sub("<<HEREDOC", cmd)


def mask_heredocs(cmd):
    """``strip_heredocs`` that can be undone: (masked command, original heredocs)."""
    kept = []

    def _keep(match):
        kept.append(match.group(0))
        return f"<<AHHEREDOC{len(kept) - 1}"

    return HEREDOC.sub(_keep, cmd), kept


def unmask_heredocs(cmd, kept):
    return re.sub(r"<<AHHEREDOC(\d+)", lambda m: kept[int(m.group(1))], cmd)


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


# --- message construction -----------------------------------------------------

PREAMBLE = (
    "A value read here enters the transcript, which is indexed and permanent — "
    "an exposed credential has to be rotated, not deleted."
)

# Shell rc basenames stay OUT of the injected exclusions. The harness parses an
# option value as a file operand (2.1.259), so `--exclude=.bashrc` matches the
# Read(~/.bashrc) deny rule and prompts on every recursive search. Those paths
# are already fail-closed in permissions.deny, and a home-wide recursion is
# blocked outright rather than rewritten.
EXCLUDE_BASENAMES = [".env", ".env.*", "*.env"] + sorted(CREDENTIAL_FILES)
GREP_EXCLUDES = " ".join("--exclude=" + shlex.quote(b) for b in EXCLUDE_BASENAMES)
RG_EXCLUDES = " ".join("-g " + shlex.quote("!" + b) for b in EXCLUDE_BASENAMES)

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
        "Name-only views carry no values:  env | cut -d= -f1  ·  env | wc -l\n"
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


# --- per-tool decisions -------------------------------------------------------


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

    # Walk the pipeline in order: once a stage strips values, everything after it
    # only ever sees key names, so the rest of the pipeline is harmless.
    for stage in stages[1:]:
        if any(p.search(stage) for p in STRIPPER_STAGE):
            return None
        if not NEUTRAL_STAGE.search(stage):
            break
    return block(
        "dumping the whole environment ({})".format(verb),
        KIND_ENVIRONMENT,
    )


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


def renderer_verdict(command):
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
    if re.search(r"\bkubectl\b.*\bget\s+secret", low) and re.search(r"-o\s*(yaml|json)", low):
        return (
            block(
                "printing Secret data (kubectl get secret -o yaml/json)",
                KIND_ENVIRONMENT,
            )
            + "\nkubectl describe secret <name> shows the keys and byte sizes without the values."
        )
    return None


# --- operand-aware reader detection ------------------------------------------


class Context:
    def __init__(self, cwd="", recursive=False, allow_rewrite=False):
        self.cwd = cwd
        self.recursive = recursive
        self.allow_rewrite = allow_rewrite
        self.find_filters = None  # name filters of the last find stage in this pipeline

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
        return ALLOW  # -c/-l/-q emit counts or filenames, not content

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


def segment_verdict(segment, ctx):
    ctx.find_filters = None
    parts = split_outside_quotes(STAGE_DELIM, segment)
    changed = False
    for i in range(0, len(parts), 2):
        if not parts[i].strip():
            continue
        v = stage_verdict(parts[i], ctx)
        if v.block:
            return v
        if v.rewrite:
            parts[i] = v.rewrite
            changed = True
    return Verdict(rewrite="".join(parts)) if changed else ALLOW


def decide_bash(tool_input, ctx=None):
    command = tool_input.get("command") or ""
    if not command:
        return ALLOW
    ctx = ctx or Context()
    command, heredocs = mask_heredocs(command)
    parts = split_outside_quotes(SEGMENT_DELIM, command)
    changed = False
    for i in range(0, len(parts), 2):
        seg = parts[i]
        if not seg.strip():
            continue
        for check in (env_dump_verdict, echo_secret_verdict, renderer_verdict):
            reason = check(seg)
            if reason:
                return Verdict(block=reason)
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


def evaluate(payload, *, recursive=True, allow_rewrite=False):
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


# --- standalone entry point ---------------------------------------------------


def enabled():
    return os.environ.get("CREDENTIAL_GUARD_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}


def main(argv=None):
    if not enabled():
        return 0
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return 0
        reason = decide(payload)
    except Exception as exc:  # NOSONAR — a guard bug must not brick a session, unless a credential is in play
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
