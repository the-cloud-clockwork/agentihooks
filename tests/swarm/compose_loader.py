from pathlib import Path

import yaml


class ComposeLoader(yaml.SafeLoader):
    pass


ComposeLoader.add_constructor("!reset", lambda loader, node: "!reset")


def compose_file(path: Path) -> dict:
    return yaml.load(path.read_text(), Loader=ComposeLoader)
