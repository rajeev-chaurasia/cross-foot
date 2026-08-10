"""Check a regenerated corpus against the committed scorecard.

Scores the deterministic tiers of a freshly generated dataset and compares them
cell by cell with the numbers this repository publishes. Nothing is written: the
comparison runs in process so a reproduction attempt cannot leave a scorecard
behind that looks like a published one.

Two tiers are deliberately out of scope and reported as such rather than
silently passed. The scanned tiers need a vision model, and which model read
which document lives in an uncommitted cost ledger. `scan_heavy` could not be
compared even with that model, because 17 of its 32 renders differ between two
runs at the same seed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from crossfoot.constants import QualityTier, SplitName
from crossfoot.evals.metrics import score_fields
from crossfoot.evals.runner import extract_split, load_manifest

# The tiers a fresh clone can reproduce with no API key and no GPU.
DETERMINISTIC_TIERS = (QualityTier.CLEAN_DIGITAL, QualityTier.CSV, QualityTier.XLSX)
COUNTERS = ("fields_expected", "fields_extracted", "correct_canonical", "fields_spurious")
SCORECARD_NAME = "scorecard.json"


def _committed_cells(scorecards_dir: Path) -> tuple[dict[tuple[str, str], dict[str, int]], str]:
    """The newest committed scorecard that carries field accuracy, and its cells."""
    cards = sorted(scorecards_dir.glob(f"*/{SCORECARD_NAME}"))
    for path in reversed(cards):
        card = json.loads(path.read_text(encoding="utf-8"))
        if card.get("field_accuracy"):
            cells = {
                (cell["field_family"], cell["quality_tier"]): cell
                for cell in card["field_accuracy"]
            }
            return cells, card["run_id"]
    raise SystemExit(f"no committed scorecard with field accuracy under {scorecards_dir}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path, help="Freshly generated dataset directory.")
    parser.add_argument("--scorecards", type=Path, default=Path("scorecards"))
    parser.add_argument("--split", default=SplitName.TEST.value)
    args = parser.parse_args()

    split = SplitName(args.split)
    committed, run_id = _committed_cells(args.scorecards)
    manifest = load_manifest(args.dataset)
    fresh = score_fields(extract_split(args.dataset, manifest, split).documents, manifest, split)

    print(f"Comparing {args.dataset} against scorecard {run_id}, split {split.value}.")
    mismatches = 0
    compared = 0
    for cell in fresh:
        if cell.quality_tier not in DETERMINISTIC_TIERS:
            continue
        key = (cell.field_family.value, cell.quality_tier.value)
        want = committed.get(key)
        if want is None:
            print(f"  MISSING  {key[0]}/{key[1]} is not in the committed scorecard")
            mismatches += 1
            continue
        compared += 1
        for counter in COUNTERS:
            got = getattr(cell, counter)
            if got != want[counter]:
                print(
                    f"  DIFFERS  {key[0]}/{key[1]} {counter}: got {got}, published {want[counter]}"
                )
                mismatches += 1

    print(f"\n{compared} deterministic cells compared, {mismatches} differences.")
    print(
        "Not compared: scan_light and scan_heavy. Both need a vision model, and scan_heavy\n"
        "does not regenerate byte identically, so its images would differ from the ones the\n"
        "published numbers were read off. See Limitations in the README."
    )
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
