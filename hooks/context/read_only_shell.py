"""Which file or repository write a shell command makes outside a read only seat's own folders."""

from __future__ import annotations

import shlex
from collections.abc import Callable, Iterator
from itertools import dropwhile, islice, takewhile
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from hooks.context.credential_guard import HEREDOC, strip_heredocs

SHELLS = frozenset({"bash", "sh", "zsh", "dash"})
WRAPPERS: dict[str, frozenset[str]] = {
    "sudo": frozenset({"-u", "-g"}),
    "env": frozenset({"-u"}),
    "nice": frozenset({"-n"}),
    "timeout": frozenset({"-s", "-k", "--signal", "--kill-after"}),
    "flock": frozenset({"-w", "-E", "--timeout", "--conflict-exit-code"}),
    "stdbuf": frozenset({"-i", "-o", "-e"}),
    "ionice": frozenset({"-c", "-n", "-p"}),
    "command": frozenset(),
    "nohup": frozenset(),
    "exec": frozenset(),
    "time": frozenset(),
    "setsid": frozenset(),
    "builtin": frozenset(),
}
POSITIONAL_WRAPPERS = frozenset({"timeout", "flock"})
ENV_OPTIONS = frozenset({"-C", "--chdir", "-S", "--split-string"})
KEYWORDS = frozenset({"if", "then", "else", "elif", "do", "while", "until", "!", "{"})
FOLDER_CHANGES = frozenset({"cd", "pushd", "popd"})
COPIES = frozenset({"cp", "ln", "install", "rsync"})
WRITERS = frozenset({"mv", "rm", "rmdir", "touch", "chown", "tee", "unlink"})
IN_PLACE = frozenset({"sed", "perl"})
PACKERS = frozenset({"gzip", "gunzip", "bzip2", "bunzip2", "xz", "unxz", "zstd", "unzstd"})
PACKER_STREAMS = frozenset({"--stdout", "--to-stdout", "--list", "--test"})
TARGET_OPTIONS = frozenset({"-t", "--target-directory"})
OUTPUT_OPTIONS = frozenset({"-o", "--output"})
REMOTE_NAMES = frozenset({"-O", "--remote-name", "--remote-name-all"})
WGET_OUTPUTS = frozenset({"-O", "--output-document"})
FIND_WRITES = frozenset({"-fprint", "-fprint0", "-fls", "-fprintf"})
UNZIP_READS = frozenset({"-l", "-t", "-p", "-v", "-Z", "-z"})
EMPTY = ""
NO_LETTERS = ""
NO_LONG_VALUES: frozenset[str] = frozenset()
OPERAND = frozenset({EMPTY})
STREAM = "-"
HERE = "."
CHMOD_FLAGS = frozenset(
    {
        "-R",
        "-c",
        "-f",
        "-v",
        "-h",
        "-H",
        "-P",
        "--recursive",
        "--changes",
        "--silent",
        "--quiet",
        "--verbose",
        "--preserve-root",
        "--no-preserve-root",
        "--no-dereference",
    }
)
CHMOD_FOLLOWS = frozenset({"-L", "--dereference"})
CHMOD_VALUES = frozenset({"--reference"})
TRUNCATE_LETTERS = "rs"
TRUNCATE_VALUES = frozenset({"--reference", "--size"})
MKDIR_LETTERS = "m"
MKDIR_VALUES = frozenset({"--mode"})
TAR_LETTERS = "bCfFgHIKLNTVX"
TAR_VALUES = frozenset(
    {
        "--add-file",
        "--after-date",
        "--blocking-factor",
        "--checkpoint-action",
        "--directory",
        "--exclude",
        "--exclude-from",
        "--exclude-ignore",
        "--exclude-ignore-recursive",
        "--exclude-tag",
        "--exclude-tag-all",
        "--exclude-tag-under",
        "--file",
        "--files-from",
        "--format",
        "--group",
        "--group-map",
        "--hole-detection",
        "--index-file",
        "--info-script",
        "--label",
        "--level",
        "--listed-incremental",
        "--mode",
        "--mtime",
        "--new-volume-script",
        "--newer",
        "--newer-mtime",
        "--no-quote-chars",
        "--owner",
        "--owner-map",
        "--pax-option",
        "--quote-chars",
        "--quoting-style",
        "--record-size",
        "--rmt-command",
        "--rsh-command",
        "--set-mtime-format",
        "--sort",
        "--sparse-version",
        "--starting-file",
        "--strip-components",
        "--suffix",
        "--tape-length",
        "--to-command",
        "--transform",
        "--use-compress-program",
        "--volno-file",
        "--warning",
        "--xattrs-exclude",
        "--xattrs-include",
        "--xform",
    }
)
TAR_ARCHIVES = frozenset(
    {"-c", "-r", "-u", "-A", "--create", "--append", "--update", "--delete", "--concatenate", "--catenate"}
)
TAR_EXTRACTS = frozenset({"-x", "--extract", "--get"})
TAR_STREAMS = frozenset({"-O", "--to-stdout"})
TAR_FOLDERS = frozenset({"-C", "--directory"})
TAR_FILES = frozenset({"-f", "--file"})
TAR_RUNS = frozenset(
    {
        "-F",
        "-g",
        "-I",
        "-P",
        "--absolute-names",
        "--checkpoint-action",
        "--index-file",
        "--info-script",
        "--listed-incremental",
        "--new-volume-script",
        "--one-top-level",
        "--remove-files",
        "--rmt-command",
        "--rsh-command",
        "--set-mtime-command",
        "--to-command",
        "--transform",
        "--use-compress-program",
        "--volno-file",
        "--xform",
    }
)
TAR_LONG_OPTIONS = frozenset(
    (
        "--absolute-names --acls --add-file --after-date --anchored --append --atime-preserve --auto-compress "
        "--backup --block-number --blocking-factor --bzip2 --catenate --check-device --check-links --checkpoint "
        "--checkpoint-action --clamp-mtime --compare --compress --concatenate --confirmation --create "
        "--delay-directory-restore --delete --dereference --diff --directory --exclude --exclude-backups "
        "--exclude-caches --exclude-caches-all --exclude-caches-under --exclude-from --exclude-ignore "
        "--exclude-ignore-recursive --exclude-tag --exclude-tag-all --exclude-tag-under --exclude-vcs "
        "--exclude-vcs-ignores --extract --file --files-from --force-local --format --full-time --get --group "
        "--group-map --gunzip --gzip --hard-dereference --help --hole-detection --ignore-case "
        "--ignore-command-error --ignore-failed-read --ignore-zeros --incremental --index-file --info-script "
        "--interactive --keep-directory-symlink --keep-newer-files --keep-old-files --label --level --list "
        "--listed-incremental --lzip --lzma --lzop --mode --mtime --multi-volume --new-volume-script --newer "
        "--newer-mtime --no-acls --no-anchored --no-auto-compress --no-check-device --no-delay-directory-restore "
        "--no-ignore-case --no-ignore-command-error --no-null --no-overwrite-dir --no-quote-chars --no-recursion "
        "--no-same-owner --no-same-permissions --no-seek --no-selinux --no-unquote --no-verbatim-files-from "
        "--no-wildcards --no-wildcards-match-slash --no-xattrs --null --numeric-owner --occurrence --old-archive "
        "--one-file-system --one-top-level --overwrite --overwrite-dir --owner --owner-map --pax-option "
        "--portability --posix --preserve-order --preserve-permissions --quote-chars --quoting-style "
        "--read-full-records --record-size --recursion --recursive-unlink --remove-files --restrict --rmt-command "
        "--rsh-command --same-order --same-owner --same-permissions --seek --selinux --set-mtime-command "
        "--set-mtime-format --show-defaults --show-omitted-dirs --show-snapshot-field-ranges --show-stored-names "
        "--show-transformed-names --skip-old-files --sort --sparse --sparse-version --starting-file "
        "--strip-components --suffix --tape-length --test-label --to-command --to-stdout --totals --touch "
        "--transform --uncompress --ungzip --unlink-first --unquote --update --usage --use-compress-program --utc "
        "--verbatim-files-from --verbose --verify --version --volno-file --warning --wildcards "
        "--wildcards-match-slash --xattrs --xattrs-exclude --xattrs-include --xform --xz --zstd"
    ).split()
)
UNZIP_LETTERS = "dIOP"
UNZIP_FOLDERS = frozenset({"-d"})
UNZIP_PARENTS = "-:"
PATCH_LETTERS = "BdDFgioprVxYz"
PATCH_VALUES = frozenset(
    {
        "--basename-prefix",
        "--debug",
        "--directory",
        "--fuzz",
        "--get",
        "--ifdef",
        "--input",
        "--output",
        "--prefix",
        "--quoting-style",
        "--read-only",
        "--reject-file",
        "--reject-format",
        "--strip",
        "--suffix",
        "--version-control",
    }
)
PATCH_LONG_OPTIONS = PATCH_VALUES | frozenset(
    (
        "--backup --backup-if-mismatch --batch --binary --context --dry-run --ed --follow-symlinks --force "
        "--forward --help --ignore-whitespace --merge --no-backup-if-mismatch --normal --posix --quiet "
        "--remove-empty-files --reverse --set-time --set-utc --silent --unified --verbose --version"
    ).split()
)
PATCH_FOLDERS = frozenset({"-d", "--directory"})
PATCH_OUTPUTS = frozenset({"-o", "--output", "-r", "--reject-file"})
PATCH_UNSAFE = frozenset({"-B", "-Y", "--prefix", "--basename-prefix", "--follow-symlinks"})
DRY_RUN = "--dry-run"
GIT_READS = frozenset(
    {
        "status",
        "log",
        "diff",
        "show",
        "fetch",
        "rev-parse",
        "rev-list",
        "merge-base",
        "blame",
        "grep",
        "ls-files",
        "ls-tree",
        "ls-remote",
        "cat-file",
        "describe",
        "shortlog",
        "for-each-ref",
        "show-ref",
        "name-rev",
        "range-diff",
        "diff-tree",
        "cherry",
        "version",
        "help",
    }
)
GIT_LISTINGS: dict[str, frozenset[str]] = {
    "branch": frozenset({"--show-current", "-a", "-r", "-v", "-vv", "-l", "--list", "--all", "--remotes"}),
    "tag": frozenset({"-l", "--list"}),
    "remote": frozenset({"-v", "get-url", "show"}),
    "config": frozenset({"--get", "--get-all", "--get-regexp", "--list", "-l"}),
    "stash": frozenset({"list", "show"}),
    "worktree": frozenset({"list"}),
    "reflog": frozenset({"show"}),
    "submodule": frozenset({"status"}),
}
BARE_LISTINGS = frozenset({"branch", "tag", "remote", "reflog", "submodule"})
GIT_VALUES = frozenset({"-C", "--git-dir", "--work-tree", "--namespace"})
GH_WRITES = frozenset({("pr", "checkout"), ("pr", "co"), ("repo", "clone")})
GH_VALUES = frozenset({"-R", "--repo", "--hostname"})
EXECS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
XARGS_VALUES = frozenset({"-I", "-n", "-P", "-d", "-L", "-s", "-E", "-a"})
DEVICES = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"})
SEPARATORS = frozenset(";|()\n")
OPERATOR_CHARS = ";|()\n&<>"
OPERATORS = frozenset(OPERATOR_CHARS)
QUOTES = "'\""
TICKED = "'\"`"
ESCAPES = "'\"\\"
DEPTHS = {"(": 1, ")": -1}
PLACEHOLDER = "{}"
UNKNOWN = "files named only at run time"


class Simple(NamedTuple):
    words: list[str]
    targets: list[str]
    scripts: list[str]
    depth: int
    isolated: bool


def _quote_end(command: str, start: int) -> int:
    """The index just past the quote closing the one at *start*, or past the end of *command* when none does."""
    quote, escaped = command[start], False
    for index in range(start + 1, len(command)):
        if command[index] == quote and not escaped:
            return index + 1
        escaped = quote == '"' and command[index] == "\\" and not escaped
    return len(command) + 1


def _paren_end(command: str, start: int) -> int:
    """The index just past the parenthesis closing the one at *start*, or the end of *command*."""
    depth, resume = 0, start
    for index in range(start, len(command)):
        if index < resume:
            continue
        if command[index] in QUOTES:
            resume = _quote_end(command, index)
            continue
        depth += DEPTHS.get(command[index], 0)
        if depth == 0:
            return index + 1
    return max(len(command), resume)


def _word_end(command: str, start: int) -> int:
    """The index where the unquoted word starting at *start* ends."""
    resume = 0
    for index in range(start, len(command)):
        if index < resume:
            continue
        if command[index] in OPERATORS or command[index].isspace():
            return index
        if command.startswith("$(", index):
            resume = _paren_end(command, index + 1)
        elif command[index] in TICKED:
            resume = _quote_end(command, index)
        else:
            resume = index + (2 if command[index] == "\\" else 1)
    return len(command)


def _tokens(command: str) -> Iterator[tuple[bool, str]]:
    """The unquoted operator runs (True) and the words (False) of *command*, file descriptor numbers dropped."""
    resume = 0
    for index in range(len(command)):
        if index < resume:
            continue
        if command[index] in OPERATORS:
            resume = len(command) - len(command[index + 1 :].lstrip(OPERATOR_CHARS))
            yield True, command[index:resume]
            continue
        if command[index].isspace():
            continue
        resume = _word_end(command, index)
        if not (command[index:resume].isdigit() and command[resume : resume + 1] in ("<", ">")):
            yield False, command[index:resume]


def _operator(run: str) -> tuple[bool, str]:
    """Whether an operator run ends a simple command, and what the redirect at its tail expects next."""
    run = run.replace(">|", ">")
    tail = run[max(run.rfind(char) for char in SEPARATORS) + 1 :]
    redirect = tail.lstrip("&") if "<" in tail or ">" in tail else ""
    ends = len(tail) < len(run) or not redirect
    if ">" in redirect:
        return ends, "dup" if redirect.endswith("&") else "out"
    return ends, "into" if redirect else ""


def _substitutions(word: str) -> list[str]:
    """The commands *word* runs through $(...) or backticks, outside single quotes."""
    found: list[str] = []
    quoted, resume = False, 0
    for index in range(len(word)):
        if index < resume:
            continue
        if word[index] == '"':
            quoted = not quoted
        elif word[index] == "'" and not quoted:
            resume = _quote_end(word, index)
        elif word.startswith("$(", index):
            resume = _paren_end(word, index + 1)
            found.append(word[index + 2 : resume - 1])
        elif word[index] == "`":
            resume = _quote_end(word, index)
            found.append(word[index + 1 : resume - 1])
        elif word[index] == "\\":
            resume = index + 2
    return found


def _heredoc_scripts(command: str) -> list[str]:
    """The commands an unquoted heredoc body runs through substitution."""
    return [
        script for body in HEREDOC.finditer(command) if not body.group(1) for script in _substitutions(body.group(4))
    ]


def _unquoted(word: str) -> str:
    """*word* as the shell passes it on; a tilde the shell leaves unexpanded stays a literal folder name."""
    try:
        parts = shlex.split(word)
    except ValueError:
        return word
    if len(parts) != 1:
        return word
    prefix = word.partition("/")[0]
    literal = prefix[:1] != "~" or any(char in prefix for char in ESCAPES)
    return f"./{parts[0]}" if parts[0][:1] == "~" and literal else parts[0]


def _fd(word: str) -> bool:
    return word == "-" or word.rstrip("-").isdigit()


def _piped(run: str) -> bool:
    return "|" in run.replace("||", "").replace(">|", "")


def _backgrounded(run: str) -> bool:
    bare = run.replace("&&", "")
    return "&" in bare.replace(">&", "").replace("&>", "").replace("<&", "")


def _simple_commands(command: str) -> list[Simple]:
    """Each simple command of *command*: its words, redirect targets, substituted scripts, subshell depth and isolation."""
    commands = [Simple([], [], [], 0, False)]
    pending, depth = "", 0
    for operator, text in _tokens(strip_heredocs(command.replace("\\\n", " "))):
        if operator:
            ends, pending = _operator(text)
            if _piped(text) or _backgrounded(text):
                commands[-1] = commands[-1]._replace(isolated=True)
            depth = max(0, depth + text.count("(") - text.count(")"))
            if ends:
                commands.append(Simple([], [], [], depth, _piped(text)))
            continue
        commands[-1].scripts.extend(_substitutions(text))
        word = _unquoted(text)
        if pending == "out" or (pending == "dup" and not _fd(word)):
            commands[-1].targets.append(word)
        elif not pending:
            commands[-1].words.append(word)
        pending = ""
    return commands


def _assignment(word: str) -> bool:
    name, equals, _ = word.partition("=")
    return bool(equals) and name.isidentifier()


def _prefix(word: str) -> bool:
    return word in KEYWORDS or _assignment(word)


def _name(words: list[str]) -> str:
    return PurePosixPath(*words[:1]).name


def _unwrapped(words: list[str]) -> list[str]:
    """*words* from the program that wrappers, keywords and assignments run; env changing its folder or command stays."""
    rest = list(dropwhile(_prefix, words))
    wrapper = _name(rest)
    if wrapper not in WRAPPERS:
        return rest
    args = iter(rest[1:])
    for flag in args:
        if not flag.startswith("-"):
            command = [flag, *args]
            return _unwrapped(command[1:] if wrapper in POSITIONAL_WRAPPERS else command)
        if wrapper == "env" and flag.partition("=")[0] in ENV_OPTIONS:
            return ["env", flag, *args]
        if flag in WRAPPERS[wrapper]:
            next(args, "")
    return []


def _dynamic(value: str) -> bool:
    """Whether the shell expands *value* at run time: a variable, a substitution or a glob that can match a parent."""
    globbed = any(part[:1] == "." and any(char in part for char in "*?[") for part in value.split("/"))
    return "$" in value or "`" in value or globbed


def _located(value: str, base: Path | None) -> Path | None:
    """The absolute path *value* names from *base*, or None when only run time knows it."""
    if value == UNKNOWN or _dynamic(value):
        return None
    try:
        path = Path(value).expanduser() if value[:1] == "~" else Path(value)
        if path.is_absolute():
            return path.resolve()
        return None if base is None else (base / path).resolve()
    except (ValueError, RuntimeError, OSError):
        return None


def _within(value: str, base: Path | None, roots: list[Path]) -> bool:
    if value in DEVICES:
        return True
    target = _located(value, base)
    return target is not None and any(target.is_relative_to(root) for root in roots)


def _moved(words: list[str], base: Path | None) -> Path | None:
    """The folder a cd, pushd or popd moves to, or None when only run time knows it."""
    targets = [word for word in words[1:] if not word.startswith("-") or word == "-"]
    if words[0] == "popd" or any(target == "-" or target.startswith("+") for target in targets):
        return None
    if not targets:
        return None if words[0] == "pushd" else _located("~", None)
    return _located(targets[0], base)


def _option_values(words: list[str], names: frozenset[str]) -> list[str]:
    """The values *words* gives the options *names*, written as a separate word or after an equals sign."""
    values: list[str] = []
    rest = iter(words[1:])
    for word in rest:
        name, equals, value = word.partition("=")
        if equals and name in names:
            values.append(value)
        elif word in names:
            values.extend(islice(rest, 1))
    return values


def _literal(value: str) -> str:
    """*value* glued to its option, where the shell leaves a leading tilde as a folder name."""
    return f"./{value}" if value[:1] == "~" else value


def _long_option(word: str, valued: frozenset[str], rest: Iterator[str]) -> tuple[str, str]:
    """The name and value of the long option *word*; a valued option without an equals sign takes the next word."""
    name, equals, value = word.partition("=")
    if equals or name not in valued:
        return name, _literal(value)
    return name, next(rest, UNKNOWN)


def _short_options(word: str, letters: str, rest: Iterator[str]) -> list[tuple[str, str]]:
    """Each option of the cluster *word*; the first of *letters* in it takes the rest of the word or the next word."""
    found: list[tuple[str, str]] = []
    for index, letter in enumerate(word[1:], 2):
        if letter in letters:
            glued = word[index:]
            return [*found, (f"-{letter}", _literal(glued) if glued else next(rest, UNKNOWN))]
        found.append((f"-{letter}", EMPTY))
    return found


def _options(words: list[str], letters: str, valued: frozenset[str]) -> list[tuple[str, str]]:
    """Each option after the command name in *words* with its value, and each operand under an empty name, in order."""
    found: list[tuple[str, str]] = []
    rest = iter(words[1:])
    for word in rest:
        if word == "--":
            found.extend((EMPTY, operand) for operand in rest)
        elif word[:2] == "--":
            found.append(_long_option(word, valued, rest))
        elif word[:1] == "-" and word != STREAM:
            found.extend(_short_options(word, letters, rest))
        else:
            found.append((EMPTY, word))
    return found


def _named(options: list[tuple[str, str]], names: frozenset[str]) -> list[str]:
    return [value for name, value in options if name in names]


def _unknown_long(options: list[tuple[str, str]], known: frozenset[str]) -> bool:
    """Whether a long option in *options* is none of *known*: an abbreviation getopt expands, or an option the reader
    does not follow."""
    return any(name[:2] == "--" and name not in known for name, _ in options)


def _joined(folder: str, value: str) -> str:
    """*value* as a path from *folder*, kept whole when it is absolute or starts at the home folder, and unknown when
    either is."""
    if UNKNOWN in (folder, value):
        return UNKNOWN
    if not folder or value.startswith(("/", "~")):
        return value
    return f"{folder}/{value}"


def _chain(options: list[tuple[str, str]], names: frozenset[str]) -> list[str]:
    """Each folder the options *names* move to in turn, joined to the folder before it."""
    folders: list[str] = []
    for value in _named(options, names):
        folders.append(_joined(folders[-1] if folders else EMPTY, value))
    return folders


def _folder(path: str) -> str:
    return path if path == UNKNOWN or path.endswith("/") else f"{path}/"


def _in_place(word: str) -> bool:
    flags = word.partition("=")[0].partition(".")[0]
    return flags == "--in-place" or (flags[:1] == "-" and flags[1:].isalpha() and "i" in flags)


def _script_flag(word: str) -> bool:
    return word in ("--expression", "--file") or (word[:1] == "-" and word[1:].isalpha() and word[-1] in "ef")


def _edited_files(words: list[str]) -> list[str]:
    """The files an in place sed or perl edit names, its script left out."""
    files: list[str] = []
    scripted, rest = False, iter(words[1:])
    for word in rest:
        if _script_flag(word):
            scripted = True
            next(rest, "")
        elif word.startswith(("--expression=", "--file=")):
            scripted = True
        elif not word.startswith("-"):
            files.append(word)
    return files if scripted else files[1:]


def _short_flags(word: str) -> str:
    return word[1:] if word[:1] == "-" and word[1:2] != "-" else ""


def _plain_targets(words: list[str], operands: list[str]) -> list[str]:
    return operands


def _chmod_targets(words: list[str], operands: list[str]) -> list[str]:
    """The files chmod changes: every operand but the first, which is the mode unless a mode option or --reference
    gave it; unknown when it follows links while it recurses."""
    options = _options(words, NO_LETTERS, CHMOD_VALUES)
    if CHMOD_FOLLOWS & {name for name, _ in options}:
        return [UNKNOWN]
    files = _named(options, OPERAND)
    return files[1:] if all(name in CHMOD_FLAGS for name, _ in options if name) else files


def _truncate_targets(words: list[str], operands: list[str]) -> list[str]:
    return _named(_options(words, TRUNCATE_LETTERS, TRUNCATE_VALUES), OPERAND)


def _mkdir_targets(words: list[str], operands: list[str]) -> list[str]:
    return _named(_options(words, MKDIR_LETTERS, MKDIR_VALUES), OPERAND)


def _copy_targets(words: list[str], operands: list[str]) -> list[str]:
    return _option_values(words, TARGET_OPTIONS) or operands[-1:]


def _edit_targets(words: list[str], operands: list[str]) -> list[str]:
    return _edited_files(words) if any(_in_place(word) for word in words[1:]) else []


def _dd_targets(words: list[str], operands: list[str]) -> list[str]:
    return [word[3:] for word in words[1:] if word.startswith("of=")]


def _patch_targets(words: list[str], operands: list[str]) -> list[str]:
    """The folder patch writes the files its diff names into, its -d folder or the current one, and the output, reject
    and patched files named under it."""
    options = _options(words, PATCH_LETTERS, PATCH_VALUES)
    names = {name for name, _ in options}
    if DRY_RUN in names:
        return []
    if names & PATCH_UNSAFE or _unknown_long(options, PATCH_LONG_OPTIONS):
        return [UNKNOWN]
    folders = _chain(options, PATCH_FOLDERS)
    folder = folders[-1] if folders else HERE
    files = [*_named(options, PATCH_OUTPUTS), *_named(options, OPERAND)[:1]]
    return [_folder(folder), *(_joined(folder, name) for name in files if name != STREAM)]


def _sort_targets(words: list[str], operands: list[str]) -> list[str]:
    return _option_values(words, OUTPUT_OPTIONS)


def _uniq_targets(words: list[str], operands: list[str]) -> list[str]:
    return operands[1:2]


def _curl_targets(words: list[str], operands: list[str]) -> list[str]:
    named = [value for value in _option_values(words, OUTPUT_OPTIONS) if value != "-"]
    return named + ([UNKNOWN] if REMOTE_NAMES & set(words) else [])


def _wget_targets(words: list[str], operands: list[str]) -> list[str]:
    named = _option_values(words, WGET_OUTPUTS)
    if named == ["-"] or any(word.endswith("O-") or word == "--spider" for word in words):
        return []
    return named or [UNKNOWN]


def _find_targets(words: list[str], operands: list[str]) -> list[str]:
    return _option_values(words, FIND_WRITES)


def _packer_targets(words: list[str], operands: list[str]) -> list[str]:
    streams = any(word in PACKER_STREAMS or set(_short_flags(word)) & set("clt") for word in words[1:])
    return [] if streams else operands


def _tar_words(words: list[str]) -> list[str]:
    """*words* with an old style first argument spelled out as short options, each followed by its value."""
    if len(words) < 2 or words[1][:1] == "-":
        return words
    values = iter(words[2:])
    spelled: list[str] = []
    for letter in words[1]:
        spelled.append(f"-{letter}")
        spelled.extend(islice(values, 1 if letter in TAR_LETTERS else 0))
    return [*words[:1], *spelled, *values]


def _tar_archives(options: list[tuple[str, str]]) -> list[str]:
    """The archives a tar create, append, update, delete or concatenate writes, each also judged from every -C
    folder."""
    named = _named(options, TAR_FILES)
    archives = [value for value in named if value != STREAM]
    if not named or any(":" in value.partition("/")[0] for value in archives):
        return [UNKNOWN]
    return [*archives, *(_joined(folder, value) for folder in _chain(options, TAR_FOLDERS) for value in archives)]


def _tar_extracted(options: list[tuple[str, str]]) -> list[str]:
    """The folders a tar extract writes into: each -C folder, and the current folder unless a -C precedes every
    member."""
    folders = _chain(options, TAR_FOLDERS)
    leading = takewhile(lambda option: option[0] not in TAR_FOLDERS, options)
    here = [HERE] if not folders or any(not name for name, _ in leading) else []
    return [_folder(path) for path in here + folders]


def _tar_targets(words: list[str], operands: list[str]) -> list[str]:
    options = _options(_tar_words(words), TAR_LETTERS, TAR_VALUES)
    names = {name for name, _ in options}
    if names & TAR_RUNS or _unknown_long(options, TAR_LONG_OPTIONS):
        return [UNKNOWN]
    archives = _tar_archives(options) if names & TAR_ARCHIVES else []
    extracted = [] if names & TAR_STREAMS or not names & TAR_EXTRACTS else _tar_extracted(options)
    return archives + extracted


def _unzip_targets(words: list[str], operands: list[str]) -> list[str]:
    options = _options(words, UNZIP_LETTERS, NO_LONG_VALUES)
    names = {name for name, _ in options}
    if UNZIP_READS & names:
        return []
    if UNZIP_PARENTS in names or _unknown_long(options, NO_LONG_VALUES):
        return [UNKNOWN]
    return [_folder(path) for path in _named(options, UNZIP_FOLDERS) or [HERE]]


TOOLS: dict[str, Callable[[list[str], list[str]], list[str]]] = {
    **dict.fromkeys(WRITERS, _plain_targets),
    **dict.fromkeys(COPIES, _copy_targets),
    **dict.fromkeys(IN_PLACE, _edit_targets),
    **dict.fromkeys(PACKERS, _packer_targets),
    "chmod": _chmod_targets,
    "truncate": _truncate_targets,
    "mkdir": _mkdir_targets,
    "dd": _dd_targets,
    "patch": _patch_targets,
    "sort": _sort_targets,
    "uniq": _uniq_targets,
    "curl": _curl_targets,
    "wget": _wget_targets,
    "find": _find_targets,
    "tar": _tar_targets,
    "unzip": _unzip_targets,
}


def _write_targets(words: list[str]) -> list[str]:
    head = _name(words)
    tool = TOOLS.get(head)
    if tool is None:
        return []
    named = tool(words, [word for word in words[1:] if not word.startswith("-")])
    return [UNKNOWN if PLACEHOLDER in value else value for value in named]


def _head_reason(words: list[str]) -> str:
    head = "".join(words[:1])
    if head == "env":
        return f"runs env {words[1]}, which hides the folder or the command it runs"
    named = any(char in head for char in "$`*?") or ("[" in head and head not in ("[", "[["))
    return f"runs a command named by {head}" if named else ""


def _gh_reason(args: list[str]) -> str:
    plain: list[str] = []
    rest = iter(args)
    for word in rest:
        if word in GH_VALUES:
            next(rest, "")
        elif word.partition("=")[0] not in GH_VALUES:
            plain.append(word)
    pair = (*plain[:2],)
    return f"runs gh {' '.join(pair)}" if pair in GH_WRITES else ""


def _git_read(subcommand: str, args: list[str]) -> bool:
    if subcommand in GIT_READS:
        return True
    listing = GIT_LISTINGS.get(subcommand)
    if listing is None:
        return False
    if not args:
        return subcommand in BARE_LISTINGS
    if subcommand == "branch":
        return all(arg in listing for arg in args)
    return args[0] in listing


def _git_reason(words: list[str]) -> str:
    """Which git or gh write *words* runs, or empty for a git read and every other command."""
    head = _name(words)
    if head == "gh":
        return _gh_reason(words[1:])
    if head != "git":
        return ""
    if any(word == "--output" or word.startswith("--output=") for word in words):
        return "writes a file with git --output"
    rest = iter(words[1:])
    for word in rest:
        if word in ("-c", "--config-env") or word.startswith("--config-env="):
            return "runs git with -c configuration"
        if word in GIT_VALUES:
            next(rest, "")
        elif not word.startswith("-"):
            return "" if _git_read(word, list(rest)) else f"runs git {word}"
    return ""


def _xargs_command(words: list[str]) -> list[str]:
    args = iter(words)
    for word in args:
        if not word.startswith("-"):
            return [word, *args]
        if word in XARGS_VALUES:
            next(args, "")
    return []


def _not_end(word: str) -> bool:
    return word not in (";", "+")


def _script_option(flag: str) -> bool:
    return flag[:1] == "-" and flag[1:].isalpha() and "c" in flag


def _nested(words: list[str]) -> list[str]:
    """The command lines *words* hands on: a shell's -c script, eval's words, find -exec and xargs commands."""
    head = _name(words)
    if head in SHELLS:
        return [script for flag, script in zip(words[1:], words[2:]) if _script_option(flag)]
    if head == "eval":
        return [" ".join(words[1:])]
    if head == "find":
        return [shlex.join(takewhile(_not_end, words[i + 1 :])) for i, word in enumerate(words) if word in EXECS]
    if head == "xargs":
        return [shlex.join([*_xargs_command(words[1:]), PLACEHOLDER])]
    return []


def _first(scripts: list[str], cwd: Path | None, roots: list[Path]) -> str:
    for script in scripts:
        found = write_reason(script, cwd, roots)
        if found:
            return found
    return ""


def _nested_reason(words: list[str], cwd: Path | None, roots: list[Path]) -> str:
    head = _name(words)
    if head == "find" and "-delete" in words:
        return "deletes files with find -delete"
    if head in SHELLS and all(word.startswith("-") for word in words[1:]):
        return f"feeds a script to {head} on its input"
    return _first(_nested(words), cwd, roots)


def _command_reason(simple: Simple, words: list[str], base: Path | None, roots: list[Path]) -> str:
    outside = [value for value in simple.targets + _write_targets(words) if not _within(value, base, roots)]
    return (
        (f"writes {outside[-1]}" if outside else "")
        or _head_reason(words)
        or _git_reason(words)
        or _nested_reason(words, base, roots)
        or _first(simple.scripts, base, roots)
    )


def write_reason(command: str, cwd: Path | None, roots: list[Path]) -> str:
    """What *command* writes outside *roots* or which repository write it runs, as a phrase, or empty."""
    bases: list[Path | None] = [cwd]
    for simple in _simple_commands(command):
        del bases[simple.depth + 1 :]
        bases += [bases[-1]] * (simple.depth + 1 - len(bases))
        words = _unwrapped(simple.words)
        if words[:1] and words[0] in FOLDER_CHANGES and not simple.isolated:
            bases[-1] = _moved(words, bases[-1])
        found = _command_reason(simple, words, bases[-1], roots)
        if found:
            return found
    return _first(_heredoc_scripts(command), cwd, roots)
