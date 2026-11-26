"""CODE_TOUR.md links to exact lines. If code moved, rebuild it:
uv run python scripts/build_tour.py"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_code_tour_is_up_to_date():
    spec = importlib.util.spec_from_file_location("build_tour", ROOT / "scripts" / "build_tour.py")
    assert spec and spec.loader
    build_tour = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build_tour)
    assert (ROOT / "CODE_TOUR.md").read_text() == build_tour.render(), "run scripts/build_tour.py"
