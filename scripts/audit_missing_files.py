#!/usr/bin/env python3
"""Find files the repo REFERS to but does not contain.

Upstream-to-upstream comparison cannot see these: a config that points at a
missing script, a module declared but never written, a test fixture that was
never committed. This walks the actual references.

Usage: python3 scripts/audit_missing_files.py [--json]
Exit code 0 always (an audit, not a gate).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "node_modules", "target", "__pycache__", ".venv", "dist",
             "toolchains", "go-work", "gop", "nltk_data", "backups"}


def _walk(pattern: str = "*"):
    for p in ROOT.rglob(pattern):
        if any(part in SKIP_DIRS for part in p.relative_to(ROOT).parts):
            continue
        if p.is_file():
            yield p


def exists(rel: str) -> bool:
    rel = rel.strip().strip('"').strip("'")
    if not rel or rel.startswith(("http://", "https://", "$", "-")):
        return True
    return (ROOT / rel).exists()


findings: list[dict] = []


def note(kind: str, where: str, detail: str) -> None:
    findings.append({"kind": kind, "where": where, "detail": detail})


# --------------------------------------------------------------- Makefile ---

def audit_makefile() -> None:
    mk = ROOT / "Makefile"
    if not mk.exists():
        note("makefile", "Makefile", "Makefile does not exist")
        return
    text = mk.read_text()
    for m in re.finditer(r"^\t(?:@?-?)(?:python3?|bash|sh|\./)?\s*([\w./-]+\.(?:py|sh))", text, re.M):
        ref = m.group(1)
        if not exists(ref):
            note("makefile", "Makefile", f"recipe references missing file: {ref}")
    for m in re.finditer(r"-f\s+([\w./-]+)", text):
        if not exists(m.group(1)):
            note("makefile", "Makefile", f"-f target missing: {m.group(1)}")


# ------------------------------------------------------- Docker / compose ---

def audit_docker() -> None:
    for df in list(_walk("Dockerfile*")) + list(_walk("*.dockerfile")):
        text = df.read_text()
        for m in re.finditer(r"^\s*(?:COPY|ADD)\s+(.+)$", text, re.M | re.I):
            args = m.group(1).split()
            for src in args[:-1]:
                if src.startswith(("--from=", "$")):
                    continue
                rel = src.lstrip("./")
                if rel in ("", "."):
                    continue
                if not exists(rel.rstrip("/")):
                    note("dockerfile", str(df.relative_to(ROOT)), f"COPY source missing: {src}")

    for comp in list(_walk("docker-compose*.yml")):
        text = comp.read_text()
        for m in re.finditer(r"^\s*-\s+([\w./~-]+\.(?:sh|py|json|yml|yaml|conf|sql|toml))", text, re.M):
            ref = m.group(1)
            if ref.startswith(("./", "~")):
                if not exists(ref):
                    note("compose", str(comp.relative_to(ROOT)), f"mounted file missing: {ref}")
        for m in re.finditer(r"env_file:\s*\n\s*-\s*([\w./-]+)", text):
            if not exists(m.group(1)):
                note("compose", str(comp.relative_to(ROOT)), f"env_file missing: {m.group(1)}")


# ------------------------------------------------------------ CI workflows ---

def audit_workflows() -> None:
    wf_dir = ROOT / ".github" / "workflows"
    if not wf_dir.is_dir():
        note("ci", ".github/workflows", "no workflows directory")
        return
    for wf in sorted(wf_dir.glob("*.yml")):
        text = wf.read_text()
        paths: set[str] = set()
        for m in re.finditer(r"^\s{2,}([\w./-]+\.(?:py|sh|json|js|ts|tsx|yml|yaml|toml))(?:\s|$)", text, re.M):
            paths.add(m.group(1))
        for m in re.finditer(r"working-directory:\s*([\w./-]+)", text):
            paths.add(m.group(1))
        for m in re.finditer(r"cache-dependency-path:\s*([\w./-]+)", text):
            paths.add(m.group(1))
        for rel in sorted(paths):
            if rel.startswith(("uses:", "run:")):
                continue
            if "/" in rel or rel.endswith((".py", ".sh", ".json", ".toml")):
                if not exists(rel):
                    note("ci", wf.name, f"CI references missing path: {rel}")


# ------------------------------------------------------------- Python refs ---

FILE_LITERAL = re.compile(r"""["']((?:docs|contracts|scripts|observability|loadtest|alembic|tests|app)/[\w./${}-]+\.(?:md|json|sql|yml|yaml|py|sh|txt|j2|template))["']""")


def audit_python_literals() -> None:
    for py in _walk("*.py"):
        try:
            text = py.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for m in FILE_LITERAL.finditer(text):
            ref = m.group(1)
            if "$" in ref or "{" in ref:
                continue
            if not exists(ref):
                note("py-literal", str(py.relative_to(ROOT)), f"path literal with no file: {ref}")


# --------------------------------------------------------------- Rust mods ---

def audit_rust() -> None:
    for rs in _walk("*.rs"):
        if "vendor/" in str(rs):
            continue                                    # vendored crate: upstream ships complete
        text = rs.read_text(encoding="utf-8")
        base = rs.parent
        for m in re.finditer(r"^\s*(?:pub\s+)?mod\s+(\w+)\s*;", text, re.M):
            name = m.group(1)
            if (base / f"{name}.rs").exists() or (base / name / "mod.rs").exists():
                continue
            note("rust", str(rs.relative_to(ROOT)), f"`mod {name};` has no file")
    for cargo in _walk("Cargo.toml"):
        if "vendor/" in str(cargo):
            continue
        text = cargo.read_text()
        base = cargo.parent
        for key in ("path", "build"):
            for m in re.finditer(rf'^{key}\s*=\s*"([^"]+)"', text, re.M):
                if not (base / m.group(1)).exists():
                    note("cargo", str(cargo.relative_to(ROOT)), f"{key} missing: {m.group(1)}")
        for line in text.splitlines():
            s = line.strip()
            if s.startswith('"') and s.endswith('",') and "/" not in s:
                rel = s.strip('",')
                if rel.startswith(("crates/", "bins/", "benches/")) and not (base / rel).exists():
                    note("cargo", str(cargo.relative_to(ROOT)), f"workspace member missing: {rel}")


# ----------------------------------------------------------------- Go refs ---

def audit_go() -> None:
    for go in _walk("*.go"):
        text = go.read_text(encoding="utf-8")
        for m in re.finditer(r"//go:embed\s+(.+)", text):
            for pat in m.group(1).split():
                pat = pat.strip()
                if not list((go.parent).glob(pat)):
                    note("go", str(go.relative_to(ROOT)), f"go:embed pattern matches nothing: {pat}")


# ------------------------------------------------- frontend config/imports ---

def audit_frontend() -> None:
    for pkg_json in (ROOT / "dashboard" / "package.json", ROOT / "dashboard-next" / "package.json"):
        if not pkg_json.exists():
            note("frontend", str(pkg_json.relative_to(ROOT)), "package.json missing")
            continue
        data = json.loads(pkg_json.read_text())
        base = pkg_json.parent
        for name, cmd in (data.get("scripts") or {}).items():
            for m in re.finditer(r"([\w./-]+\.(?:js|ts|mjs|cjs|json|html))", cmd):
                ref = m.group(1)
                if not (base / ref).exists():
                    note("frontend", f"{pkg_json.parent.name}/package.json",
                         f"script '{name}' references missing {ref}")
        for field in ("main", "module", "types", "style"):
            rel = data.get(field)
            if rel and not (base / rel).exists() and "/" in str(rel):
                note("frontend", f"{pkg_json.parent.name}/package.json", f"{field} missing: {rel}")

    for ts in _walk("tsconfig.json"):
        text = re.sub(r"//.*", "", ts.read_text())
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        for m in re.finditer(r'"\./([\w./-]+)"', json.dumps(data)):
            ref = m.group(1)
            if not (ts.parent / ref).exists():
                note("frontend", str(ts.relative_to(ROOT)), f"tsconfig references missing {ref}")


# ------------------------------------------------------------------- run ----

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--json", action="store_true")
    args = p.parse_args()
    if not (ROOT / "app").is_dir():
        print("run me from inside the repo", file=sys.stderr)
        return 2

    for fn in (audit_makefile, audit_docker, audit_workflows, audit_python_literals,
               audit_rust, audit_go, audit_frontend):
        try:
            fn()
        except Exception as exc:                        # audit must never crash
            note("audit-error", fn.__name__, f"{type(exc).__name__}: {exc}")

    if args.json:
        print(json.dumps(findings, indent=1))
    else:
        print(f"reference-driven checks: {len(findings)} finding(s)")
        for f in findings:
            print(f"  [{f['kind']}] {f['where']}: {f['detail']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
