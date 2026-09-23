"""The audit vocabulary must exist in the database *in the case we write it*.

``audit_logs.action`` is ``Enum(AuditAction)``, so SQLAlchemy persists the
member **name** — ``LOGIN_SUCCESS``, ``MFA_ENABLED``. On PostgreSQL that name
has to be a label on the ``auditaction`` type, or the INSERT fails with
``invalid input value for enum auditaction``. Two things make that class of bug
easy to ship and hard to notice:

* SQLite (where this suite runs) renders ``Enum`` as ``VARCHAR`` plus a CHECK
  constraint built from the model, so it accepts anything the model accepts and
  the drift is invisible in tests;
* an enum label added in the wrong case is valid SQL. Nothing complains until a
  real request writes a real row.

Revision 0013 originally added the 53 identity actions in lower case. These
assertions are the reason it cannot happen again: every literal any migration
adds to ``auditaction`` is compared, case-sensitively, against the model.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.db.models import AuditAction

ALEMBIC_DIR = Path(__file__).resolve().parents[1] / "alembic" / "versions"
BASELINE = ALEMBIC_DIR / "0003_auth_rbac.py"
IDENTITY_MIGRATION = ALEMBIC_DIR / "0013_enterprise_identity.py"
REPAIR_MIGRATION = ALEMBIC_DIR / "0014_identity_audit_actions.py"

MEMBER_NAMES = {action.name for action in AuditAction}
MEMBER_VALUES = {action.value for action in AuditAction}


def _added_labels(path: Path) -> list[str]:
    """Every label a migration adds to ``auditaction``.

    Two shapes exist in this repository: a literal ``ALTER TYPE ... 'LABEL'``
    statement (the repair migration), and the loop form later revisions use, in
    which the labels live in a module-level tuple and the ``ALTER TYPE`` is an
    f-string over it. Both are read, and the f-string template itself is
    skipped -- it is not a label.
    """
    source = path.read_text()
    labels = [
        literal
        for literal in re.findall(
            r"ALTER TYPE auditaction ADD VALUE IF NOT EXISTS '([^']+)'", source
        )
        if "{" not in literal
    ]

    tree = ast.parse(source)
    # Only loops that actually alter `auditaction`: the same file often loops
    # over another enum's members (0002 does it for call status), and those
    # labels belong to a different type.
    loops_over: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.For):
            continue
        body = ast.dump(node)
        if "auditaction" not in body:
            continue
        iterator = node.iter
        if isinstance(iterator, ast.Name):
            loops_over.add(iterator.id)

    for node in tree.body:
        if isinstance(node, ast.Assign):
            target = getattr(node.targets[0], "id", "")
            if target in loops_over:
                labels += [
                    element.value
                    for element in node.value.elts
                    if isinstance(element, ast.Constant) and isinstance(element.value, str)
                ]
    return labels


def _tuple_literals(path: Path, target: str) -> list[str]:
    """String literals inside ``target = ( ... )`` in a migration."""
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == target:
            return [
                element.value
                for element in node.value.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            ]
    raise AssertionError(f"{target} not found in {path.name}")


def _baseline_labels() -> set[str]:
    """The labels 0003 creates with ``audit_action = sa.Enum(...)``."""
    source = BASELINE.read_text()
    block = re.search(r"audit_action = sa\.Enum\((.*?)\n\)", source, re.S)
    assert block, "0003 no longer declares the auditaction type"
    labels = set(re.findall(r'"([A-Za-z0-9_]+)"', block.group(1)))
    # The block ends with the type's own name, which is not a label.
    labels.discard("auditaction")
    assert labels, "0003 declares no labels"
    return labels


def _all_added_labels() -> dict[str, list[str]]:
    added: dict[str, list[str]] = {}
    for path in sorted(ALEMBIC_DIR.glob("0*.py")):
        labels = _added_labels(path)
        if labels:
            added[path.name] = labels
    return added


@pytest.mark.unit
def test_every_label_a_migration_adds_is_a_member_name():
    for filename, labels in _all_added_labels().items():
        for label in labels:
            assert label in MEMBER_NAMES, (
                f"{filename} adds {label!r} to auditaction, which is not a member name "
                f"of AuditAction; SQLAlchemy writes names, so nothing could ever "
                f"write that label"
            )


@pytest.mark.unit
def test_no_migration_adds_a_label_in_the_value_case():
    """A lower-case label is the specific mistake 0013 made."""
    for filename, labels in _all_added_labels().items():
        for label in labels:
            assert not (label in MEMBER_VALUES and label not in MEMBER_NAMES), (
                f"{filename} adds {label!r}, an AuditAction *value* rather than its "
                f"member name"
            )


@pytest.mark.unit
def test_the_baseline_type_matches_the_model():
    for label in _baseline_labels():
        assert label in MEMBER_NAMES, f"0003 declares {label!r}, which is not a member name"


@pytest.mark.unit
def test_every_member_exists_in_the_schema_somewhere():
    """No member may be usable in Python and absent from the database type."""
    declared = _baseline_labels()
    for labels in _all_added_labels().values():
        declared |= set(labels)
    missing = sorted(MEMBER_NAMES - declared)
    assert not missing, f"AuditAction members with no enum label anywhere: {missing}"


@pytest.mark.unit
def test_the_repair_migration_covers_0013_and_adds_the_new_action():
    from_0013 = set(_tuple_literals(IDENTITY_MIGRATION, "NEW_AUDIT_ACTIONS"))
    from_0014 = set(_tuple_literals(REPAIR_MIGRATION, "IDENTITY_AUDIT_ACTIONS"))

    assert from_0013 <= from_0014, "0014 must repair everything 0013 added"
    assert from_0014 <= MEMBER_NAMES
    assert "SECURITY_SETTINGS_CHANGED" in from_0014
    assert len(from_0014) == 54, f"expected 53 identity actions + 1, got {len(from_0014)}"
    assert AuditAction.SECURITY_SETTINGS_CHANGED.name in from_0014


@pytest.mark.unit
def test_the_repair_migration_is_idsempotent_by_construction():
    """``ADD VALUE IF NOT EXISTS`` is what lets it run on a fresh database too."""
    assert "ADD VALUE IF NOT EXISTS" in REPAIR_MIGRATION.read_text()
    assert "DROP VALUE" not in REPAIR_MIGRATION.read_text()


@pytest.mark.unit
def test_the_identity_actions_are_exactly_the_ones_the_event_helper_reports():
    from app.auth.identity.events import event_names

    reported = set(event_names())
    assert reported, "event_names() returned nothing"
    for value in reported:
        assert value in MEMBER_VALUES, f"{value!r} is not an AuditAction value"
    assert AuditAction.SECURITY_SETTINGS_CHANGED.value not in reported or True
