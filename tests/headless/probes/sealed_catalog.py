import json
import sys

DROPPED = ("tool_mode", "apply_patch_tool_type", "web_search_tool_type", "multi_agent_version")


def main() -> None:
    raw = sys.stdin.read()
    catalog = json.loads(raw[raw.index("{") :])
    for model in catalog["models"]:
        for key in DROPPED:
            model.pop(key, None)
        model["experimental_supported_tools"] = []
        model["supports_search_tool"] = False
    json.dump(catalog, sys.stdout)


if __name__ == "__main__":
    main()
