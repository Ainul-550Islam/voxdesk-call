#!/usr/bin/env python3
"""Static contract check: every ``<module>.<function>(...)`` keyword argument used
by the new identity routes must exist in the real signature.

This catches the class of mistake that unit tests catch late and reviewers catch
by eye: a keyword-only parameter renamed in the service layer while the route
still passes the old name. Positional arity is checked too, because ``inspect``
can enforce it without running anything.
"""
from __future__ import annotations

import ast
import importlib
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ROUTE_FILES = [
    "app/api/identity_routes.py",
    "app/api/password_routes.py",
    "app/api/machine_routes.py",
    "app/api/api_key_routes.py",
    "app/api/service_account_routes.py",
    "app/api/domain_routes.py",
    "app/api/sso_routes.py",
    "app/api/scim_routes.py",
]

# Local aliases used in the route modules -> real module.
ALIASES = {
    "identity_mfa": "app.auth.identity.mfa",
    "identity_sessions": "app.auth.identity.sessions",
    "identity_email": "app.auth.identity.email",
    "identity_events": "app.auth.identity.events",
    "identity_tokens": "app.auth.identity.tokens",
    "identity_secrets": "app.auth.identity.secrets",
    "identity_exc": "app.auth.identity.exceptions",
    "identity_service": "app.auth.identity.service",
    "policies": "app.auth.identity.policies",
    "key_service": "app.auth.identity.api_keys",
    "sa_service": "app.auth.identity.service_accounts",
    "domain_service": "app.auth.identity.domains",
    "sso_service": "app.auth.identity.sso.service",
    "scim_service": "app.auth.identity.scim.service",
    "scim_schemas": "app.auth.identity.scim.schemas",
    "schemas": "app.auth.identity.scim.schemas",
    "pw": "app.auth.password",
    "service": "app.auth.service",
    "translate": "app.api.identity_errors",
}

# Modules whose functions are called; resolved lazily and cached.
MODULES: dict[str, object] = {}


def module_for(alias: str):
    if alias not in ALIASES:
        return None
    if alias not in MODULES:
        try:
            MODULES[alias] = importlib.import_module(ALIASES[alias])
        except Exception as exc:  # noqa: BLE001
            print(f"IMPORT FAILURE {ALIASES[alias]}: {type(exc).__name__}: {exc}")
            MODULES[alias] = None
    return MODULES[alias]


def check_file(path: Path) -> list[str]:
    problems: list[str] = []
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        owner = func.value
        if not isinstance(owner, ast.Name):
            continue
        module = module_for(owner.id)
        if module is None:
            continue
        target = getattr(module, func.attr, None)
        if target is None:
            problems.append(f"{path.name}:{node.lineno}: {owner.id}.{func.attr} does not exist")
            continue
        if not callable(target):
            continue
        try:
            sig = inspect.signature(target)
        except (TypeError, ValueError):
            continue
        kwargs = {kw.arg for kw in node.keywords if kw.arg}
        params = sig.parameters
        unknown = sorted(k for k in kwargs if k not in params)
        if unknown:
            problems.append(
                f"{path.name}:{node.lineno}: {owner.id}.{func.attr} has no parameter(s) "
                f"{unknown}; signature is {sig}"
            )
        # Positional arity (ignoring method self-bound functions).
        positional = len(node.args) + len([k for k in node.keywords if k.arg is None])
        required_positional = sum(
            1
            for p in params.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) and p.default is p.empty
        )
        if positional and positional < required_positional and "*" not in str(sig):
            problems.append(
                f"{path.name}:{node.lineno}: {owner.id}.{func.attr} called with {positional} "
                f"positional argument(s) but needs {required_positional}: {sig}"
            )
    return problems


def main() -> int:
    all_problems: list[str] = []
    for rel in ROUTE_FILES:
        path = ROOT / rel
        if not path.exists():
            all_problems.append(f"missing route file: {rel}")
            continue
        all_problems.extend(check_file(path))
    if all_problems:
        print(f"{len(all_problems)} contract problem(s):")
        for p in all_problems:
            print(" -", p)
        return 1
    print(f"OK: keyword contracts hold across {len(ROUTE_FILES)} route modules")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
