"""Fail CI when tracked source files appear to contain committed credentials.

This is a conservative repository-local guard. It intentionally ignores example
placeholders and generated/cache directories while detecting common private key,
OpenAI token, GitHub token, and non-placeholder Bybit credential assignments.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SKIP_PREFIXES = (
    ".git/",
    ".venv/",
    "venv/",
    "node_modules/",
    "__pycache__/",
)

BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip",
    ".sqlite", ".sqlite3", ".db", ".pyc",
}

PATTERNS = (
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("OpenAI key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("GitHub token", re.compile(r"\bgh(?:p|o|u|s|r)_[A-Za-z0-9]{20,}\b")),
    (
        "Bybit API credential",
        re.compile(
            r"(?im)^\s*(?:BYBIT_API_KEY|BYBIT_API_SECRET)\s*=\s*"
            r"(?!\s*$|your_|example|replace|changeme|<)[^\s#]{8,}\s*$"
        ),
    ),
)


def tracked_files() -> list[Path]:
    output = subprocess.check_output(
        ["git", "ls-files", "-z"], cwd=ROOT
    ).decode("utf-8", errors="strict")
    return [ROOT / name for name in output.split("\0") if name]


def main() -> int:
    findings: list[str] = []
    for path in tracked_files():
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith(SKIP_PREFIXES) or path.suffix.lower() in BINARY_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for label, pattern in PATTERNS:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                findings.append(f"{relative}:{line}: possible {label}")

    if findings:
        print("Committed-secret scan failed:")
        for finding in findings:
            print(f"- {finding}")
        return 1

    print("Committed-secret scan passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
