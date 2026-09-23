"""The SCIM filter parser, in isolation.

A filter arrives from an external system and is executed against a tenant's
rows, which makes it an input that must be parsed rather than trusted. These
tests are the grammar's rules: which attributes an integrator may name, which
operators exist, how a value is coerced, and — the part that matters most —
that a value is always a bound parameter and never part of the SQL text.

Compiled predicates are asserted as strings here rather than executed, because
the property under test is structural: whatever the filter said, the statement
carries a placeholder.
"""
from __future__ import annotations

import re

import pytest
from sqlalchemy import Boolean, Column, Integer, String, select

from app.auth.identity.exceptions import SCIMInvalidFilter, SCIMInvalidValue
from app.auth.identity.scim import filter as scim_filter
from app.db.models import User, UserRole
from tests.conftest import make_user

pytestmark = pytest.mark.asyncio

COLUMNS = {"userName": User.email, "displayName": User.full_name, "id": User.id}


def _parse(text: str):
    return scim_filter.parse(text, allowed=scim_filter.USER_ATTRIBUTES)


def _sql(text: str) -> str:
    """The compiled SQL for a filter over the columns exposed for users."""
    statement = select(User.id).where(scim_filter.compile_expression(_parse(text), COLUMNS))
    return str(statement.compile(compile_kwargs={"literal_binds": False}))


def test_an_empty_filter_is_no_filter():
    assert scim_filter.parse("", allowed=scim_filter.USER_ATTRIBUTES) is None
    assert scim_filter.parse("   ", allowed=scim_filter.USER_ATTRIBUTES) is None


def test_every_comparison_operator_parses():
    expected = {
        'userName eq "a@b.example"': ("eq", "a@b.example"),
        'userName ne "a@b.example"': ("ne", "a@b.example"),
        'userName co "b.example"': ("co", "b.example"),
        'userName sw "a@"': ("sw", "a@"),
        'userName ew ".example"': ("ew", ".example"),
        "userName pr": ("pr", None),
        'meta.lastModified gt "2026-01-01T00:00:00Z"': ("gt", "2026-01-01T00:00:00Z"),
        'meta.lastModified ge "2026-01-01T00:00:00Z"': ("ge", "2026-01-01T00:00:00Z"),
        'meta.lastModified lt "2026-01-01T00:00:00Z"': ("lt", "2026-01-01T00:00:00Z"),
        'meta.lastModified le "2026-01-01T00:00:00Z"': ("le", "2026-01-01T00:00:00Z"),
    }
    for text, (operator, value) in expected.items():
        parsed = _parse(text)
        assert isinstance(parsed, scim_filter.Comparison), text
        assert parsed.operator == operator, text
        assert parsed.value == value, text


def test_attribute_names_are_matched_against_the_allow_list():
    with pytest.raises(SCIMInvalidFilter):
        _parse('password_hash eq "x"')
    with pytest.raises(SCIMInvalidFilter):
        _parse('User.email eq "x"')
    with pytest.raises(SCIMInvalidFilter):
        _parse('userName or "x"')  # a word with no operator is not a comparison

    # The same grammar is legal for a resource whose allow-list contains it.
    group_view = scim_filter.parse('displayName eq "Okta"', allowed=scim_filter.GROUP_ATTRIBUTES)
    assert isinstance(group_view, scim_filter.Comparison)
    with pytest.raises(SCIMInvalidFilter):
        scim_filter.parse('userName eq "x"', allowed=scim_filter.GROUP_ATTRIBUTES)


def test_logical_grouping_and_negation():
    parsed = _parse('(userName eq "a" or userName eq "b") and not userName pr')
    assert isinstance(parsed, scim_filter.Logical)
    assert parsed.operator == "and"
    assert isinstance(parsed.left, scim_filter.Logical)
    assert parsed.left.operator == "or"
    assert isinstance(parsed.right, scim_filter.Negation)

    # Precedence: `and` binds tighter than `or`.
    parsed = _parse("displayName pr and userName pr or userName pr")
    assert isinstance(parsed, scim_filter.Logical)
    assert parsed.operator == "or"
    assert isinstance(parsed.left, scim_filter.Logical)

    assert _sql('userName eq "a" or displayName pr').count("OR") == 1


def test_a_malformed_filter_is_refused_with_a_message():
    for text in (
        "userName",
        "userName eq",
        'userName eq "unterminated',
        'eq "value"',
        "userName eq 5 = 6",
        '(userName eq "a"',
        'userName eq "a")',
        'userName eq "a" and',
        "userName eq (userName)",
        'userName not eq "a"',
        "userName @eq 1",
    ):
        with pytest.raises((SCIMInvalidValue, SCIMInvalidFilter)) as caught:
            _parse(text)
        assert str(caught.value), f"{text!r} was refused without a reason"


def test_string_values_are_unescaped_per_the_rfc():
    assert scim_filter.unescape_string('"a\\"b"') == 'a"b'
    assert scim_filter.unescape_string('"a\\\\b"') == "a\\b"

    parsed = _parse(r'userName eq "quote\" inside"')
    assert parsed.value == 'quote" inside'


def test_values_are_coerced_to_the_type_of_the_column():
    assert _parse('userName eq "x"').value == "x"
    assert _parse("userName eq 42").value == 42
    assert _parse("userName eq 4.5").value == 4.5
    assert _parse("userName eq true").value is True
    assert _parse("userName eq FALSE").value is False
    assert _parse("userName eq null").value is None

    # A boolean column coerces whatever spelling the IdP sent.
    boolean_column = Column("active", Boolean)
    assert scim_filter._typed(boolean_column, "true") is True
    assert scim_filter._typed(boolean_column, "1") is True
    assert scim_filter._typed(boolean_column, "yes") is True
    assert scim_filter._typed(boolean_column, "no") is False
    assert scim_filter._typed(boolean_column, True) is True

    integer_column = Column("n", Integer)
    assert scim_filter._typed(integer_column, 3.7) == 3
    assert isinstance(scim_filter._typed(integer_column, 3.7), int)

    # A column with no Python type passes the value through untouched.
    assert scim_filter._typed(Column("s", String), "value") == "value"


def test_a_value_is_never_interpolated_into_the_statement():
    """The whole reason this module exists rather than a string translation."""
    hostile = [
        'userName eq "\'; DROP TABLE users; --"',
        'userName eq "%"',
        'userName co "%"',
        'userName sw "_"',
    ]
    # A quoted string cannot be an attribute name, so the classic tautology is
    # refused by the parser before SQL is ever built.
    with pytest.raises(SCIMInvalidFilter):
        _parse('userName eq "a" or "1" eq "1"')
    for text in hostile:
        sql = _sql(text)
        assert "DROP TABLE" not in sql, text
        assert ";" not in sql.split("WHERE", 1)[-1], text
        assert re.search(r":\w+", sql), f"{text} was not parameterised: {sql}"

    # A LIKE wildcard inside a value is escaped when the bound value is built
    # (the execution test below shows it matching nothing rather than
    # everything).
    assert scim_filter._escape_like("%") == "\\%"
    assert scim_filter._escape_like("_") == "\\_"
    assert scim_filter._escape_like("a\\b") == "a\\\\b"


def test_compiling_a_filter_for_a_column_that_was_not_offered_is_refused():
    """The allow-list is enforced twice: at parse time, and again at compile
    time against the mapping the caller actually built."""
    expression = _parse("userName pr")
    with pytest.raises(SCIMInvalidValue, match="not supported"):
        scim_filter.compile_expression(expression, {"displayName": User.full_name})

    with pytest.raises(SCIMInvalidValue):
        scim_filter.compile_expression(None, COLUMNS)


def test_equality_against_null_is_a_null_test():
    assert "IS NULL" in _sql("userName eq null")
    assert "IS NOT NULL" in _sql("userName ne null")


async def test_compiled_filters_actually_select(db, tenant_a):
    """One execution path, so the structural assertions above are not the only
    evidence that the compiled predicate is the predicate that was written."""
    wanted = await make_user(db, tenant_a, UserRole.AGENT, email="wanted@filter.example")
    await make_user(db, tenant_a, UserRole.AGENT, email="other@filter.example")

    expression = scim_filter.parse(
        'userName ew "@filter.example" and userName sw "wanted"',
        allowed=scim_filter.USER_ATTRIBUTES,
    )
    rows = (
        await db.execute(
            select(User.id).where(
                User.tenant_id == tenant_a.id,
                scim_filter.compile_expression(expression, COLUMNS),
            )
        )
    ).scalars().all()
    assert list(rows) == [wanted.id]

    everything = scim_filter.parse("userName pr", allowed=scim_filter.USER_ATTRIBUTES)
    rows = (
        await db.execute(
            select(User.id).where(
                User.tenant_id == tenant_a.id,
                scim_filter.compile_expression(everything, {"userName": User.email}),
            )
        )
    ).scalars().all()
    assert len(rows) == 2

    negation = scim_filter.parse(
        'not (userName sw "wanted")', allowed=scim_filter.USER_ATTRIBUTES
    )
    rows = (
        await db.execute(
            select(User.id).where(
                User.tenant_id == tenant_a.id,
                scim_filter.compile_expression(negation, COLUMNS),
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    assert wanted.id not in rows

    # A wildcard does not turn `co` into a match-everything.
    wildcard = scim_filter.parse('userName co "%"', allowed=scim_filter.USER_ATTRIBUTES)
    rows = (
        await db.execute(
            select(User.id).where(
                User.tenant_id == tenant_a.id,
                scim_filter.compile_expression(wildcard, COLUMNS),
            )
        )
    ).scalars().all()
    assert rows == []
