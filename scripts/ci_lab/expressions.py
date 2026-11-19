"""GitHub Actions expressions in the workflow steps the lab runs: a step's if condition and the ${{ }} in its env.

The subset: string, true, false and null literals, context names, !, ==, !=, && and || with GitHub's precedence,
operand results and number conversion, parentheses, and the status functions of a lab run, which is never cancelled
and runs each step after steps that succeeded. Anything else, and a context name the caller did not supply, is
refused, never guessed.
"""

import math
import re
from collections import deque
from collections.abc import Iterable, Mapping

__all__ = ["Value", "evaluate", "holds", "interpolate", "reads"]

Value = str | bool
TOKEN = re.compile(r"'(?:[^']|'')*'|&&|\|\||==|!=|[!()]|[A-Za-z_][\w.-]*(?:\(\))?")
EMBEDDED = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
WRAPPED = re.compile(r"\s*\$\{\{(.*)\}\}\s*", re.DOTALL)
STATUS = {"always()": True, "cancelled()": False, "failure()": False, "success()": True}
LITERALS: dict[str, Value] = {"false": False, "null": "", "true": True}
NAME = re.compile(r"[A-Za-z_][\w.-]*")
NUMBER = re.compile(r"\s*-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][-+]?\d+)?\s*")


def _tokens(expression: str) -> deque[str]:
    """Split an expression into its tokens, refusing the text no token reads."""
    unread = TOKEN.sub(" ", expression).split()
    if unread:
        raise ValueError(f"cannot read {' '.join(unread)!r}")
    return deque(TOKEN.findall(expression))


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
    if not NAME.fullmatch(token):
        raise ValueError(f"expected a value, found {token!r}")
    if token not in context:
        raise ValueError(f"no value for {token!r} in the context")
    return context[token]


def _unary(tokens: deque[str], context: Mapping[str, str]) -> Value:
    """Read a value under any number of ! negations."""
    if tokens and tokens[0] == "!":
        tokens.popleft()
        return not _unary(tokens, context)
    return _primary(tokens, context)


def _number(value: Value) -> float:
    """The number GitHub converts a value to: true is 1, false and empty text 0, text in JSON number form its number,
    and any other text NaN."""
    if isinstance(value, bool):
        return float(value)
    if not value.strip():
        return 0.0
    return float(value) if NUMBER.fullmatch(value) else math.nan


def _equal(left: Value, right: Value) -> bool:
    """Compare as GitHub does: strings ignore case, and values of different types compare as numbers."""
    if isinstance(left, str) and isinstance(right, str):
        return left.casefold() == right.casefold()
    return _number(left) == _number(right)


def _compared(tokens: deque[str], context: Mapping[str, str]) -> Value:
    """Read values joined by == and !=, comparing from the left."""
    value = _unary(tokens, context)
    while tokens and tokens[0] in ("==", "!="):
        equal = tokens.popleft() == "=="
        value = _equal(value, _unary(tokens, context)) == equal
    return value


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
    """Return the value of one expression, reading context names from context and refusing a name it lacks."""
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


def reads(condition: str, texts: Iterable[str]) -> frozenset[str]:
    """Return the context names a step's if condition and the ${{ }} expressions in texts read.

    It reads names without evaluating, so it never raises: a function call outside the subset reads as its name.
    """
    sources = [condition, *(match.group(1) for text in texts for match in EMBEDDED.finditer(text))]
    tokens = (token for source in sources for token in TOKEN.findall(source))
    return frozenset(token for token in tokens if NAME.fullmatch(token) and token not in LITERALS)
