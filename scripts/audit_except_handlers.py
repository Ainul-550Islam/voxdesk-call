#!/usr/bin/env python3
"""Audit broad `except Exception` handlers for silent swallowing.

A handler is "silent" when it neither logs, nor re-raises, nor returns
anything that carries the error to the caller. Those are the shapes that can
hide a P0 failure behind an apparently-healthy response.

Usage:  python3 audit_except_handlers.py <root>              # human report
        python3 audit_except_handlers.py <root> --json
Exit code is always 0: this is an audit, not a gate.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

BROAD = ("Exception", "BaseException")
LOG_ATTRS = ("log", "logger", "_log", "logging", "LOG")
# Any call whose name/attribute looks like observability or error plumbing.
EFFECT_MARKERS = (
    "log", "logger", "record", "report", "metric", "observe", "alert", "emit",
    "print", "warn", "audit", "fail", "error", "exception", "counter", "trace",
    "save", "persist", "commit", "flush", "send", "publish", "enqueue", "retry",
    "rollback", "mark", "set_", "update", "raise_for",
)
# Builtins that convert/wrap but do not surface anything on their own.
INERT_CALLS = ("str", "int", "float", "bool", "dict", "list", "set", "tuple", "len",
               "repr", "format", "type", "isinstance", "getattr")


def _handler_actions(handler: ast.ExceptHandler) -> dict[str, bool]:
    """What does this handler body do with the error?"""
    logs = raises = returns_error = swallows_exc = False
    effectful = 0
    for node in ast.walk(handler):
        if isinstance(node, ast.Raise):
            raises = True
        if isinstance(node, ast.Call):
            func = node.func
            # log.error(...) / logger.warning(...) / structlog-style
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                if func.value.id in LOG_ATTRS:
                    logs = True
            if isinstance(func, ast.Name) and func.id in ("print", "warn"):
                logs = True
            # exception(...) / self.log(...) / logger.bind(...).error(...)
            if isinstance(func, ast.Attribute) and func.attr in (
                "exception",
                "error",
                "warning",
                "critical",
                "debug",
                "info",
            ):
                logs = True
            # Any other call whose name suggests it observes/records the
            # failure (helper like _log_agent_failure, record_provider_error,
            # metrics counter, retry bookkeeping, ...).
            label = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id
                if isinstance(func, ast.Name)
                else ""
            )
            if label and label not in INERT_CALLS:
                if any(marker in label.lower() for marker in EFFECT_MARKERS):
                    effectful += 1
        if isinstance(node, ast.Return) and node.value is not None:
            returns_error = True
    # `except Exception as exc:` — is `exc` referenced anywhere in the body?
    if handler.name:
        for node in ast.walk(handler):
            if isinstance(node, ast.Name) and node.id == handler.name and node.ctx is ast.Load:
                swallows_exc = False
                break
        else:
            swallows_exc = True
    return {
        "logs": logs,
        "raises": raises,
        "returns_error": returns_error,
        "binds_exc": bool(handler.name),
        "exc_unused": swallows_exc and bool(handler.name),
        "effectful_calls": effectful,
    }


def scan(root: Path) -> list[dict]:
    findings = []
    for path in sorted(root.rglob("*.py")):
        if any(part in {".venv", "node_modules", "__pycache__"} for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:  # a parse error is itself worth reporting
            findings.append({"file": str(path), "line": 0, "kind": "syntax_error", "detail": str(exc)})
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            for handler in node.handlers:
                if handler.type is None:
                    name = "bare"
                elif isinstance(handler.type, ast.Name):
                    name = handler.type.id
                else:
                    name = ast.unparse(handler.type)
                if name not in BROAD:
                    continue
                actions = _handler_actions(handler)
                # Silent = nothing observes the failure: no re-raise, no log,
                # and no other effectful call (record/report/persist/retry...).
                silent = (
                    not actions["logs"]
                    and not actions["raises"]
                    and actions["effectful_calls"] == 0
                )
                findings.append(
                    {
                        "file": str(path),
                        "line": handler.lineno,
                        "kind": "silent_swallow" if silent else "handled",
                        "except_type": name,
                        **actions,
                    }
                )
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    findings = scan(args.root)
    if args.json:
        print(json.dumps(findings, indent=1))
        return 0

    silent = [f for f in findings if f["kind"] == "silent_swallow"]
    print(f"broad `except Exception` handlers: {len(findings)}")
    print(f"  silent (no observable effect):  {len(silent)}")
    print(f"  handled (logged / re-raised / recorded): {len(findings) - len(silent)}")
    if silent:
        print("\nsilent handlers:")
        for f in silent:
            print(f"  {f['file']}:{f['line']}  except {f['except_type']}  (exc bound: {f['binds_exc']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
