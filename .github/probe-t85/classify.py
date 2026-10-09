import json
import sys

from hooks.context.local_test_guard import check_local_tests
from hooks.hook_manager import BlockAction


def verdict(tool_input: dict) -> str:
    try:
        check_local_tests({"tool_name": "Bash", "tool_input": tool_input})
    except BlockAction:
        return "REFUSED"
    return "ALLOWED"


for index, cmd in enumerate(json.load(open(sys.argv[1]))):
    print(
        f"form{index}\tbash.command={verdict({'command': cmd})}\tcodex.cmd={verdict({'cmd': cmd})}\t{json.dumps(cmd)}"
    )
