def _frozen(tool_name, tool_input, cwd):
    found = [(tokens, folders) for tokens, folders in located(str(tool_input.get("command"))) if _freezes(tokens)]
    current = _panic_state(_store())
    if current is None or all(
        # ruleid: panic-spare-ignores-command-target
        _spares(tokens) and _on_fix_branch(_directory(cwd, folders), current.fixes)
        for tokens, folders in found
    ):
        return None
    return FROZEN.format(reason=current.reason)


def _freezes(tokens):
    # ruleid: panic-spare-ignores-command-target
    words = [Path(tokens[0]).name, *tokens[1:]] if tokens else []
    return any(words[: len(prefix)] == list(prefix) for prefix in _FROZEN_COMMANDS)


def _spared(tokens, cwd, fixes):
    words, options = _command(tokens)
    targets = _targets(words)
    paths = [value for option, value in options if option == "-C"]
    if targets is None or len(paths) < len(options):
        return False
    here = _directory(cwd, paths)
    # ok: panic-spare-ignores-command-target
    return _on_fix_branch(here, fixes) and _writes(here, targets)


def _freezes(tokens):
    # ok: panic-spare-ignores-command-target
    words = _command(tokens)[0]
    return any(words[: len(prefix)] == list(prefix) for prefix in _FROZEN_COMMANDS)
