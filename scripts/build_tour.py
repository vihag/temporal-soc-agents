"""Build CODE_TOUR.md from docs/CODE_TOUR.tpl.md.

The template links code as {{path::text on the line}}. This script finds the first line that
contains the text and writes `path#L<n>`, so the tour's links stay exact as the code moves.
Run it after changing code: `uv run python scripts/build_tour.py`. tests/test_docs.py fails
when CODE_TOUR.md is stale or an anchor no longer exists.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE, OUTPUT = ROOT / "docs" / "CODE_TOUR.tpl.md", ROOT / "CODE_TOUR.md"
ANCHOR = re.compile(r"\{\{([^:}]+)::([^}]+)\}\}")


def line_of(path: str, text: str) -> int:
    for number, line in enumerate((ROOT / path).read_text().splitlines(), start=1):
        if text in line:
            return number
    raise LookupError(f"CODE_TOUR anchor not found: {path} :: {text}")


def render() -> str:
    return ANCHOR.sub(lambda m: f"{m[1]}#L{line_of(m[1], m[2])}", TEMPLATE.read_text())


if __name__ == "__main__":
    OUTPUT.write_text(render())
    print(f"wrote {OUTPUT.relative_to(ROOT)}")
