"""Style guard run by pre-commit on staged files and commit messages.

Rejects em-dashes and attribution trailers. Repo policy: plain hyphens,
commas, or colons instead of em-dashes; commits carry no tool attribution.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

BANNED_CHARS = {"\u2014": "em-dash", "\u2015": "horizontal bar"}
BANNED_PHRASES = re.compile("co-authored-by|generated with", re.IGNORECASE)


def check(path: Path) -> list[str]:
    problems: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return problems
    for line_no, line in enumerate(text.splitlines(), start=1):
        for char, label in BANNED_CHARS.items():
            if char in line:
                problems.append(f"{path}:{line_no}: {label}")
        match = BANNED_PHRASES.search(line)
        if match:
            problems.append(f"{path}:{line_no}: banned phrase '{match.group(0)}'")
    return problems


def main(argv: list[str]) -> int:
    problems = [problem for name in argv for problem in check(Path(name))]
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
