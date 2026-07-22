#!/usr/bin/env python3
"""Fleet gate: fail the build if a credential is baked into source.

Canonical source: bot-infra/shared/check_no_baked_secrets.py
Copy to a repo root and run it in CI:  python check_no_baked_secrets.py .

Written after the 2026-07-20 fleet secret leak, where live Shopify, Gorgias,
Whop, Chargeflow and Parcel Panel credentials sat in source as `getenv`
defaults -- and one pair sat in a plaintext deploy guide, which a code-only
pattern grep missed. So this scans docs too.

Two detectors:
  1. Known credential prefixes anywhere (code OR docs).
  2. A `getenv`/`environ.get` fallback default on a secret-named variable.

Escape hatch: put `# secret-exempt: <reason>` on the offending line.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# 1. Provider prefixes that are never legitimate in source.
# `sk-` gets its own alternative: modern keys carry an early subtype segment
# (sk-proj-...), so a rule demanding 16 unbroken alphanumerics would miss them.
PREFIX_RE = re.compile(
    r"(?:shpss_|shpat_|shppa_|shpca_|apik_|xkeysib-|ghp_|github_pat_)[A-Za-z0-9_\-]{8,}"
    r"|sk-[A-Za-z0-9_\-]{20,}"
)

# 2. A fallback default on a secret-named env var:
#    os.getenv("X_SECRET", "literal")  /  os.environ.get(...)  /  os.getenv("X") or "literal"
# The inner alternation MUST stay non-capturing: a capturing group here shifts
# every later group index, which silently pointed the placeholder check at the
# keyword instead of the value.
SECRET_NAME = r"[A-Z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL)[A-Z0-9_]*"
FALLBACK_RE = re.compile(
    rf"""os\.(?:getenv|environ\.get)\(\s*["']({SECRET_NAME})["']\s*
        (?:,\s*["']([^"']{{16,}})["']       # two-arg default
        |\)\s*or\s*["']([^"']{{16,}})["'])  # `or "..."` fallback
    """,
    re.VERBOSE,
)

SCAN_SUFFIXES = {".py", ".md", ".yml", ".yaml", ".sh", ".env.example"}
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache"}
# Test fixtures legitimately carry fake credential-shaped strings.
SKIP_NAME_HINTS = ("test_", "_test", "conftest")
EXEMPT = "secret-exempt:"

# Placeholder WORDS that mark a value as an obvious non-secret. Deliberately the
# same conservative set the guard shipped with -- broad substrings (abcdef,
# 123456, jouw, ...) are NOT here, because any of them could occur inside a real
# credential. A doc/spec that must keep an example credential uses a
# '# secret-exempt:' marker instead of weakening this detector for everyone.
PLACEHOLDER_RE = re.compile(
    r"(xxx+|your[_-]|example|placeholder|dummy|fake|redacted|<[^>]+>|\.\.\.)", re.I
)

# A var whose name ends this way is CONFIG, not a secret, even if the name
# contains SECRET/KEY/TOKEN (e.g. SECRETS_LIST_FILE, TOKEN_PATH, KEY_ID). This is
# also where a legitimate URL value lives (…_URL / …_URI), so URL-shaped values
# are handled by name here rather than by value shape below.
CONFIG_SUFFIX_RE = re.compile(
    r"_(FILE|PATH|DIR|LIST|URL|URI|NAME|ID|HOST|PORT|REGION|BUCKET|ENV|MODE|LEVEL)$"
)
# A value that is ENTIRELY a local path or a bare filename is not a credential.
# Fully anchored; the path body allows only path-safe chars, so a base64/secret
# value with '+', '=' or other symbols stays flagged. URLs are intentionally NOT
# skipped by shape -- a URL can embed a credential (user:pass@ or a signed query).
NON_SECRET_VALUE_RE = re.compile(
    r"^(?:(?:/|\./|\.\./|~/)[\w./\-]+"
    r"|[\w.\-]+\.(?:json|ya?ml|txt|pem|cfg|ini|env|log))$"
)


def _skip(path: Path) -> bool:
    if any(part in SKIP_DIRS for part in path.parts):
        return True
    if "tests" in path.parts:
        return True
    return any(hint in path.name for hint in SKIP_NAME_HINTS)


def scan(root: Path) -> list[tuple[Path, int, str]]:
    findings: list[tuple[Path, int, str]] = []
    for path in root.rglob("*"):
        if not path.is_file() or _skip(path):
            continue
        if path.suffix not in SCAN_SUFFIXES and path.name != ".env.example":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        lines = text.splitlines()

        def line_at(pos: int) -> int:
            return text.count("\n", 0, pos) + 1

        def exempted(start: int, end: int) -> bool:
            # Honour the marker anywhere in the matched span, or on the line
            # just above. Note the span ends at the default value's closing
            # quote, so on a multi-line call the marker must sit on or before
            # that line -- not on a later closing-parenthesis line.
            first, last = line_at(start), line_at(end)
            span = lines[max(0, first - 2):last]
            return any(EXEMPT in ln for ln in span)

        hits: dict[int, str] = {}

        # Scan whole-file, not line-by-line: a getenv call is often wrapped
        # across several lines, and per-line scanning silently misses it.
        for m in PREFIX_RE.finditer(text):
            value = m.group(0)
            # Judge the MATCHED VALUE, never the surrounding text -- otherwise a
            # stray word like "example" on the line disables the whole detector.
            if PLACEHOLDER_RE.search(value) or exempted(m.start(), m.end()):
                continue
            hits.setdefault(line_at(m.start()), "baked credential literal")

        for m in FALLBACK_RE.finditer(text):
            name, value = m.group(1), (m.group(2) or m.group(3) or "")
            # A config-suffixed name (…_FILE, …_PATH, …_ID) or a local-path value
            # is not a credential, even when the name contains SECRET/KEY/TOKEN.
            if CONFIG_SUFFIX_RE.search(name) or NON_SECRET_VALUE_RE.match(value):
                continue
            if PLACEHOLDER_RE.search(value) or exempted(m.start(), m.end()):
                continue
            hits[line_at(m.start())] = f"fallback default on {name}"

        findings.extend((path, ln, why) for ln, why in sorted(hits.items()))
    return findings


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    findings = scan(root)
    if not findings:
        print("check_no_baked_secrets: OK - no baked credentials found")
        return 0
    print("check_no_baked_secrets: FAIL - credentials must load from env only\n")
    for path, lineno, why in findings:
        # Never echo the matched value - printing it would leak into CI logs.
        print(f"  {path.relative_to(root)}:{lineno}: {why}")
    print(
        "\nFix: read the value from the environment with no literal fallback, set it as a "
        "repo secret + VM .env, and rotate the exposed value at the provider.\n"
        "If a match is genuinely not a credential, append '# secret-exempt: <reason>'."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
