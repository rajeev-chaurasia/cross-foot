"""The published figures: read a committed scorecard, draw it, write the PNGs beside it.

Nothing here measures anything. Every value a figure shows is read from the
scorecard whose run id its caption names, so a figure and its numbers travel
together and one can always be checked against the other. Drawing goes through
the Agg canvas directly rather than pyplot: the same backend, none of the global
figure state, and no display is ever needed. Everything is written at 2x so it
stays legible at README width, and no figure carries a timestamp or any random
jitter, so the same scorecard renders the same bytes twice.

Two conventions the frozen scorecard shape cannot express on its own are stated
here, once, because both the writer and the reader depend on them:

- A cell that produced no reading at all is absent, not zero. XLSX has no
  extractor, so those fields were never attempted, and drawing them as 0 percent
  would claim the pipeline tried and failed at something it never tried.
- A scorecard's `threshold_sweep` runs one family at a time: the sweep measured
  on the split the threshold was chosen from, in ascending threshold order, then
  one last point, which is what the reported split reached at the applied
  threshold. The applied point is the curve point sharing that threshold.
  `family_sweeps` is the only reader of that layout, and it raises rather than
  guesses when the last point does not line up with the curve.

Greyscale safety is by construction: series are told apart by marker and line
style, and the only fills are levels of grey.
"""

from __future__ import annotations

import textwrap
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import matplotlib
import numpy as np
from matplotlib.axes import Axes
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import Rectangle

from crossfoot.constants import ExceptionType, FieldFamily, QualityTier, ReconMode
from crossfoot.models.scorecard import (
    CalibrationBin,
    FieldAccuracyCell,
    ReconCell,
    Scorecard,
    ThresholdPoint,
)

SCORECARD_FILENAME = "scorecard.json"

FIELD_ACCURACY_PNG = "field-accuracy-heatmap.png"
RELIABILITY_PNG = "reliability-diagram.png"
THRESHOLD_SWEEP_PNG = "threshold-sweep.png"
EXCEPTION_RECALL_PNG = "exception-recall.png"

# 2x: every figure is sized in inches for a README column and rendered at twice
# the ordinary screen density, so its text survives being scaled down in a page.
BASE_DPI = 100
FIGURE_SCALE = 2
DPI = BASE_DPI * FIGURE_SCALE

# Stripped so two runs of one scorecard write the same bytes. The default value
# names the matplotlib version, which is not a fact about the numbers.
PNG_METADATA: dict[str, str | None] = {"Software": None}

PERCENT = 100.0

_TITLE_SIZE = 11
_LABEL_SIZE = 9
_TICK_SIZE = 8
_ANNOTATION_SIZE = 7
_CAPTION_SIZE = 7
_CAPTION_COLOR = "0.35"
# Wrap to the figure's own width rather than a fixed count, so a wide figure does
# not carry a narrow column of caption. Measured for DejaVu Sans at this size.
_CAPTION_CHARS_PER_INCH = 16
# Caption band, in inches: one line's height times the lines, plus a margin.
_CAPTION_LINE_INCHES = 0.16
_CAPTION_MARGIN_INCHES = 0.10

_GRID_COLOR = "0.85"
_IDEAL_COLOR = "0.45"

# Matplotlib narrows this itself; naming it keeps the tables below checkable.
LineStyle = Literal["solid", "dashed", "dotted", "dashdot"]

# Told apart without colour: one marker and one line style per family.
_FAMILY_MARKERS: dict[FieldFamily, str] = {
    FieldFamily.AMOUNT: "o",
    FieldFamily.DATE: "s",
    FieldFamily.REFERENCE: "^",
    FieldFamily.TEXT: "D",
}
_FAMILY_LINES: dict[FieldFamily, LineStyle] = {
    FieldFamily.AMOUNT: "solid",
    FieldFamily.DATE: "dashed",
    FieldFamily.REFERENCE: "dotted",
    FieldFamily.TEXT: "dashdot",
}
_FAMILY_GREYS: dict[FieldFamily, str] = {
    FieldFamily.AMOUNT: "0.05",
    FieldFamily.DATE: "0.35",
    FieldFamily.REFERENCE: "0.15",
    FieldFamily.TEXT: "0.50",
}

_MODE_FILL: dict[ReconMode, str] = {ReconMode.ORACLE: "0.78", ReconMode.END_TO_END: "0.30"}
_MODE_HATCH: dict[ReconMode, str] = {ReconMode.ORACLE: "", ReconMode.END_TO_END: "///"}
_MODE_LINES: dict[ReconMode, LineStyle] = {
    ReconMode.ORACLE: "solid",
    ReconMode.END_TO_END: "dashed",
}
_MODE_LABELS: dict[ReconMode, str] = {
    ReconMode.ORACLE: "oracle",
    ReconMode.END_TO_END: "end to end",
}

# Above this percentage a heatmap cell is dark enough that black text on it
# stops being readable.
_DARK_TEXT_ABOVE = 55.0

_FAMILY_ORDER = {family: index for index, family in enumerate(FieldFamily)}
_TIER_ORDER = {tier: index for index, tier in enumerate(QualityTier)}
_TYPE_ORDER = {kind: index for index, kind in enumerate(ExceptionType)}


class MalformedSweepError(ValueError):
    """A published sweep whose last point does not name a threshold on its own curve."""


@dataclass(frozen=True, slots=True)
class FamilySweep:
    """One family's published sweep, split into the curve, the choice, and the result."""

    field_family: FieldFamily
    # Measured on the split the threshold was chosen from.
    curve: tuple[ThresholdPoint, ...]
    # The curve point a threshold was chosen at, and what the reported split
    # reached at that same threshold. The distance between them is the drift.
    applied: ThresholdPoint
    achieved: ThresholdPoint


@dataclass(frozen=True, slots=True)
class RenderedFigures:
    """What one render wrote, and what it could not draw for want of numbers."""

    written: tuple[Path, ...]
    skipped: tuple[str, ...]


def latest_scorecard_path(scorecards_root: Path) -> Path | None:
    """The most recently written scorecard, or None when none is committed."""
    dated: list[tuple[str, str, Path]] = []
    for path in sorted(scorecards_root.glob(f"*/{SCORECARD_FILENAME}")):
        card = _read(path)
        if card is not None:
            dated.append((card.created_at.isoformat(), card.run_id, path))
    if not dated:
        return None
    return max(dated)[2]


def render_figures(scorecard_path: Path, *, scorecards_root: Path | None = None) -> RenderedFigures:
    """Draw every figure this scorecard's numbers support, into its own directory."""
    scorecard = Scorecard.model_validate_json(scorecard_path.read_bytes())
    out_dir = scorecard_path.parent
    root = scorecards_root if scorecards_root is not None else out_dir.parent
    written: list[Path] = []
    skipped: list[str] = []

    if scorecard.field_accuracy:
        written.append(_save(_field_accuracy_figure(scorecard), out_dir / FIELD_ACCURACY_PNG))
    else:
        skipped.append(f"{FIELD_ACCURACY_PNG}: the scorecard publishes no field accuracy")
    if scorecard.calibration:
        written.append(_save(_reliability_figure(scorecard), out_dir / RELIABILITY_PNG))
    else:
        skipped.append(f"{RELIABILITY_PNG}: the scorecard publishes no calibration bins")
    if scorecard.threshold_sweep:
        written.append(_save(_threshold_sweep_figure(scorecard), out_dir / THRESHOLD_SWEEP_PNG))
    else:
        skipped.append(f"{THRESHOLD_SWEEP_PNG}: the scorecard publishes no threshold sweep")
    if scorecard.reconciliation:
        sources = _recon_sources(scorecard, root)
        written.append(_save(_exception_recall_figure(sources), out_dir / EXCEPTION_RECALL_PNG))
    else:
        skipped.append(f"{EXCEPTION_RECALL_PNG}: the scorecard publishes no reconciliation")
    return RenderedFigures(written=tuple(written), skipped=tuple(skipped))


def family_sweeps(points: Sequence[ThresholdPoint]) -> tuple[FamilySweep, ...]:
    """Recover each family's curve, applied point, and achieved point from one tuple.

    The layout is the module docstring's: curve first, achieved point last. A
    tuple that does not obey it raises, because silently drawing a curve point as
    an operating point would publish a number nothing measured.
    """
    grouped: defaultdict[FieldFamily, list[ThresholdPoint]] = defaultdict(list)
    for point in points:
        grouped[point.field_family].append(point)
    sweeps: list[FamilySweep] = []
    for family in sorted(grouped, key=lambda item: _FAMILY_ORDER[item]):
        run = grouped[family]
        if len(run) < 2:
            raise MalformedSweepError(f"{family} publishes {len(run)} sweep points, needs a curve")
        curve, achieved = tuple(run[:-1]), run[-1]
        applied = next((p for p in curve if p.threshold == achieved.threshold), None)
        if applied is None:
            raise MalformedSweepError(
                f"{family} reports a result at threshold {achieved.threshold},"
                " which is not a point on its published curve"
            )
        sweeps.append(
            FamilySweep(field_family=family, curve=curve, applied=applied, achieved=achieved)
        )
    return tuple(sweeps)


def cell_accuracy(cell: FieldAccuracyCell) -> float | None:
    """Canonical accuracy as a percentage, or None when the cell was never extracted.

    Absent is not zero. A cell whose extractor never ran produced no reading to
    be right or wrong about, so it has no accuracy rather than one of nought.
    """
    if cell.fields_extracted == 0:
        return None
    return _percent(cell.correct_canonical, cell.fields_expected)


def scorecard_stamp(scorecard: Scorecard) -> str:
    """The provenance line every caption opens with."""
    return (
        f"Scorecard {scorecard.run_id}, {scorecard.split} split,"
        f" dataset {scorecard.dataset_config_hash[:8]}, git {scorecard.git_sha}."
    )


def figure_names(scorecard: Scorecard) -> tuple[str, ...]:
    """Which PNGs a scorecard's sections support, for callers that only need the list."""
    sections: tuple[tuple[str, Sequence[object]], ...] = (
        (FIELD_ACCURACY_PNG, scorecard.field_accuracy),
        (RELIABILITY_PNG, scorecard.calibration),
        (THRESHOLD_SWEEP_PNG, scorecard.threshold_sweep),
        (EXCEPTION_RECALL_PNG, scorecard.reconciliation),
    )
    return tuple(name for name, section in sections if section)


def _percent(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else PERCENT * numerator / denominator


def _read(path: Path) -> Scorecard | None:
    try:
        return Scorecard.model_validate_json(path.read_bytes())
    except ValueError:
        return None


def _recon_sources(scorecard: Scorecard, root: Path) -> dict[ReconMode, Scorecard]:
    """This scorecard's modes, plus the newest committed run of any mode it lacks.

    Oracle against end to end is the point of the figure: the gap between them
    attributes error to extraction rather than to matching. One reconcile command
    writes one mode, so the counterpart is looked up rather than recomputed, and
    the caption names both run ids.
    """
    sources = {cell.mode: scorecard for cell in scorecard.reconciliation}
    for path in sorted(root.glob(f"*/{SCORECARD_FILENAME}")):
        other = _read(path)
        if other is None or other.run_id == scorecard.run_id:
            continue
        same_run = (other.dataset_config_hash, other.split)
        if same_run != (scorecard.dataset_config_hash, scorecard.split):
            continue
        for mode in {cell.mode for cell in other.reconciliation} - set(sources):
            sources[mode] = other
    return sources


def _figure(width: float, height: float) -> Figure:
    figure = Figure(figsize=(width, height), dpi=DPI)
    FigureCanvasAgg(figure)
    return figure


def _save(figure: Figure, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(path, format="png", metadata=PNG_METADATA)
    return path


def _caption(figure: Figure, text: str) -> None:
    """Lay the caption in a band reserved under the axes, so nothing overlaps."""
    lines = textwrap.wrap(text, int(figure.get_figwidth() * _CAPTION_CHARS_PER_INCH))
    height = figure.get_figheight()
    band = (len(lines) * _CAPTION_LINE_INCHES + _CAPTION_MARGIN_INCHES) / height
    figure.tight_layout(rect=(0.0, band, 1.0, 1.0))
    figure.text(
        0.5,
        _CAPTION_MARGIN_INCHES / height,
        "\n".join(lines),
        ha="center",
        va="bottom",
        fontsize=_CAPTION_SIZE,
        color=_CAPTION_COLOR,
    )


def _style_axes(axes: Axes, *, xlabel: str, ylabel: str, title: str = "") -> None:
    """One look for every panel: labelled, lightly gridded, unboxed."""
    axes.set_xlabel(xlabel, fontsize=_LABEL_SIZE)
    axes.set_ylabel(ylabel, fontsize=_LABEL_SIZE)
    if title:
        axes.set_title(title, fontsize=_TITLE_SIZE)
    axes.tick_params(labelsize=_TICK_SIZE)
    axes.grid(True, color=_GRID_COLOR, linewidth=0.6)
    axes.set_axisbelow(True)
    for side in ("top", "right"):
        axes.spines[side].set_visible(False)


# ---------------------------------------------------------------------------
# Figure 1: per field accuracy, family by quality tier
# ---------------------------------------------------------------------------

_HEATMAP_SIZE = (9.0, 4.4)
# Sequential grey: darker is more accurate, which survives a greyscale print
# because it never carried colour in the first place.
_HEATMAP_COLORMAP = "Greys"


def _field_accuracy_figure(scorecard: Scorecard) -> Figure:
    cells = {(cell.field_family, cell.quality_tier): cell for cell in scorecard.field_accuracy}
    families = sorted({family for family, _ in cells}, key=lambda item: _FAMILY_ORDER[item])
    tiers = sorted({tier for _, tier in cells}, key=lambda item: _TIER_ORDER[item])
    grid = np.full((len(families), len(tiers)), np.nan)
    for row, family in enumerate(families):
        for column, tier in enumerate(tiers):
            cell = cells.get((family, tier))
            accuracy = None if cell is None else cell_accuracy(cell)
            if accuracy is not None:
                grid[row, column] = accuracy

    figure = _figure(*_HEATMAP_SIZE)
    axes = figure.subplots()
    # Absent cells fall off the scale entirely; the hatch below is what shows them.
    colours = matplotlib.colormaps[_HEATMAP_COLORMAP].with_extremes(bad="white")
    image = axes.imshow(
        np.ma.masked_invalid(grid), cmap=colours, vmin=0.0, vmax=PERCENT, aspect="auto"
    )
    bar = figure.colorbar(image, ax=axes, pad=0.02)
    bar.set_label("canonical accuracy, percent of printed fields", fontsize=_LABEL_SIZE)
    bar.ax.tick_params(labelsize=_TICK_SIZE)

    axes.set_xticks(range(len(tiers)), [tier.value.replace("_", " ") for tier in tiers])
    axes.set_yticks(range(len(families)), [family.value for family in families])
    axes.tick_params(labelsize=_TICK_SIZE)
    axes.set_title("Per field accuracy by quality tier", fontsize=_TITLE_SIZE)
    axes.set_xlabel("quality tier", fontsize=_LABEL_SIZE)
    axes.set_ylabel("field family", fontsize=_LABEL_SIZE)
    for spine in axes.spines.values():
        spine.set_visible(False)

    absent = _annotate_heatmap(axes, cells, families, tiers)
    caption = (
        f"{scorecard_stamp(scorecard)} Each cell is the scorecard's own correct_canonical over"
        " fields_expected, so it counts only the fields the artifact actually printed."
    )
    if absent:
        caption += (
            " Hatched cells produced no reading at all, so they are absent rather than zero:"
            " that format has no extractor, which is not the same as extracting it wrongly."
        )
    _caption(figure, caption)
    return figure


def _annotate_heatmap(
    axes: Axes,
    cells: dict[tuple[FieldFamily, QualityTier], FieldAccuracyCell],
    families: Sequence[FieldFamily],
    tiers: Sequence[QualityTier],
) -> int:
    """Write each cell's rate over its counts; hatch the ones that have neither."""
    absent = 0
    for row, family in enumerate(families):
        for column, tier in enumerate(tiers):
            cell = cells.get((family, tier))
            accuracy = None if cell is None else cell_accuracy(cell)
            if cell is None:
                _absent_cell(axes, row, column, "no cell")
                absent += 1
            elif accuracy is None:
                _absent_cell(axes, row, column, f"not extracted\n0 of {cell.fields_expected}")
                absent += 1
            else:
                axes.text(
                    column,
                    row,
                    f"{accuracy:.1f}%\n{cell.correct_canonical} of {cell.fields_expected}",
                    ha="center",
                    va="center",
                    fontsize=_ANNOTATION_SIZE,
                    color="white" if accuracy > _DARK_TEXT_ABOVE else "black",
                )
    return absent


def _absent_cell(axes: Axes, row: int, column: int, label: str) -> None:
    """Hatch, never shade. An absent cell must not read anywhere on the accuracy scale."""
    axes.add_patch(
        Rectangle(
            (column - 0.5, row - 0.5),
            1.0,
            1.0,
            facecolor="white",
            edgecolor="0.55",
            hatch="////",
            linewidth=0.6,
        )
    )
    axes.text(
        column,
        row,
        label,
        ha="center",
        va="center",
        fontsize=_ANNOTATION_SIZE,
        color="0.25",
        # The hatch runs under the words, so they get their own ground to sit on.
        bbox={"facecolor": "white", "edgecolor": "none", "pad": 2.0},
    )


# ---------------------------------------------------------------------------
# Figure 2: reliability diagram
# ---------------------------------------------------------------------------

_RELIABILITY_SIZE = (7.0, 5.4)
# Both axes start at the lowest published value, rounded down to this step. The
# range stays square so the ideal line is still the 45 degree diagonal, and no
# published point is cropped; only empty space is.
_RELIABILITY_FLOOR_STEP = 0.05
_RELIABILITY_PAD = 0.02


def _reliability_figure(scorecard: Scorecard) -> Figure:
    by_family: defaultdict[FieldFamily, list[CalibrationBin]] = defaultdict(list)
    for one_bin in scorecard.calibration:
        by_family[one_bin.field_family].append(one_bin)
    plotted = [value for one_bin in scorecard.calibration for value in _bin_point(one_bin)]
    floor = _RELIABILITY_FLOOR_STEP * (min(plotted, default=0.0) // _RELIABILITY_FLOOR_STEP)
    low, high = floor - _RELIABILITY_PAD, 1.0 + _RELIABILITY_PAD

    figure = _figure(*_RELIABILITY_SIZE)
    axes = figure.subplots()
    axes.plot(
        [low, high],
        [low, high],
        color=_IDEAL_COLOR,
        linewidth=1.0,
        linestyle=(0, (2, 2)),
        label="ideal, confidence equals accuracy",
        zorder=1,
    )
    for family in sorted(by_family, key=lambda item: _FAMILY_ORDER[item]):
        bins = sorted(by_family[family], key=lambda one_bin: one_bin.mean_confidence)
        counted = sum(one_bin.count for one_bin in bins)
        axes.plot(
            [one_bin.mean_confidence for one_bin in bins],
            [one_bin.empirical_accuracy for one_bin in bins],
            marker=_FAMILY_MARKERS[family],
            markersize=4.5,
            linewidth=1.2,
            linestyle=_FAMILY_LINES[family],
            color=_FAMILY_GREYS[family],
            label=f"{family.value}, {counted} fields in {len(bins)} bins",
            zorder=2,
        )
    _style_axes(
        axes,
        xlabel="mean predicted confidence (fraction)",
        ylabel="empirical accuracy (fraction of fields correct)",
        title="Reliability of the confidence score",
    )
    axes.set_xlim(low, high)
    axes.set_ylim(low, high)
    axes.set_aspect("equal")
    axes.legend(fontsize=_TICK_SIZE, loc="upper left", frameon=False)
    _caption(
        figure,
        f"{scorecard_stamp(scorecard)} Equal count bins, least confident first, exactly as"
        " published in the scorecard's calibration section. A marker below the diagonal is a"
        " family claiming more confidence than it earns at that level. Both axes start at the"
        " lowest published value, so nothing is cropped and the diagonal stays at 45 degrees.",
    )
    return figure


def _bin_point(one_bin: CalibrationBin) -> tuple[float, float]:
    return one_bin.mean_confidence, one_bin.empirical_accuracy


# ---------------------------------------------------------------------------
# Figure 3: threshold sweep with the applied operating point marked
# ---------------------------------------------------------------------------

_SWEEP_SIZE = (9.0, 6.8)
_SWEEP_COLUMNS = 2
_SWEEP_Y_PAD = 2.0
_SWEEP_X_PAD = 4.0
# Label offsets in points: sideways clear of the marker, and one line up or down
# so the two labels of a pair never collide even when the markers coincide.
_NUDGE = 9.0
_LIFT = 9.0
# Past this much of the panel's width a label is written leftwards instead.
_LABEL_FLIP_FRACTION = 0.45


def _threshold_sweep_figure(scorecard: Scorecard) -> Figure:
    sweeps = family_sweeps(scorecard.threshold_sweep)
    rows = -(-len(sweeps) // _SWEEP_COLUMNS)
    figure = _figure(*_SWEEP_SIZE)
    panels = figure.subplots(rows, _SWEEP_COLUMNS, squeeze=False)
    for index in range(rows * _SWEEP_COLUMNS):
        panel = panels[index // _SWEEP_COLUMNS][index % _SWEEP_COLUMNS]
        if index < len(sweeps):
            _sweep_panel(panel, sweeps[index], scorecard)
        else:
            panel.set_visible(False)
    figure.suptitle(
        "Auto accept precision against review rate, with the applied operating point",
        fontsize=_TITLE_SIZE,
    )
    _caption(
        figure,
        f"{scorecard_stamp(scorecard)} The curve is the sweep on the calibration split, the"
        " only split a threshold may be chosen on. The filled marker is the point chosen"
        f" there; the open marker is what the held out {scorecard.split} split reached at the"
        " same threshold. The arrow between them is the generalization gap, drawn rather than"
        " described.",
    )
    return figure


def _sweep_panel(axes: Axes, sweep: FamilySweep, scorecard: Scorecard) -> None:
    curve = sorted(sweep.curve, key=lambda point: point.review_rate)
    reviews = [point.review_rate * PERCENT for point in curve]
    precisions = [point.auto_accept_precision * PERCENT for point in curve]
    axes.plot(
        reviews,
        precisions,
        color=_FAMILY_GREYS[sweep.field_family],
        linewidth=1.2,
        linestyle=_FAMILY_LINES[sweep.field_family],
        zorder=2,
    )
    applied = (sweep.applied.review_rate * PERCENT, sweep.applied.auto_accept_precision * PERCENT)
    achieved = (
        sweep.achieved.review_rate * PERCENT,
        sweep.achieved.auto_accept_precision * PERCENT,
    )
    axes.annotate(
        "",
        xy=achieved,
        xytext=applied,
        arrowprops={"arrowstyle": "->", "color": "0.25", "shrinkA": 6.0, "shrinkB": 6.0},
        zorder=3,
    )
    axes.plot(*applied, marker="o", markersize=7.0, color="black", linestyle="none", zorder=4)
    axes.plot(
        *achieved,
        marker="o",
        markersize=7.0,
        markerfacecolor="white",
        markeredgecolor="black",
        markeredgewidth=1.4,
        linestyle="none",
        zorder=4,
    )
    _style_axes(
        axes,
        xlabel="review rate, percent of fields sent to a human",
        ylabel="auto accept precision, percent",
        title=f"{sweep.field_family.value}, threshold {sweep.applied.threshold:.4f}",
    )
    axes.set_ylim(max(0.0, min([*precisions, applied[1], achieved[1]]) - _SWEEP_Y_PAD), PERCENT + 1)
    axes.set_xlim(-_SWEEP_X_PAD, max([*reviews, applied[0], achieved[0]]) + _SWEEP_X_PAD)
    # Limits first: which side a label goes on depends on where the point sits.
    _label_point(axes, f"calibration: {applied[1]:.2f}% at {applied[0]:.1f}%", applied, _LIFT)
    _label_point(
        axes, f"{scorecard.split}: {achieved[1]:.2f}% at {achieved[0]:.1f}%", achieved, -_LIFT
    )


def _label_point(axes: Axes, label: str, point: tuple[float, float], lift: float) -> None:
    """Label a marker on whichever side of it has room, so nothing runs off the panel."""
    low, high = axes.get_xlim()
    leftwards = (point[0] - low) / (high - low) > _LABEL_FLIP_FRACTION
    axes.annotate(
        label,
        xy=point,
        xytext=(-_NUDGE if leftwards else _NUDGE, lift),
        textcoords="offset points",
        ha="right" if leftwards else "left",
        va="center",
        fontsize=_ANNOTATION_SIZE,
        # The curve can run under a label, so the words get their own ground.
        bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.85, "pad": 1.5},
    )


# ---------------------------------------------------------------------------
# Figure 4: exception recall by type, with the dollar weighted overlay
# ---------------------------------------------------------------------------

_RECALL_SIZE = (9.0, 5.4)
_BAR_WIDTH = 0.34
_BAR_LABEL_LIFT = 2.0
# Headroom above the tallest possible bar, kept clear for the legend so it never
# sits over a bar. The ticks still stop at 100, which is where recall stops.
_RECALL_CEILING = 132.0
_RECALL_TICK_STEP = 20
# Below this height a bar is too short to hold its own label, so it goes above.
_INSIDE_LABEL_ABOVE = 12.0

# Oracle first: it is the ceiling the end to end run is read against.
_MODE_DRAW_ORDER: tuple[ReconMode, ...] = (ReconMode.ORACLE, ReconMode.END_TO_END)


def _exception_recall_figure(sources: dict[ReconMode, Scorecard]) -> Figure:
    modes = [mode for mode in _MODE_DRAW_ORDER if mode in sources]
    cells: dict[tuple[ReconMode, ExceptionType], ReconCell] = {
        (cell.mode, cell.exception_type): cell
        for mode in modes
        for cell in sources[mode].reconciliation
        if cell.mode is mode
    }
    kinds = sorted({kind for _, kind in cells}, key=lambda item: _TYPE_ORDER[item])
    positions = np.arange(len(kinds), dtype=float)

    figure = _figure(*_RECALL_SIZE)
    axes = figure.subplots()
    for mode, offset in zip(modes, _bar_offsets(len(modes)), strict=True):
        centres = positions + offset
        heights = [_recall(cells.get((mode, kind))) for kind in kinds]
        axes.bar(
            centres,
            [0.0 if height is None else height for height in heights],
            width=_BAR_WIDTH,
            color=_MODE_FILL[mode],
            edgecolor="black",
            linewidth=0.6,
            hatch=_MODE_HATCH[mode],
            zorder=2,
        )
        for centre, kind, height in zip(centres, kinds, heights, strict=True):
            cell = cells.get((mode, kind))
            if cell is None or height is None:
                continue
            _bar_label(axes, centre, height, f"{cell.detected_true}/{cell.injected}", mode)
            dollars = _dollar_recall(cell)
            if dollars is not None:
                axes.hlines(
                    dollars,
                    centre - _BAR_WIDTH / 2,
                    centre + _BAR_WIDTH / 2,
                    color="black",
                    linewidth=1.8,
                    linestyles=_MODE_LINES[mode],
                    zorder=4,
                )
    _style_axes(
        axes,
        xlabel="exception type",
        ylabel="recall, percent of injected discrepancies caught",
        title="Exception recall by type, counts with a dollar weighted overlay",
    )
    axes.set_xticks(positions, [kind.value.replace("_", "\n") for kind in kinds])
    axes.set_ylim(0.0, _RECALL_CEILING)
    axes.set_yticks(range(0, int(PERCENT) + 1, _RECALL_TICK_STEP))
    axes.legend(
        handles=_recall_legend(modes),
        fontsize=_TICK_SIZE,
        loc="upper left",
        ncols=len(modes),
        frameon=False,
    )
    _caption(figure, _recall_caption(sources, modes, cells, kinds))
    return figure


def _bar_label(axes: Axes, centre: float, height: float, label: str, mode: ReconMode) -> None:
    """Inside the bar, so a dollar rule drawn just above it never lands on the words."""
    if height < _INSIDE_LABEL_ABOVE:
        axes.text(
            centre,
            height + _BAR_LABEL_LIFT,
            label,
            ha="center",
            va="bottom",
            fontsize=_ANNOTATION_SIZE,
            zorder=5,
        )
        return
    axes.text(
        centre,
        height - _BAR_LABEL_LIFT,
        label,
        ha="center",
        va="top",
        fontsize=_ANNOTATION_SIZE,
        color="white" if float(_MODE_FILL[mode]) < 0.5 else "black",
        zorder=5,
    )


def _bar_offsets(count: int) -> list[float]:
    return [(index - (count - 1) / 2) * _BAR_WIDTH for index in range(count)]


def _recall(cell: ReconCell | None) -> float | None:
    return None if cell is None else _percent(cell.detected_true, cell.injected)


def _dollar_recall(cell: ReconCell) -> float | None:
    return _percent(cell.caught_dollar_cents, cell.injected_dollar_cents)


def _recall_legend(modes: Sequence[ReconMode]) -> list[Line2D]:
    bars = [
        Line2D(
            [],
            [],
            marker="s",
            markersize=8,
            markerfacecolor=_MODE_FILL[mode],
            markeredgecolor="black",
            linestyle="none",
            label=f"{_MODE_LABELS[mode]}: count recall",
        )
        for mode in modes
    ]
    rules = [
        Line2D(
            [],
            [],
            color="black",
            linewidth=1.8,
            linestyle=_MODE_LINES[mode],
            label=f"{_MODE_LABELS[mode]}: dollar weighted recall",
        )
        for mode in modes
    ]
    return bars + rules


def _recall_caption(
    sources: dict[ReconMode, Scorecard],
    modes: Sequence[ReconMode],
    cells: dict[tuple[ReconMode, ExceptionType], ReconCell],
    kinds: Sequence[ExceptionType],
) -> str:
    parts = [
        " ".join(f"{mode.value}: {scorecard_stamp(sources[mode])}" for mode in modes),
        "Oracle feeds the engine truth lines, end to end feeds it extracted lines, so the"
        " distance between the two is the error extraction adds to matching. Bars are"
        " detected_true over injected; each rule is caught dollars over injected dollars."
        " Both are read from the scorecards named above.",
    ]
    absent = sorted(
        {kind.value for mode in modes for kind in kinds if _absent_dollars(cells, mode, kind)}
    )
    if absent:
        parts.append(
            f"No rule is drawn for {', '.join(absent)}: no dollars were injected there, so a"
            " dollar weighted recall is absent rather than zero."
        )
    false_positives = [
        f"{mode.value} {kind.value} {cells[mode, kind].detected_false}"
        for mode in modes
        for kind in kinds
        if (mode, kind) in cells and cells[mode, kind].detected_false
    ]
    if false_positives:
        parts.append(f"False detections, which recall does not show: {'; '.join(false_positives)}.")
    return " ".join(parts)


def _absent_dollars(
    cells: dict[tuple[ReconMode, ExceptionType], ReconCell],
    mode: ReconMode,
    kind: ExceptionType,
) -> bool:
    cell = cells.get((mode, kind))
    return cell is not None and _dollar_recall(cell) is None
