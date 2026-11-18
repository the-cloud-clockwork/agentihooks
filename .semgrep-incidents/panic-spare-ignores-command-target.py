def _frozen(tool_name: str, tool_input: dict[str, Any], cwd: str) -> str | None:
    found = [(tokens, folders) for tokens, folders in located(str(tool_input.get("command"))) if _freezes(tokens)]
    current = _panic_state(_store())
    if current is None or all(
        # ruleid: panic-spare-ignores-command-target
        _spares(tokens) and _on_fix_branch(_directory(cwd, folders), current.fixes)
        for tokens, folders in found
    ):
        return None
    return FROZEN.format(reason=current.reason)


def _freezes(tokens: list[str]) -> bool:
    # ruleid: panic-spare-ignores-command-target
    words = [Path(tokens[0]).name, *tokens[1:]] if tokens else []
    return any(words[: len(prefix)] == list(prefix) for prefix in _FROZEN_COMMANDS)


def _spared(tokens: list[str], cwd: str, fixes: tuple[str, ...]) -> bool:
    words, options = _command(tokens)
    targets = _targets(words)
    if targets is None or any(option != "-C" for option, _ in options):
        return False
    here = _directory(cwd, [value for _, value in options])
    # ok: panic-spare-ignores-command-target
    return _on_fix_branch(here, fixes, targets) and not (words[:2] == ["git", "push"] and _routed(here))


def _spared_without_targets(tokens: list[str], cwd: str, fixes: tuple[str, ...]) -> bool:
    words, options = _command(tokens)
    here = _directory(cwd, [value for _, value in options])
    # ruleid: panic-spare-ignores-command-target
    return _on_fix_branch(here, fixes, ()) and not (words[:2] == ["git", "push"] and _routed(here))


def _freezes(tokens: list[str]) -> bool:
    # ok: panic-spare-ignores-command-target
    words = _command(tokens)[0]
    return any(words[: len(prefix)] == list(prefix) for prefix in _FROZEN_COMMANDS)
