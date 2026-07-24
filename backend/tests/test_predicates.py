"""Unit tests for declarative branch predicates and prompt rendering."""

from __future__ import annotations

import pytest

from app.engine.handlers.agent_node import render_template
from app.engine.predicates import PredicateError, evaluate, validate


# --- evaluation ---------------------------------------------------------
@pytest.mark.parametrize(
    ("predicate", "values", "expected"),
    [
        ({"var": "c", "op": "eq", "value": "bug"}, {"c": "bug"}, True),
        ({"var": "c", "op": "eq", "value": "bug"}, {"c": "billing"}, False),
        ({"var": "n", "op": "lt", "value": 0.6}, {"n": 0.4}, True),
        ({"var": "n", "op": "gte", "value": 0.6}, {"n": 0.6}, True),
        ({"var": "c", "op": "in", "value": ["a", "b"]}, {"c": "a"}, True),
        ({"var": "tags", "op": "contains", "value": "vip"}, {"tags": ["vip"]}, True),
        ({"var": "flag", "op": "truthy"}, {"flag": 1}, True),
        ({"var": "flag", "op": "falsy"}, {"flag": 0}, True),
        ({"var": "x", "op": "is_null"}, {}, True),
        ({"var": "x", "op": "not_null"}, {"x": 0}, True),
    ],
)
def test_operators(predicate, values, expected):
    assert evaluate(predicate, values) is expected


def test_missing_variable_does_not_crash_the_run():
    """None compared against a number means 'did not match', not TypeError."""
    assert evaluate({"var": "confidence", "op": "lt", "value": 0.6}, {}) is False
    assert evaluate({"var": "confidence", "op": "gt", "value": 0.6}, {}) is False


def test_combinators():
    values = {"category": "bug", "confidence": 0.9}
    assert evaluate(
        {
            "all": [
                {"var": "category", "op": "eq", "value": "bug"},
                {"var": "confidence", "op": "gte", "value": 0.6},
            ]
        },
        values,
    )
    assert evaluate(
        {
            "any": [
                {"var": "category", "op": "eq", "value": "billing"},
                {"var": "confidence", "op": "gte", "value": 0.6},
            ]
        },
        values,
    )
    assert not evaluate({"not": {"var": "category", "op": "eq", "value": "bug"}}, values)


# --- structural validation ---------------------------------------------
def test_validate_accepts_a_well_formed_predicate():
    validate({"all": [{"var": "a", "op": "eq", "value": 1}, {"var": "b", "op": "truthy"}]})


@pytest.mark.parametrize(
    "bad",
    [
        "not-an-object",
        {"var": "a", "op": "nonsense"},
        {"op": "eq", "value": 1},  # no var
        {"var": "a", "op": "eq"},  # binary op without a value
        {"all": []},  # empty combinator
        {"any": [{"var": "a", "op": "bogus", "value": 1}]},  # bad nested op
    ],
)
def test_validate_rejects_malformed_predicates(bad):
    with pytest.raises(PredicateError):
        validate(bad)


# --- prompt rendering ---------------------------------------------------
def test_placeholders_are_substituted_and_dicts_pretty_printed():
    rendered = render_template(
        "Subject: {subject}\nCustomer:\n{customer}",
        {"subject": "Export fails", "customer": {"plan": "enterprise"}},
    )
    assert "Subject: Export fails" in rendered
    assert '"plan": "enterprise"' in rendered


def test_literal_braces_survive_rendering():
    """A prompt containing a JSON example must not be mangled.

    str.format would raise or corrupt this; the renderer only touches exact
    {identifier} placeholders.
    """
    template = 'Reply like {{"category": "bug"}} for {subject}'
    rendered = render_template(template, {"subject": "X"})
    assert rendered == 'Reply like {{"category": "bug"}} for X'


def test_unknown_placeholder_is_left_alone():
    assert render_template("{present} {absent}", {"present": "yes"}) == "yes {absent}"


def test_none_renders_as_empty_string():
    assert render_template("[{value}]", {"value": None}) == "[]"
