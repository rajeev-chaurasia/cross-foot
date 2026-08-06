import importlib.util
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_guard() -> ModuleType:
    path = REPO_ROOT / "scripts" / "check_style.py"
    spec = importlib.util.spec_from_file_location("check_style", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_flags_em_dash(tmp_path: Path) -> None:
    guard = _load_guard()
    bad = tmp_path / "bad.md"
    bad.write_text("a \u2014 b", encoding="utf-8")
    assert guard.check(bad)


def test_flags_attribution_trailer(tmp_path: Path) -> None:
    guard = _load_guard()
    bad = tmp_path / "msg.txt"
    bad.write_text("fix: thing\n\nCo-Authored-By: someone", encoding="utf-8")
    assert guard.check(bad)


def test_clean_file_passes(tmp_path: Path) -> None:
    guard = _load_guard()
    good = tmp_path / "good.py"
    good.write_text("x = 1  # plain hyphen - is fine\n", encoding="utf-8")
    assert not guard.check(good)
