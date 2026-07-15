#!/usr/bin/env python3
"""Fleet usage-reporting CI gate (fleet-usage-standard design spec, §5 layer 1).

Scans a repo's `*.py` files for paid-provider LLM/media SDK imports (or paid
API host string literals). Any file that calls a paid provider MUST either
import `usage_reporter` (the fleet spend reporter) itself, or carry a
`# usage-exempt: <reason>` comment at the paid-provider import site.
Otherwise CI fails with a fix-it message naming the offending files.

Mirrors command-center's `scripts/check-usage.mjs` check 2 in Python terms.
Canonical copy: bot-infra/shared/check_usage_reporting.py -- copied per-repo
(same convention as usage_reporter.py, see bot-infra/shared/README.md) and
wired as a CI step next to ruff + py_compile.

Usage:
    python check_usage_reporting.py [repo_root]

repo_root defaults to the current directory. Exit codes: 0 = clean (or
nothing to check), 1 = one or more offending files found.
"""
import argparse
import ast
import os
import re
import sys

PAID_IMPORT_MODULES = (
    "anthropic",
    "openai",
    "google.generativeai",
    "google.genai",
    "fal_client",
    "fal",
    "elevenlabs",
)

PAID_HOSTS = (
    "api.anthropic.com",
    "api.openai.com",
    "generativelanguage.googleapis.com",
    "fal.run",
    "api.elevenlabs.io",
)

EXCLUDE_DIRS = {
    ".git", "venv", ".venv", "env", "node_modules", "__pycache__",
    "build", "dist", ".tox", ".mypy_cache", ".pytest_cache",
}

# Self-exempt: neither this script nor the reporter it looks for should ever
# flag themselves (the reporter module legitimately mentions provider names
# in comments/docstrings; this script mentions them in its own tables above).
SELF_NAME = "check_usage_reporting.py"
REPORTER_NAME = "usage_reporter.py"

_IMPORT_RE = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import\b|import\s+([\w.]+))")

# Matches an import of a module literally named (or dotted-ending-in)
# `usage_reporter`, e.g. `from usage_reporter import UsageReporter`,
# `from worker.usage_reporter import UsageReporter`, `import usage_reporter`.
USAGE_REPORTER_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+(?:[\w.]+\.)?usage_reporter\s+import\b"
    r"|import\s+(?:[\w.]+\.)?usage_reporter\b)",
    re.MULTILINE,
)

EXEMPT_RE = re.compile(r"#\s*usage-exempt\s*:\s*\S")


def _strip_comments(line: str) -> str:
    """Best-effort: drop a trailing `# ...` comment. Naive about strings
    containing '#' -- acceptable for a lint-style scan, not a full parser."""
    in_str = None
    for i, ch in enumerate(line):
        if in_str:
            if ch == in_str and line[i - 1] != "\\":
                in_str = None
        elif ch in ("'", '"'):
            in_str = ch
        elif ch == "#":
            return line[:i]
    return line


def _iter_py_files(repo_root: str):
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def _module_matches_paid(module: str) -> bool:
    return any(module == m or module.startswith(m + ".") for m in PAID_IMPORT_MODULES)


def _find_paid_import_lines_ast(raw_text):
    """AST-based paid-import detection: robust to grouped (`import os,
    anthropic`), aliased, and multi-line imports that a per-line regex misses
    (Codex P1 -- a regex capturing only the first module let `import os,
    anthropic` slip the gate). Returns 0-based line indexes, or None if the
    file doesn't parse (caller falls back to the regex scan)."""
    try:
        tree = ast.parse(raw_text)
    except SyntaxError:
        return None
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(_module_matches_paid(alias.name) for alias in node.names):
                hits.append(node.lineno - 1)
        elif isinstance(node, ast.ImportFrom):
            # A relative import (level>0) has module=None. For an absolute
            # `from X import a, b`, the paid provider can be X itself
            # (`from anthropic import Anthropic`) OR `X.a` for a submodule
            # imported by name -- crucially `from google import genai`, where
            # module is "google" and the paid package is "google.genai" (Codex
            # P1 r2: matching only node.module misses this common form).
            if not node.module:
                continue
            if _module_matches_paid(node.module) or any(
                _module_matches_paid(f"{node.module}.{alias.name}") for alias in node.names
            ):
                hits.append(node.lineno - 1)
    return hits


def _find_paid_import_lines_regex(lines):
    """Regex fallback for files AST can't parse. Weaker (misses grouped
    imports) but better than skipping a non-parsing file entirely."""
    hits = []
    for i, raw in enumerate(lines):
        line = _strip_comments(raw)
        m = _IMPORT_RE.match(line)
        if not m:
            continue
        module = m.group(1) or m.group(2)
        if module and _module_matches_paid(module):
            hits.append(i)
    return hits


def _find_paid_import_lines(raw_text, lines):
    ast_hits = _find_paid_import_lines_ast(raw_text)
    return ast_hits if ast_hits is not None else _find_paid_import_lines_regex(lines)


def _find_paid_host_lines(lines):
    hits = []
    for i, raw in enumerate(lines):
        line = _strip_comments(raw)
        if any(host in line for host in PAID_HOSTS):
            hits.append(i)
    return hits


def _imports_usage_reporter(raw_text, lines) -> bool:
    """True if the file imports a module named (or dotted-ending-in)
    `usage_reporter`. AST-based (same grouped-import robustness as the paid
    scan), with the regex as a fallback for non-parsing files."""
    try:
        tree = ast.parse(raw_text)
    except SyntaxError:
        stripped_text = "\n".join(_strip_comments(line) for line in lines)
        return bool(USAGE_REPORTER_IMPORT_RE.search(stripped_text))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name == "usage_reporter" or a.name.endswith(".usage_reporter") for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "usage_reporter" or mod.endswith(".usage_reporter"):
                return True
    return False


def _has_exemption_at(lines, idx: int) -> bool:
    """Exemption comment on the offending line itself, or the line directly
    above it. Comments are NOT stripped here -- the exemption IS a comment."""
    candidates = [lines[idx]]
    if idx > 0:
        candidates.append(lines[idx - 1])
    return any(EXEMPT_RE.search(c) for c in candidates)


def check_file(path: str):
    """Return a fix-it reason string if `path` offends, else None."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            raw_text = f.read()
    except OSError:
        return None

    lines = raw_text.splitlines()
    offending_lines = sorted(set(_find_paid_import_lines(raw_text, lines) + _find_paid_host_lines(lines)))
    if not offending_lines:
        return None

    if _imports_usage_reporter(raw_text, lines):
        return None

    unexempted = [i for i in offending_lines if not _has_exemption_at(lines, i)]
    if not unexempted:
        return None

    line_no = unexempted[0] + 1
    return (
        f"{path}:{line_no}: paid-provider call with no usage_reporter import "
        f"and no '# usage-exempt: <reason>' at the import site"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_usage_reporting.py",
        description=(
            "Fleet usage-reporting CI gate: fails if a repo makes paid-provider "
            "API calls (Anthropic/OpenAI/Gemini/fal/ElevenLabs) without importing "
            "usage_reporter, and without an explicit '# usage-exempt: <reason>' "
            "comment at the import site."
        ),
    )
    parser.add_argument(
        "repo_root", nargs="?", default=".",
        help="Repo root to scan (default: current directory).",
    )
    args = parser.parse_args(argv)

    offenders = []
    for path in _iter_py_files(args.repo_root):
        base = os.path.basename(path)
        if base in (SELF_NAME, REPORTER_NAME):
            continue
        reason = check_file(path)
        if reason:
            offenders.append(reason)

    if not offenders:
        print("check_usage_reporting: OK -- all paid-provider call sites are reported or exempt.")
        return 0

    print("check_usage_reporting: FAILED", file=sys.stderr)
    print(
        "The following files call a paid provider (Anthropic/OpenAI/Gemini/fal/"
        "ElevenLabs) but do not import usage_reporter and have no "
        "'# usage-exempt: <reason>' comment at the import site:",
        file=sys.stderr,
    )
    for reason in offenders:
        print(f"  - {reason}", file=sys.stderr)
    print(
        "\nFix: add `from usage_reporter import UsageReporter` (adjust the import "
        "path to your repo's copy) and report spend/activity at the call site, OR "
        "annotate the paid import with `# usage-exempt: <reason>` if this call is "
        "genuinely not billed fleet spend.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
