"""The stand in task client a compose smoke script embeds for the hive spoke it runs."""

import ast


def methods(script: str) -> dict[str, list[str]]:
    """Each method of the stand in task client in the script's first embedded Python, with its parameter names."""
    source = script.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    client = next(node for node in ast.parse(source).body if isinstance(node, ast.ClassDef) and node.name == "Tasks")
    return {node.name: [arg.arg for arg in node.args.args] for node in client.body if isinstance(node, ast.FunctionDef)}
