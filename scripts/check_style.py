"""Style guard run by pre-commit on staged files and commit messages.

Rejects long dashes and attribution trailers. Repo policy: a comma, a colon, a
semicolon, or two sentences in place of a dash that is not a plain hyphen;
commits carry no tool attribution.

The en-dash and the figure dash are here for the same reason the em-dash is.
They render as the same long stroke at body size, so a file that swapped one
for the other would read as though the policy had been followed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

BANNED_CHARS = {
    "\u2012": "figure dash",
    "\u2013": "en-dash",
    "\u2014": "em-dash",
    "\u2015": "horizontal bar",
}
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
