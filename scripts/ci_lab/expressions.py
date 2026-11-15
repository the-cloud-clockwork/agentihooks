"""GitHub Actions expressions in the workflow steps the lab runs: a step's if condition and the ${{ }} in its env.

The subset: string, true, false and null literals, context names, !, ==, !=, && and || with GitHub's precedence and
operand results, parentheses, and the status functions of a lab run, which is never cancelled and runs each step after
steps that succeeded. Anything else is refused, never guessed.
"""

import re
from collections import deque
from collections.abc import Mapping

__all__ = ["Value", "evaluate", "holds", "interpolate"]

Value = str | bool
TOKEN = re.compile(r"\s*('(?:[^']|'')*'|&&|\|\||==|!=|[!()]|[A-Za-z_][\w.-]*(?:\(\))?)")
EMBEDDED = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
WRAPPED = re.compile(r"\s*\$\{\{(.*)\}\}\s*", re.DOTALL)
STATUS = {"always()": True, "cancelled()": False, "failure()": False, "success()": True}
LITERALS: dict[str, Value] = {"false": False, "null": "", "true": True}
OPERATORS = frozenset({"!", "!=", "&&", "(", ")", "==", "||"})


def _tokens(expression: str) -> deque[str]:
    """Split an expression into its tokens, refusing the first text no token reads."""
    tokens: deque[str] = deque()
    position = 0
    while expression[position:].strip():
        match = TOKEN.match(expression, position)
        if match is None:
            raise ValueError(f"cannot read {expression[position:].strip()!r}")
        tokens.append(match.group(1))
        position = match.end()
    return tokens


def _primary(tokens: deque[str], context: Mapping[str, str]) -> Value:
    """Read a parenthesised expression, a literal, a status function or a context name."""
    token = tokens.popleft() if tokens else ""
    if token == "(":
        value = _either(tokens, context)
        closing = tokens.popleft() if tokens else ""
        if closing != ")":
            raise ValueError(f"expected ')', found {closing!r}")
        return value
    if token.startswith("'"):
        return token[1:-1].replace("''", "'")
    if token in STATUS:
        return STATUS[token]
    if token in LITERALS:
        return LITERALS[token]
    if not token or token in OPERATORS or token.endswith("()"):
        raise ValueError(f"expected a value, found {token!r}")
    return context.get(token, "")


def _unary(tokens: deque[str], context: Mapping[str, str]) -> Value:
    """Read a value under any number of ! negations."""
    if tokens and tokens[0] == "!":
        tokens.popleft()
        return not _unary(tokens, context)
    return _primary(tokens, context)


def _equal(left: Value, right: Value) -> bool:
    """Compare as GitHub does for these values: strings ignore case, values of different types differ."""
    if isinstance(left, str) and isinstance(right, str):
        return left.casefold() == right.casefold()
    return left == right


def _compared(tokens: deque[str], context: Mapping[str, str]) -> Value:
    """Read a value, or one == or != comparison of two values."""
    left = _unary(tokens, context)
    if tokens and tokens[0] in ("==", "!="):
        equal = tokens.popleft() == "=="
        return _equal(left, _unary(tokens, context)) == equal
    return left


def _both(tokens: deque[str], context: Mapping[str, str]) -> Value:
    """Read comparisons joined by &&, giving the first falsy operand or the last."""
    value = _compared(tokens, context)
    while tokens and tokens[0] == "&&":
        tokens.popleft()
        right = _compared(tokens, context)
        value = value and right
    return value


def _either(tokens: deque[str], context: Mapping[str, str]) -> Value:
    """Read && groups joined by ||, giving the first truthy operand or the last."""
    value = _both(tokens, context)
    while tokens and tokens[0] == "||":
        tokens.popleft()
        right = _both(tokens, context)
        value = value or right
    return value


def evaluate(expression: str, context: Mapping[str, str]) -> Value:
    """Return the value of one expression, reading context names from context and an absent name as empty."""
    tokens = _tokens(expression)
    value = _either(tokens, context)
    if tokens:
        raise ValueError(f"expected the end, found {tokens[0]!r}")
    return value


def _text(value: Value) -> str:
    """The text GitHub puts in place of a value: true and false in lower case."""
    return str(value).lower() if isinstance(value, bool) else value


def interpolate(text: str, context: Mapping[str, str]) -> str:
    """Return text with each ${{ }} expression replaced by the text of its value."""
    return EMBEDDED.sub(lambda match: _text(evaluate(match.group(1), context)), text)


def holds(condition: str, context: Mapping[str, str]) -> bool:
    """True when a step's if condition holds in a lab run; an empty condition holds."""
    wrapped = WRAPPED.fullmatch(condition)
    expression = condition if wrapped is None else wrapped.group(1)
    return not expression.strip() or bool(evaluate(expression, context))
