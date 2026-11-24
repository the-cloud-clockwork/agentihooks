import os
from pathlib import Path

IMAGE_TOOLS = "/usr/local/bin"


def venv_and_system_folders_only(venv: Path) -> str:
    # ruleid: lab-static-step-path-without-image-tools
    return os.pathsep.join((str(venv / "bin"), os.defpath))


def names_the_image_tool_folder(venv: Path) -> str:
    # ok: lab-static-step-path-without-image-tools
    return os.pathsep.join((str(venv / "bin"), IMAGE_TOOLS, os.defpath))
