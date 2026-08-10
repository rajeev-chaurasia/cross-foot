"""The published numbers: the summary tile from the database, the rest from a scorecard.

Nothing here computes an accuracy figure. The scorecard is a committed artifact
and this route hands it over unchanged, so a number on the metrics page traces to
a file with a run id on it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from pydantic import ValidationError

from crossfoot.api.deps import Connection, Paths
from crossfoot.api.dto import MetricsPayload, Summary
from crossfoot.db import stats
from crossfoot.models.scorecard import Scorecard

_LOGGER = logging.getLogger(__name__)

router = APIRouter(tags=["metrics"])

SCORECARD_FILENAME = "scorecard.json"
# Where the scorecards live is server configuration, so it stays out of a body a
# client reads; the operator learns the directory from the log line instead.
NO_SCORECARD_DETAIL = "no committed scorecard"


@router.get("/stats/summary")
def stats_summary(connection: Connection) -> Summary:
    """Counts, rates, and money, all of them counted in SQL."""
    return Summary.from_row(stats.summary(connection))


@router.get("/metrics")
def metrics(paths: Paths) -> MetricsPayload:
    """The latest committed scorecard with its calibration points and threshold sweep."""
    # This docstring is the route description in the frozen OpenAPI snapshot, so
    # it stays verbatim; the sweep it publishes is the scorecard's own, in the
    # order the scorecard wrote it, because that order is what identifies the
    # held out result. See MetricsPayload.of for why applied_thresholds is not
    # served in its place.
    scorecard = latest_scorecard(paths.scorecards_dir)
    if scorecard is None:
        _LOGGER.warning("no committed scorecard under %s", paths.scorecards_dir)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NO_SCORECARD_DETAIL)
    return MetricsPayload.of(scorecard)


def latest_scorecard(scorecards_dir: Path) -> Scorecard | None:
    """The newest scorecard by the time it was written, or None when none is committed."""
    scorecards: list[Scorecard] = []
    for path in sorted(scorecards_dir.glob(f"*/{SCORECARD_FILENAME}")):
        try:
            scorecards.append(Scorecard.model_validate_json(path.read_bytes()))
        except ValidationError:
            # A scorecard written before a schema change is not a reason to lose
            # the metrics page; it is a reason to stop publishing that one.
            _LOGGER.warning("ignoring unreadable scorecard %s", path)
    return max(scorecards, key=lambda card: (card.created_at, card.run_id), default=None)
