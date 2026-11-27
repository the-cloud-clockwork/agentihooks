import re

# ruleid: heredoc-mask-swallows-delimiter-line
SWALLOWS_LINE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1.*?^\s*\2\s*$", re.S | re.M)


def strip_quoted(check: str) -> str:
    # ruleid: heredoc-mask-swallows-delimiter-line
    return re.sub(r"<<-?\s*'?(\w+)'?.*?\n\1\b", "", check, flags=re.DOTALL)


# ok: heredoc-mask-swallows-delimiter-line
KEEPS_LINE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1([^\n]*)\n(.*?)^\s*\2\s*$", re.S | re.M)

# ok: heredoc-mask-swallows-delimiter-line
OPENER = re.compile(r"(?<!<)<<(?!<)-?\s*(?P<q>['\"]?)(?P<end>\w+)(?P=q)")
