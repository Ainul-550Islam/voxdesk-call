"""A SCIM filter parser (RFC 7644 §3.4.2.2) that compiles to SQLAlchemy.

Why not ``eval`` a translated string, and why not a library: a filter arrives
from an external system and is executed against a tenant's data, so the grammar
is parsed, the attributes are matched against an allow-list, and the *values*
are bound parameters. Anything else is a SQL injection with extra steps.

Supported, which is what identity providers actually send:

* comparisons — ``eq ne co sw ew pr gt ge lt le``
* logical — ``and or not``, with parentheses
* paths — attribute names, including the sub-attributes we expose
  (``emails.value``, ``name.formatted``, ``meta.lastModified``) and the
  ``userName``/``externalId``/``displayName``/``active``/``id`` attributes.

Anything else raises ``SCIMInvalidValue`` with the offending token, so an
integrator gets a useful error instead of an empty result set.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import ColumnElement, and_, not_, or_

from app.auth.identity.exceptions import SCIMInvalidFilter, SCIMInvalidValue

#: Attributes an IdP may filter on, per resource type. The value on the right is
#: a key into the model-mapping the caller supplies; nothing outside this table
#: can ever be referenced by a filter.
USER_ATTRIBUTES: frozenset[str] = frozenset({
    "id",
    "userName",
    "externalId",
    "displayName",
    "active",
    "name.formatted",
    "emails.value",
    "emails.primary",
    "meta.lastModified",
    "meta.created",
})
GROUP_ATTRIBUTES: frozenset[str] = frozenset({
    "id",
    "displayName",
    "externalId",
    "meta.lastModified",
    "meta.created",
})

_OPERATORS = {
    "eq", "ne", "co", "sw", "ew", "pr", "gt", "ge", "lt", "le",
}

_TOKEN_RE = re.compile(
    r"""
    \s*(?:
        (?P<lparen>\()
      | (?P<rparen>\))
      | (?P<string>"(?:[^"\\]|\\.)*")
      | (?P<number>-?\d+(?:\.\d+)?)
      | (?P<word>[A-Za-z_][A-Za-z0-9_.:\[\]-]*)
      | (?P<op>eq|ne|co|sw|ew|pr|gt|ge|lt|le)\b
    )
    """,
    re.VERBOSE,
)

_TRUE = ("true", "True", "TRUE")
_FALSE = ("false", "False", "FALSE")


def unescape_string(token: str) -> str:
    """RFC 7644 §3.4.2.2 string literals: ``\\"`` and ``\\\\`` are escapes."""
    body = token[1:-1]
    out: list[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        if char == "\\" and index + 1 < len(body):
            out.append(body[index + 1])
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


@dataclass(frozen=True)
class Token:
    kind: str
    value: str


def tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    position = 0
    while position < len(text):
        if text[position].isspace():
            position += 1
            continue
        match = _TOKEN_RE.match(text, position)
        if not match or match.end() == match.start():
            raise SCIMInvalidValue(
                f"Could not parse the filter near: {text[position:position + 20]!r}"
            )
        position = match.end()
        for kind in ("lparen", "rparen", "string"):
            if match.group(kind) is not None:
                tokens.append(Token(kind, match.group(kind)))
                break
        else:
            if match.group("number") is not None:
                tokens.append(Token("number", match.group("number")))
            elif match.group("op") is not None:
                tokens.append(Token("op", match.group("op")))
            else:
                word = match.group("word")
                lowered = word.lower()
                if lowered in ("and", "or", "not"):
                    tokens.append(Token(lowered, lowered))
                elif lowered in _OPERATORS:
                    tokens.append(Token("op", lowered))
                else:
                    tokens.append(Token("word", word))
    return tokens


@dataclass(frozen=True)
class Comparison:
    attribute: str
    operator: str
    value: object = None


@dataclass(frozen=True)
class Logical:
    operator: str  # "and" | "or"
    left: object
    right: object


@dataclass(frozen=True)
class Negation:
    operand: object


class _Parser:
    def __init__(self, tokens: list[Token], allowed: frozenset[str]) -> None:
        self.tokens = tokens
        self.allowed = allowed
        self.index = 0

    def peek(self) -> Token | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def next(self) -> Token | None:
        token = self.peek()
        if token is not None:
            self.index += 1
        return token

    def expect(self, kind: str, value: str | None = None) -> Token:
        token = self.next()
        if token is None or token.kind != kind or (value is not None and token.value != value):
            raise SCIMInvalidValue(
                f"Expected {value or kind} in the filter expression."
            )
        return token

    def parse(self):
        expression = self.parse_or()
        if self.peek() is not None:
            raise SCIMInvalidValue(
                f"Unexpected {self.peek().value!r} in the filter expression."
            )
        return expression

    def parse_or(self):
        node = self.parse_and()
        while self.peek() is not None and self.peek().kind == "or":
            self.next()
            node = Logical("or", node, self.parse_and())
        return node

    def parse_and(self):
        node = self.parse_not()
        while self.peek() is not None and self.peek().kind == "and":
            self.next()
            node = Logical("and", node, self.parse_not())
        return node

    def parse_not(self):
        if self.peek() is not None and self.peek().kind == "not":
            self.next()
            return Negation(self.parse_not())
        return self.parse_primary()

    def parse_primary(self):
        token = self.peek()
        if token is None:
            raise SCIMInvalidFilter("The filter expression is empty.")
        if token.kind == "lparen":
            self.next()
            node = self.parse_or()
            self.expect("rparen")
            return node
        if token.kind == "word":
            self.next()
            attribute = token.value
            if attribute not in self.allowed:
                raise SCIMInvalidFilter(f"Filtering on {attribute!r} is not supported.")
            operator = self.next()
            if operator is None or operator.kind != "op":
                raise SCIMInvalidFilter(f"Expected a comparison operator after {attribute!r}.")
            if operator.value == "pr":
                return Comparison(attribute, "pr")
            value_token = self.next()
            if value_token is None or value_token.kind not in ("string", "number", "word"):
                raise SCIMInvalidFilter(f"Expected a value after {operator.value!r}.")
            return Comparison(attribute, operator.value, self._coerce(value_token))
        raise SCIMInvalidFilter(f"Unexpected token {token.value!r} in the filter expression.")

    @staticmethod
    def _coerce(token: Token):
        if token.kind == "string":
            return unescape_string(token.value)
        if token.kind == "number":
            return float(token.value) if "." in token.value else int(token.value)
        if token.value in _TRUE:
            return True
        if token.value in _FALSE:
            return False
        if token.value.lower() in ("null",):
            return None
        return token.value


def parse(text: str, *, allowed: frozenset[str]):
    """Parse a filter into an expression tree. ``None`` for an empty filter."""
    stripped = (text or "").strip()
    if not stripped:
        return None
    return _Parser(tokenize(stripped), allowed).parse()


def compile_expression(expression, columns: dict) -> ColumnElement:
    """Turn a parsed filter into a SQLAlchemy predicate.

    ``columns`` maps a SCIM attribute name to a model column. Only names present
    in that mapping can appear, and every *value* becomes a bound parameter.
    """
    if expression is None:
        raise SCIMInvalidValue("There is nothing to compile.")

    if isinstance(expression, Comparison):
        column = columns.get(expression.attribute)
        if column is None:
            raise SCIMInvalidValue(
                f"Filtering on {expression.attribute!r} is not supported for this resource."
            )
        return _comparison(column, expression)

    if isinstance(expression, Logical):
        left = compile_expression(expression.left, columns)
        right = compile_expression(expression.right, columns)
        return and_(left, right) if expression.operator == "and" else or_(left, right)

    if isinstance(expression, Negation):
        return not_(compile_expression(expression.operand, columns))

    raise SCIMInvalidValue("The filter could not be compiled.")


def _comparison(column, comparison: Comparison) -> ColumnElement:
    value = comparison.value
    operator = comparison.operator

    if operator == "pr":
        return column.is_not(None)
    if operator == "eq":
        return column.is_(None) if value is None else column == _typed(column, value)
    if operator == "ne":
        return column.is_not(None) if value is None else column != _typed(column, value)
    if operator == "co":
        return column.ilike(f"%{_escape_like(value)}%", escape="\\")
    if operator == "sw":
        return column.ilike(f"{_escape_like(value)}%", escape="\\")
    if operator == "ew":
        return column.ilike(f"%{_escape_like(value)}", escape="\\")
    if operator == "gt":
        return column > _typed(column, value)
    if operator == "ge":
        return column >= _typed(column, value)
    if operator == "lt":
        return column < _typed(column, value)
    if operator == "le":
        return column <= _typed(column, value)
    raise SCIMInvalidValue(f"Unsupported operator: {operator!r}")


def _escape_like(value) -> str:
    """Escape LIKE metacharacters so ``co "%"`` cannot match every row."""
    text = str(value)
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _typed(column, value):
    """Coerce a filter value to the column's type, where that is unambiguous."""
    try:
        python_type = column.type.python_type
    except (NotImplementedError, AttributeError):
        return value
    if python_type is bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes")
    if python_type is int and isinstance(value, (int, float)):
        return int(value)
    return value
