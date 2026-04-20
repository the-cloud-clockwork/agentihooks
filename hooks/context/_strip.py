"""Shared command stripping for guard pattern matching.

Removes non-command content (heredocs, quoted strings, echo/printf bodies,
curl data payloads, python -c strings) before applying security regex patterns.
This prevents false positives when commands contain documentation text or
message bodies that happen to match blocked patterns.
"""

import re


def strip_non_command_content(command: str) -> str:
    """Strip all non-command content from a shell command string.

    Returns a version of the command safe for regex pattern matching — all
    data payloads, string arguments, and heredoc bodies are removed so only
    the actual command structure remains.
    """
    check = command

    # 1. Heredocs with any delimiter (<<WORD, <<'WORD', <<-WORD, <<-'WORD')
    check = re.sub(r"<<-?\s*'?(\w+)'?.*?\n\1\b", "", check, flags=re.DOTALL)
    # Fallback: <<'EOF' or <<EOF to end of string (if delimiter not closed)
    check = re.sub(r"<<-?\s*'?\w+'?.*", "", check, flags=re.DOTALL)

    # 2. Git commit messages (-m "..." or -m '...')
    check = re.sub(r'-m\s+"[^"]*"', "-m MSG", check)
    check = re.sub(r"-m\s+'[^']*'", "-m MSG", check)

    # 3. Python -c string bodies
    check = re.sub(r"\bpython[0-9.]*\s+-c\s+'[^']*'", "python -c ''", check)
    check = re.sub(r'\bpython[0-9.]*\s+-c\s+"[^"]*"', 'python -c ""', check)

    # 4. Echo/printf string arguments (double and single quoted)
    check = re.sub(r'\b(echo|printf)\s+(-[neE]+\s+)?"[^"]*"', r"\1 MSG", check)
    check = re.sub(r"\b(echo|printf)\s+(-[neE]+\s+)?'[^']*'", r"\1 MSG", check)

    # 5. Curl data payloads (-d/--data/--data-raw/--data-binary)
    check = re.sub(r"(-d|--data(?:-raw|-binary)?)\s+'[^']*'", r"\1 ''", check)
    check = re.sub(r'(-d|--data(?:-raw|-binary)?)\s+"[^"]*"', r'\1 ""', check)

    # 6. jq/awk program arguments (single-quoted)
    check = re.sub(r"\b(jq|awk|sed)\s+'[^']*'", r"\1 ''", check)

    return check
