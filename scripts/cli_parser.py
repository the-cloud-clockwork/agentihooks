import argparse
from collections.abc import Iterable
from difflib import get_close_matches
from typing import NoReturn


class ArgumentParser(argparse.ArgumentParser):
    def add_subparsers(self, **kwargs) -> argparse._SubParsersAction:
        kwargs.setdefault("title", "commands")
        kwargs.setdefault("metavar", "COMMAND")
        return super().add_subparsers(**kwargs)

    def _known_options(self) -> set[str]:
        choices = set(self._option_string_actions)
        for action in self._actions:
            if isinstance(action, argparse._SubParsersAction):
                for parser in action.choices.values():
                    choices.update(parser._known_options())
        return choices

    def _parse_optional(self, arg_string: str):
        result = super()._parse_optional(arg_string)
        if result is not None:
            option = result[0] if isinstance(result, list) else result
            choices = self._known_options()
            name = arg_string.split("=")[0]
            if option[0] is None and not any(flag.startswith(name) for flag in choices):
                self.unknown("option", name, choices)
        return result

    def _check_value(self, action: argparse.Action, value: str) -> None:
        if action.choices is not None and value not in action.choices and not action.option_strings:
            self.unknown("command", value, action.choices)
        super()._check_value(action, value)

    def unknown(self, kind: str, value: str, choices: Iterable[str]) -> NoReturn:
        message = f"unknown {kind} {value}"
        matches = get_close_matches(value, choices)
        if matches:
            message += f"; did you mean {matches[0]}?"
        self.error(message)

    def error(self, message: str) -> NoReturn:
        if message.startswith("unrecognized arguments: "):
            value = message.removeprefix("unrecognized arguments: ").split()[0]
            kind = "option" if value.startswith("-") else "argument"
            self.unknown(kind, value, self._option_string_actions)
        self.exit(2, f"{message}, run agentihooks -h and try again\n")
