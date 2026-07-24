"""Declarative predicates used by branch nodes.

Routing rules are data, never `eval`:

    {"var": "confidence", "op": "lt", "value": 0.6}
    {"all": [{"var": "category", "op": "eq", "value": "bug"},
             {"var": "known_customer", "op": "truthy"}]}

Keeping them in their own module lets the workflow validator type-check every
predicate at registration time, so a typo in an operator is a startup error
rather than a mid-run `internal_error`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

#: operator -> (arity, implementation). Binary operators receive the declared
#: `value`; unary ones ignore it.
_BINARY: dict[str, Callable[[Any, Any], bool]] = {
    "eq": lambda left, right: left == right,
    "ne": lambda left, right: left != right,
    "in": lambda left, right: left in right,
    "not_in": lambda left, right: left not in right,
    "contains": lambda left, right: right in (left or []),
    "gt": lambda left, right: left is not None and left > right,
    "gte": lambda left, right: left is not None and left >= right,
    "lt": lambda left, right: left is not None and left < right,
    "lte": lambda left, right: left is not None and left <= right,
}

_UNARY: dict[str, Callable[[Any], bool]] = {
    "truthy": bool,
    "falsy": lambda left: not bool(left),
    "is_null": lambda left: left is None,
    "not_null": lambda left: left is not None,
}

OPERATORS: frozenset[str] = frozenset(_BINARY) | frozenset(_UNARY)
COMBINATORS: frozenset[str] = frozenset({"all", "any", "not"})


class PredicateError(ValueError):
    """A predicate is structurally invalid."""


def compare(left: Any, op: str, right: Any) -> bool:
    if op in _UNARY:
        return _UNARY[op](left)
    if op not in _BINARY:
        raise PredicateError(f"unsupported operator '{op}'")
    try:
        return _BINARY[op](left, right)
    except TypeError:
        # Comparing incompatible types (e.g. None > 0.6) means "did not match",
        # not "crash the run".
        return False


def evaluate(predicate: Mapping[str, Any], values: Mapping[str, Any]) -> bool:
    if "all" in predicate:
        return all(evaluate(p, values) for p in predicate["all"])
    if "any" in predicate:
        return any(evaluate(p, values) for p in predicate["any"])
    if "not" in predicate:
        return not evaluate(predicate["not"], values)

    var = predicate.get("var")
    if not isinstance(var, str):
        raise PredicateError(f"predicate needs a 'var' name: {predicate!r}")
    return compare(values.get(var), predicate.get("op", "truthy"), predicate.get("value"))


def validate(predicate: Any, *, path: str = "when") -> None:
    """Structural check used by :meth:`WorkflowDefinition.validate`."""
    if not isinstance(predicate, Mapping):
        raise PredicateError(f"{path}: predicate must be an object, got {type(predicate).__name__}")

    for combinator in ("all", "any"):
        if combinator in predicate:
            branches = predicate[combinator]
            if not isinstance(branches, (list, tuple)) or not branches:
                raise PredicateError(f"{path}.{combinator}: expected a non-empty list")
            for index, child in enumerate(branches):
                validate(child, path=f"{path}.{combinator}[{index}]")
            return

    if "not" in predicate:
        validate(predicate["not"], path=f"{path}.not")
        return

    var = predicate.get("var")
    if not isinstance(var, str) or not var:
        raise PredicateError(f"{path}: missing a 'var' name")

    op = predicate.get("op", "truthy")
    if op not in OPERATORS:
        raise PredicateError(
            f"{path}: unsupported operator '{op}' (known: {sorted(OPERATORS)})"
        )
    if op in _BINARY and "value" not in predicate:
        raise PredicateError(f"{path}: operator '{op}' requires a 'value'")
