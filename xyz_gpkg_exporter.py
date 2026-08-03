"""
xyz_gpkg_exporter.py

Core exporter that renders the currently loaded QGIS project into a single
GeoPackage of raster XYZ tiles (EPSG:4326).

Target platform: QGIS 4.0+ / Qt6 / PyQt6.


THREADING MODEL
===============

Three clearly separated stages, and one hard rule: *nothing* that touches
the GUI, `iface`, the map canvas or live `QgsProject` state ever runs off
the main thread.

  1. `ProjectRenderSnapshot.capture()` - MAIN THREAD ONLY.
     Resolves the active map theme, the visible layer set, the style
     overrides, the labeling engine settings and the expression context,
     and normalises the project's scale-calculation entry. All of this
     reads or mutates live project/canvas state, so it must happen on the
     GUI thread. It is O(1) and returns a plain value object that is then
     safe to hand to background threads.

     The previous implementation did this work inside `__init__`, which
     the Processing algorithm called from its *worker* thread. In
     particular `QgsProject.setLabelingEngineSettings()` emits
     `labelingEngineSettingsChanged`, which kicks the map canvas into a
     repaint on the main thread at the exact moment worker threads are
     cloning those same layers. That is a real hang vector and is now
     structurally impossible.

  2. `XyzGpkgExporter.export()` - BACKGROUND THREAD.
     Builds the tile matrices and the occupancy index, then acts as the
     controller: it feeds compact tile *runs* into a bounded queue, polls
     cancellation, and emits throttled progress. It never blocks for
     longer than the progress tick interval, so cancellation is always
     acknowledged within ~100 ms.

  3. Render workers + one writer thread.
     N persistent render workers pull runs off the work queue, render and
     encode, and push byte-budgeted batches onto a bounded writer queue.
     A single dedicated writer thread owns the SQLite connection outright.
     Renderers therefore never take a database lock, and back-pressure on
     the two bounded queues caps peak memory regardless of job size.


WHY THE GUI USED TO FREEZE
==========================

Rendering was already off the GUI thread. The freeze came from flooding
the main thread's event queue, not from blocking it:

  * `progress_callback` was invoked once per tile and wired straight to
    `QgsProcessingFeedback.setProgress()`, which emits a queued
    cross-thread signal. Millions of tiles means millions of queued
    events arriving faster than the event loop can drain them, so the UI
    is permanently backlogged and Windows reports "not responding".
    Progress is now emitted on a wall-clock interval (default 100 ms),
    turning millions of emissions into a few hundred over a whole run.

  * Every log record was mirrored into BOTH `QgsMessageLog` (Log Messages
    panel) and the Processing dialog's log widget, including one message
    per database batch flush and one per failed tile. Appending unbounded
    text to a `QTextEdit` from a queued signal is superlinear in repaint
    cost. Per-tile and per-batch logging is gone; failures are counted,
    sampled and summarised.


CORRECTNESS GUARANTEES (unchanged from the previous implementation)
===================================================================

  * All tile bounds come exclusively from `QgsTileMatrix` - never from
    independent zoom/row/col arithmetic - so tiles are gapless and
    non-overlapping by construction.
  * Per-tile render scale uses an explicit equator-based formula so that
    rule-based symbology, scale-dependent labels and scale-dependent
    layer visibility evaluate identically to the live canvas, regardless
    of the project's scale-calculation setting and of the latitude band a
    tile covers.
  * `QgsLabelingEngineSettings` is built once, on the main thread, with
    `UsePartialCandidates` forced off. Building label-engine/font objects
    on a worker thread is not safe.
  * Each worker renders against its own cloned layers; providers are not
    safe to drive concurrently through one `QgsMapLayer` instance. All
    clone construction is serialised behind a single lock at worker
    start-up, which removes the concurrent-first-open race against a
    shared physical `.gpkg` without costing anything steady-state.
  * The GeoPackage is written without WAL, so tile data can never be
    stranded in a `.gpkg-wal` side file.

METATILING
==========

Tiles are rendered in aligned square blocks of `metatile_size_px / TILE_SIZE`
tiles (default 2048 px = 8x8 = 64 tiles) in a single
`QgsMapRendererCustomPainterJob`, then sliced apart. See the METATILING block
in the constants section for the full rationale. In short: one renderer setup,
one label-engine run and one transform-cache pass per block instead of per
tile; each feature drawn once per block instead of once per tile it overlaps;
and a screen-sized label viewport instead of a 68 mm one.

The scale denominator is unaffected - a block covers N times the ground width
at N times the pixel width - so symbology and every scale-dependent rule
evaluate exactly as they do per-tile. Only label placement changes, and it
changes toward what the map canvas shows.

Set `metatile_size_px = TILE_SIZE` to disable metatiling and restore
one-job-per-tile rendering.


OUTPUT CHANGES RELATIVE TO THE ORIGINAL IMPLEMENTATION
======================================================

Three, all deliberate:

1. **Zoom-0 scale baseline corrected.** `QgsTileMatrix.fromCustomDef` takes
   `z0Dimension` as the side length of one zoom-0 *tile*, not of the whole
   matrix, so the zoom-0 tile span is 180 degrees, not 180/2 = 90. The old
   value made every `map_scale` exactly half the true denominator, i.e. one
   zoom level more "zoomed in" than reality, for every scale-dependent
   symbology rule, label threshold and layer visibility range. The writer's
   independent gpkg_tile_matrix maths corroborates 180 degrees per zoom-0
   tile. Set `SCALE_DENOMINATOR_FACTOR = 0.5` to restore the old behaviour.

2. **Label placement**, via metatiling, as described above.

3. **Map theme style overrides now apply at all.**
   `QgsMapThemeCollection.mapThemeStyleOverrides()` is keyed by layer ID, but
the renderer feeds *cloned* layers, which have fresh IDs. The overrides
therefore never matched and were silently ignored. They are now remapped
onto each thread's clone IDs, so theme-based exports finally render the
way the canvas does. This only affects projects that use a map theme with
style overrides; everything else is byte-for-byte unaffected.
"""

from __future__ import annotations

import ctypes
import logging
import math
import os
import platform
import queue
import sqlite3
import threading
import time
import traceback
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsExpressionContext,
    QgsExpressionContextScope,
    QgsExpressionContextUtils,
    QgsFeatureRequest,
    QgsLabelingEngineSettings,
    QgsMapLayer,
    QgsMapRendererCustomPainterJob,
    QgsMapSettings,
    QgsMessageLog,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsTileMatrix,
    QgsTileXYZ,
    QgsVectorLayer,
)

try:  # pragma: no cover - absent only outside a QGIS runtime
    from qgis.core import QgsApplication, QgsTask
except ImportError:  # pragma: no cover
    QgsApplication = None
    QgsTask = None

from qgis.PyQt.QtCore import QBuffer, QIODevice, QSize, QThread
from qgis.PyQt.QtGui import QColor, QImage, QPainter


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

LOG_TAG = "XyzGpkgExporter"

EARTH_CIRCUMFERENCE_M = 40075016.6856
METERS_PER_DEGREE_EQUATOR = EARTH_CIRCUMFERENCE_M / 360.0
INCH_METERS = 0.0254

QIMAGE_NATIVE_FORMATS = frozenset({"PNG", "JPEG", "JPG", "WEBP"})


# ==========================================================================
# TILING SCHEME
# ==========================================================================
# Everything about the tile pyramid is defined here and nowhere else. Change
# these five values and the tile matrices, the GeoPackage
# gpkg_tile_matrix/gpkg_tile_matrix_set rows, the per-zoom scale denominators
# and the occupancy quadtree fold all follow automatically.
#
# The defaults describe GoogleCRS84Quad: origin at the north-west corner of
# the globe, a 2 x 1 root matrix, and one zoom-0 tile spanning 180 degrees
# square. That gives a full-globe extent of -180,-90 : 180,90 at every zoom
# with perfectly square tiles.
#
# IMPORTANT - the meaning of TILE_MATRIX_Z0_TILE_SPAN:
#   QgsTileMatrix.fromCustomDef(zoom, crs, z0TopLeftPoint, z0Dimension,
#                               z0MatrixWidth, z0MatrixHeight)
#   takes z0Dimension as the side length of ONE zoom-0 TILE - not the width
#   of the whole zoom-0 matrix. The matrix extent it produces is
#   z0MatrixWidth * z0Dimension wide by z0MatrixHeight * z0Dimension tall.
#   With the defaults below that is 2*180 = 360 degrees by 1*180 = 180
#   degrees, i.e. the whole globe, and each zoom-0 tile is 180 degrees square.
#
#   This is why ZOOM0_TILE_SPAN_DEGREES below is TILE_MATRIX_Z0_TILE_SPAN
#   itself and is NOT divided by TILE_MATRIX_ROOT_WIDTH. Earlier versions of
#   this module divided by the root width, producing a zoom-0 tile span of 90
#   degrees instead of 180 and therefore a `map_scale` denominator exactly
#   half the true value at every zoom level - reporting one zoom level more
#   "zoomed in" than reality to every scale-dependent symbology rule, label
#   threshold and layer visibility range.
#
#   The writer's independent gpkg_tile_matrix maths corroborates 180: at zoom
#   0 pixel_x_size = 360/2/256 = 0.703125 degrees per pixel, and
#   0.703125 * 256 = 180 degrees per tile.
#
#   If you calibrated symbology rules by eye against the old, halved value,
#   set SCALE_DENOMINATOR_FACTOR to 0.5 to reproduce it exactly.

TILE_SIZE = 256                          # tile edge in pixels
TILE_MATRIX_TILE_ORIGIN = QgsPointXY(-180.0, 90.0)   # north-west corner
TILE_MATRIX_Z0_TILE_SPAN = 180.0         # side length of ONE zoom-0 tile, CRS units
TILE_MATRIX_ROOT_WIDTH = 2               # zoom-0 matrix columns
TILE_MATRIX_ROOT_HEIGHT = 1              # zoom-0 matrix rows  (2:1 = GoogleCRS84Quad)

# Ground span of a single zoom-0 tile; every deeper zoom is this halved once
# per level, because the matrix is a strict quadtree.
ZOOM0_TILE_SPAN_DEGREES = TILE_MATRIX_Z0_TILE_SPAN

# Multiplier applied to every computed scale denominator. Leave at 1.0 for
# geometrically correct scales; set to 0.5 to reproduce the pre-fix values.
SCALE_DENOMINATOR_FACTOR = 1.0

# Tiles must be square for metatile slicing and for QgsMapSettings to accept
# an extent without aspect-ratio adjustment. Fail loudly rather than produce
# subtly misaligned tiles.
if TILE_MATRIX_ROOT_WIDTH < 1 or TILE_MATRIX_ROOT_HEIGHT < 1:
    raise ValueError("TILE_MATRIX_ROOT_WIDTH/HEIGHT must both be >= 1")

# ==========================================================================


# --------------------------------------------------------------------------
# Metatiling
# --------------------------------------------------------------------------
# Tiles are rendered in aligned square blocks ("metatiles") of
# METATILE_SIZE_PX / TILE_SIZE tiles per side, in ONE
# QgsMapRendererCustomPainterJob, then sliced into individual tiles.
#
# Why this is both faster and more faithful:
#   * One renderer setup, one label-engine run, one symbol-preparation pass
#     and one set of coordinate-transform cache lookups per block instead of
#     per tile. Those global QGIS/Qt locks are what starve the GUI thread, so
#     touching them 64x less often is the single most effective
#     responsiveness fix available.
#   * A feature spanning several tiles is drawn ONCE per block rather than
#     once per tile it overlaps. Per-tile rendering redraws it every time.
#   * The label engine sees a screen-sized viewport instead of a 256 px one.
#     With UsePartialCandidates off, a label is only placed if it fits
#     entirely inside the render viewport, so a 256 px tile (about 68 mm at
#     96 dpi) drops nearly every label and positions the survivors against a
#     viewport nothing like the real map canvas. This is the main reason
#     per-tile exports look unlike the project on screen.
#
# The scale denominator is NOT affected. A block covers N times the ground
# width at N times the pixel width, so ground units per pixel - and therefore
# the scale denominator - is identical to a single tile at the same zoom.
# Symbol sizes, line widths and every scale-dependent rule evaluate exactly
# as they do without metatiling. Only label placement changes.
#
# Interaction with the occupancy index: a block containing no occupied tile
# is never rendered, and slicing emits ONLY the occupied sub-tiles, so the
# set of rows written to the GeoPackage is unchanged. Sparsely occupied
# blocks do rasterise some empty area, but empty-area fill is cheap next to
# the per-tile setup and duplicated feature drawing it removes.
#
# Set the metatile size equal to TILE_SIZE to disable metatiling entirely and
# get the previous one-job-per-tile behaviour.

DEFAULT_METATILE_SIZE_PX = 2048
MIN_METATILE_SIZE_PX = TILE_SIZE
# 4096 px ARGB32 is 67 MB per worker thread; beyond that memory dominates.
MAX_METATILE_SIZE_PX = 4096

WORLD_EXTENT = QgsRectangle(-180.0, -90.0, 180.0, 90.0)

# Immutable value objects reused for every render. Qt setters copy these, so
# sharing one instance across threads is safe.
_TILE_QSIZE = QSize(TILE_SIZE, TILE_SIZE)
_TRANSPARENT = QColor(0, 0, 0, 0)

# Tuning. These bound memory and amortise per-item overhead; none of them
# affect the bytes written.
WRITE_BATCH_TILES = 512         # flush a worker's pending rows at this count
WRITE_BATCH_BYTES = 4 << 20     # ...or this many encoded bytes, whichever first
WRITE_QUEUE_DEPTH = 8           # bounded writer back-pressure
PROGRESS_INTERVAL_S = 0.1       # throttle for cross-thread progress emission
MAX_REPORTED_TILE_ERRORS = 20   # sample cap; the rest are counted only


# --------------------------------------------------------------------------
# Qt / QGIS enum compatibility shims (QGIS 3.x -> 4.x moved several enums)
# --------------------------------------------------------------------------

def _first_attr(*candidates):
    """Return the first candidate that resolves without raising."""
    for factory in candidates:
        try:
            value = factory()
        except (AttributeError, TypeError):
            continue
        if value is not None:
            return value
    return None


_LABEL_FLAG_PARTIAL_CANDIDATES = _first_attr(
    lambda: Qgis.LabelingFlag.UsePartialCandidates,
    lambda: QgsLabelingEngineSettings.Flag.UsePartialCandidates,
    lambda: QgsLabelingEngineSettings.UsePartialCandidates,
)

_MS_FLAG_ANTIALIASING = _first_attr(
    lambda: Qgis.MapSettingsFlag.Antialiasing,
    lambda: QgsMapSettings.Flag.Antialiasing,
)
_MS_FLAG_RENDER_MAP_TILE = _first_attr(
    lambda: Qgis.MapSettingsFlag.RenderMapTile,
    lambda: QgsMapSettings.Flag.RenderMapTile,
)
_MS_FLAG_ADVANCED_EFFECTS = _first_attr(
    lambda: Qgis.MapSettingsFlag.UseAdvancedEffects,
    lambda: QgsMapSettings.Flag.UseAdvancedEffects,
)


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

class _QgsMessageLogHandler(logging.Handler):
    """Routes `logging` records into the QGIS Log Messages panel.

    Kept deliberately low-volume: the exporter no longer logs per tile or
    per database batch, because every record here becomes a queued
    cross-thread signal that appends to a GUI text widget.
    """

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.ERROR:
            level = Qgis.MessageLevel.Critical
        elif record.levelno >= logging.WARNING:
            level = Qgis.MessageLevel.Warning
        else:
            level = Qgis.MessageLevel.Info
        try:
            QgsMessageLog.logMessage(self.format(record), LOG_TAG, level)
        except Exception:  # pragma: no cover - never let logging kill a render
            pass


def make_logger() -> logging.Logger:
    logger = logging.getLogger(LOG_TAG)
    if not any(isinstance(h, _QgsMessageLogHandler) for h in logger.handlers):
        handler = _QgsMessageLogHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def compute_worker_count(max_cpu_percent: int) -> int:
    """Render worker count from a CPU percentage, always leaving the GUI a core.

    The percentage is applied to `cores - 1`, and the result can never exceed
    `cores - 1`. This is a deliberate behaviour change from the previous
    implementation, which returned `floor(cores * pct/100)` and therefore
    handed out every core on the machine at 100%.

    That was a primary cause of the "QGIS is not responding" freeze. Tile
    rendering is CPU-bound C++ that holds the CPU continuously; if every core
    is occupied by a render worker, the OS has nowhere to schedule the main
    thread, so the GUI cannot repaint no matter how little work it has to do
    and no matter how few signals it is sent. Reserving one core is what
    makes the difference between "usable" and "frozen"; it costs at most
    `1/cores` of throughput.
    """
    pct = max(1, min(100, int(max_cpu_percent)))
    cores = os.cpu_count() or 1
    budget = max(1, cores - 1)
    return max(1, min(budget, round(budget * (pct / 100.0))))


def lower_current_thread_priority() -> None:
    """Best-effort de-prioritisation of the calling thread at OS level.

    `QThread.start(LowPriority)` is the portable request, but Qt documents
    that thread priorities are **ignored on Linux** under the default
    scheduler, so the portable path alone is not enough on the platform where
    most heavy QGIS processing runs.

    Lowering priority matters because the render workers spend nearly all
    their time inside process-global QGIS/Qt locks - the `QFontDatabase`
    mutex taken for every label layout, the `QgsCoordinateTransform` cache
    lock taken per layer per tile, the SVG and image caches. The main thread
    needs those same locks to draw so much as a menu label. When the workers
    are de-prioritised, the scheduler preempts them in favour of the GUI
    thread, so those locks are handed over promptly instead of being
    reacquired immediately by whichever worker was already running.

    Every step is best-effort: a failure here degrades responsiveness, never
    correctness, so nothing is raised.
    """
    system = platform.system()
    try:
        if system == "Windows":
            THREAD_PRIORITY_BELOW_NORMAL = -1
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            kernel32.SetThreadPriority(
                kernel32.GetCurrentThread(), THREAD_PRIORITY_BELOW_NORMAL
            )
        elif system == "Linux":
            # On Linux `setpriority(PRIO_PROCESS, tid, n)` is per-thread when
            # given a kernel thread id, which is what makes this work at all;
            # `os.nice()` would apply to the whole process, GUI thread
            # included, and make matters worse.
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            SYS_GETTID = 186 if platform.machine() in ("x86_64", "AMD64") else 178
            tid = libc.syscall(SYS_GETTID)
            if tid > 0:
                os.setpriority(os.PRIO_PROCESS, tid, 5)
    except Exception:
        pass


def normalise_metatile_size(metatile_size_px: int) -> int:
    """Clamp a requested metatile size to a whole number of tiles.

    The block must be an exact integer multiple of TILE_SIZE, otherwise
    slicing would not land on tile boundaries. Values are rounded DOWN to the
    nearest multiple so a request never silently increases memory use, and
    clamped to [MIN_METATILE_SIZE_PX, MAX_METATILE_SIZE_PX].

    Passing TILE_SIZE yields a one-tile block, i.e. metatiling disabled.
    """
    try:
        requested = int(metatile_size_px)
    except (TypeError, ValueError):
        requested = DEFAULT_METATILE_SIZE_PX
    requested = max(MIN_METATILE_SIZE_PX, min(MAX_METATILE_SIZE_PX, requested))
    tiles = max(1, requested // TILE_SIZE)
    return tiles * TILE_SIZE


def equatorial_scale_denominator(extent_width_degrees: float, paper_width_m: float) -> float:
    """Equator-based scale denominator.

    Computed independently of the project's scale-calculation method so that
    symbology, label and layer-visibility thresholds evaluate identically for
    every tile regardless of latitude.
    """
    if paper_width_m <= 0.0:
        return 0.0
    return (extent_width_degrees * METERS_PER_DEGREE_EQUATOR) / paper_width_m


def _merge_intervals(intervals: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """Sort and coalesce inclusive integer intervals, joining adjacent ones.

    Adjacency (`start <= end + 1`) is merged as well as overlap, because these
    are contiguous tile indices: [3,5] and [6,9] describe exactly [3,9].
    """
    if len(intervals) < 2:
        return intervals
    intervals.sort()
    merged: List[Tuple[int, int]] = []
    cur_start, cur_end = intervals[0]
    for start, end in intervals:
        if start <= cur_end + 1:
            if end > cur_end:
                cur_end = end
        else:
            merged.append((cur_start, cur_end))
            cur_start, cur_end = start, end
    merged.append((cur_start, cur_end))
    return merged


# --------------------------------------------------------------------------
# Main-thread project snapshot
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ProjectRenderSnapshot:
    """Immutable capture of everything the renderer needs from the project.

    MUST be built on the main/GUI thread via :meth:`capture`. Once built it
    contains only value objects and layer references, and is safe to read
    from any number of worker threads.
    """

    project: QgsProject
    dest_crs: QgsCoordinateReferenceSystem
    extent: QgsRectangle
    layers: Tuple[QgsMapLayer, ...]
    style_overrides: Dict[str, str]
    labeling_settings: QgsLabelingEngineSettings
    expression_context: QgsExpressionContext
    theme_name: Optional[str] = None

    @classmethod
    def capture(
        cls,
        extent: Optional[QgsRectangle] = None,
        project: Optional[QgsProject] = None,
        dest_crs: Optional[QgsCoordinateReferenceSystem] = None,
    ) -> "ProjectRenderSnapshot":
        project = project or QgsProject.instance()
        dest_crs = dest_crs or QgsCoordinateReferenceSystem("EPSG:4326")

        if extent is None:
            extent = cls._canvas_extent(project, dest_crs)
        if extent is None or extent.isNull() or extent.isEmpty():
            extent = QgsRectangle(WORLD_EXTENT)

        # Normalise the legacy scale-calculation entry so any residual
        # canvas-side scale computation agrees with the explicit equator
        # scale the renderer applies per tile. Writing project entries is a
        # main-thread operation, which is exactly where we are.
        project.writeEntry("Measure", "/ScaleCalcMethod", "AtEquator")

        # Built once, here, from the project's own settings. Constructing or
        # mutating labeling-engine settings resolves fonts and text formats
        # and is not safe off the main thread.
        labeling_settings = project.labelingEngineSettings()
        if _LABEL_FLAG_PARTIAL_CANDIDATES is not None:
            labeling_settings.setFlag(_LABEL_FLAG_PARTIAL_CANDIDATES, False)
        project.setLabelingEngineSettings(labeling_settings)

        theme_name, layers, style_overrides = cls._resolve_layers(project)

        expr_context = QgsExpressionContext()
        expr_context.appendScope(QgsExpressionContextUtils.globalScope())
        expr_context.appendScope(QgsExpressionContextUtils.projectScope(project))

        return cls(
            project=project,
            dest_crs=dest_crs,
            extent=QgsRectangle(extent),
            layers=tuple(layers),
            style_overrides=dict(style_overrides),
            labeling_settings=labeling_settings,
            expression_context=expr_context,
            theme_name=theme_name,
        )

    @staticmethod
    def _canvas_extent(
        project: QgsProject, dest_crs: QgsCoordinateReferenceSystem
    ) -> Optional[QgsRectangle]:
        try:
            from qgis.utils import iface
        except Exception:
            return None
        if iface is None:
            return None
        try:
            canvas = iface.mapCanvas()
            if canvas is None:
                return None
            extent = canvas.extent()
            canvas_crs = canvas.mapSettings().destinationCrs()
            if canvas_crs != dest_crs:
                transform = QgsCoordinateTransform(canvas_crs, dest_crs, project.transformContext())
                extent = transform.transformBoundingBox(extent)
            return extent
        except Exception:
            return None

    @staticmethod
    def _resolve_layers(project: QgsProject):
        themes = project.mapThemeCollection()
        theme_name: Optional[str] = None
        try:
            from qgis.utils import iface

            if iface is not None:
                canvas = iface.mapCanvas()
                candidate = canvas.theme() if canvas is not None else ""
                if candidate and themes.hasMapTheme(candidate):
                    theme_name = candidate
        except Exception:
            theme_name = None

        if theme_name:
            return (
                theme_name,
                themes.mapThemeVisibleLayers(theme_name),
                themes.mapThemeStyleOverrides(theme_name),
            )
        return None, project.layerTreeRoot().checkedLayers(), {}


# --------------------------------------------------------------------------
# Tile matrices
# --------------------------------------------------------------------------

class TileMatrixSet:
    """One `QgsTileMatrix` per zoom level, the single source of truth for
    every tile bound in this module.

    Matrices and their dimensions live in flat lists indexed by
    `zoom - min_zoom`; the zoom range is always contiguous, so this is a
    direct array access rather than a dict hash on the hot path.
    """

    __slots__ = ("_min_zoom", "_max_zoom", "_crs", "_matrices", "_dims", "_extents")

    def __init__(self, crs: QgsCoordinateReferenceSystem, min_zoom: int, max_zoom: int):
        self._crs = crs
        self._min_zoom = min_zoom
        self._max_zoom = max_zoom
        self._matrices: List[QgsTileMatrix] = [
            QgsTileMatrix.fromCustomDef(
                zoom,
                crs,
                TILE_MATRIX_TILE_ORIGIN,
                TILE_MATRIX_Z0_TILE_SPAN,
                TILE_MATRIX_ROOT_WIDTH,
                TILE_MATRIX_ROOT_HEIGHT,
            )
            for zoom in range(min_zoom, max_zoom + 1)
        ]
        # Invariant once built; cache to avoid repeated sip calls.
        self._dims: List[Tuple[int, int]] = [
            (m.matrixWidth(), m.matrixHeight()) for m in self._matrices
        ]
        self._extents: List[QgsRectangle] = [m.extent() for m in self._matrices]

    @property
    def min_zoom(self) -> int:
        return self._min_zoom

    @property
    def max_zoom(self) -> int:
        return self._max_zoom

    def matrix(self, zoom: int) -> QgsTileMatrix:
        return self._matrices[zoom - self._min_zoom]

    def dims(self, zoom: int) -> Tuple[int, int]:
        return self._dims[zoom - self._min_zoom]

    def extent(self, zoom: int) -> QgsRectangle:
        return self._extents[zoom - self._min_zoom]

    def tile_extent(self, zoom: int, col: int, row: int) -> QgsRectangle:
        return self._matrices[zoom - self._min_zoom].tileExtent(QgsTileXYZ(col, row, zoom))


# --------------------------------------------------------------------------
# Occupancy
# --------------------------------------------------------------------------

class _RowIntervals:
    """Occupied columns per row for a single zoom level, as merged intervals.

    This replaces the previous `set` of `(col, row)` tuples, which was the
    hard scaling wall of the old implementation: a Python tuple plus its set
    slot costs roughly 100-120 bytes, so a 50-million-tile job needed several
    gigabytes of occupancy state - held for every zoom simultaneously -
    before a single tile was rendered. Real-world occupancy is strongly
    run-structured (features are contiguous in space), so interval storage is
    typically three to four orders of magnitude smaller, and it folds to
    coarser zooms exactly.
    """

    __slots__ = ("_rows", "_dirty", "_tile_count")

    def __init__(self) -> None:
        self._rows: Dict[int, List[Tuple[int, int]]] = defaultdict(list)
        self._dirty: set = set()
        self._tile_count: int = -1

    def add_block(self, col_start: int, col_end: int, row_start: int, row_end: int) -> None:
        """Mark an inclusive rectangular block of tiles as occupied.

        Cost is O(row count), not O(row count * column count): the old code
        inserted every individual tile of every feature's padded bounding
        box into a set, re-inserting the same tiles once per overlapping
        feature.
        """
        rows = self._rows
        dirty = self._dirty
        span = (col_start, col_end)
        for row in range(row_start, row_end + 1):
            bucket = rows[row]
            bucket.append(span)
            # Opportunistic compaction keeps per-row lists from growing
            # unboundedly on dense layers, without paying a sort per append.
            if len(bucket) >= 64:
                rows[row] = _merge_intervals(bucket)
            else:
                dirty.add(row)

    def compact(self) -> None:
        rows = self._rows
        for row in self._dirty:
            rows[row] = _merge_intervals(rows[row])
        self._dirty.clear()
        self._tile_count = -1

    def is_empty(self) -> bool:
        return not self._rows

    @property
    def tile_count(self) -> int:
        if self._tile_count < 0:
            self._tile_count = sum(
                (end - start + 1) for spans in self._rows.values() for start, end in spans
            )
        return self._tile_count

    def parent(self) -> "_RowIntervals":
        """Exact quadtree fold to the next coarser zoom.

        Each zoom's matrix is exactly double the previous one in both
        directions, so a coarse tile is occupied if and only if at least one
        of its four children is. Halving both interval endpoints is therefore
        exact, not an approximation, and costs O(interval count).
        """
        out = _RowIntervals()
        out_rows = out._rows
        for row, spans in self._rows.items():
            bucket = out_rows[row >> 1]
            for start, end in spans:
                bucket.append((start >> 1, end >> 1))
        for row in list(out_rows):
            out_rows[row] = _merge_intervals(out_rows[row])
        return out

    def iter_blocks(self, zoom: int, block: int) -> Iterator[Tuple]:
        """Yield aligned metatile blocks that contain at least one occupied tile.

        Each item is `(zoom, block_row, block_col, members, tile_count)` where
        `members` is a tuple of `(row, col_start, col_end)` runs clipped to the
        block. Only these runs are ever encoded and written, so metatiling
        never adds tiles that the occupancy index excluded - the set of rows in
        the GeoPackage is identical with and without metatiling.

        Blocks with no occupied tile are not emitted at all, which is what
        preserves the occupancy index's guarantee that runtime tracks real
        content rather than extent size.
        """
        groups: Dict[int, List[Tuple[int, List[Tuple[int, int]]]]] = defaultdict(list)
        for row, spans in self._rows.items():
            groups[row // block].append((row, spans))

        for block_row in sorted(groups):
            members_by_row = groups[block_row]
            block_cols: set = set()
            for _row, spans in members_by_row:
                for start, end in spans:
                    block_cols.update(range(start // block, end // block + 1))

            for block_col in sorted(block_cols):
                low = block_col * block
                high = low + block - 1
                members: List[Tuple[int, int, int]] = []
                count = 0
                for row, spans in members_by_row:
                    for start, end in spans:
                        clipped_start = start if start > low else low
                        clipped_end = end if end < high else high
                        if clipped_start <= clipped_end:
                            members.append((row, clipped_start, clipped_end))
                            count += clipped_end - clipped_start + 1
                if members:
                    members.sort()
                    yield (zoom, block_row, block_col, tuple(members), count)

    def iter_runs(self, zoom: int, max_run: int) -> Iterator[Tuple[int, int, int, int]]:
        """Yield `(zoom, row, col_start, col_end)` runs of at most `max_run`
        tiles, in ascending row then column order.

        Runs are the unit of work handed to render workers. Passing a run
        instead of individual tiles amortises queue and synchronisation
        overhead across up to `max_run` renders, and keeps the in-flight work
        representation tiny.
        """
        for row in sorted(self._rows):
            for start, end in self._rows[row]:
                col = start
                while col <= end:
                    stop = min(col + max_run - 1, end)
                    yield (zoom, row, col, stop)
                    col = stop + 1


class OccupancyIndex:
    """Which tiles actually contain content, at every requested zoom level.

    The scheduler iterates only these tiles: there is no walk over the full
    zoom/col/row grid and no per-tile emptiness check at render time, so
    runtime is proportional to real work rather than to extent size.

    A whole-layer bounding-box test was tried and rejected. For a large
    extent holding few scattered features, a layer's overall bbox covers
    almost the entire export area while real content touches a small
    fraction of it, and the mismatch compounds every zoom level (tile count
    quadruples, occupied fraction does not).

    Occupancy is built from each vector feature's own geometry bounding box,
    once, at the finest requested zoom, via
    `QgsTileMatrix.tileRangeFromExtent()`. It is padded outward by
    `tile_buffer` tiles to allow for rendered content - symbol size, line
    width, label placement - extending past bare geometry bounds, and
    clamped to the requested export extent. Coarser zooms are derived by
    exact quadtree parent folding.

    Raster and other area-covering layers are deliberately excluded from the
    occupied set. An orthophoto legitimately covers its whole extent, but
    letting it say so would reinstate exactly the "render every tile in the
    bbox" behaviour this class exists to avoid for the common sparse-vectors-
    over-a-basemap project. Area extents are used only as a last-resort
    fallback, applied once, if literally nothing else contributed a tile, so
    a pure-basemap export still produces output.
    """

    def __init__(
        self,
        snapshot: ProjectRenderSnapshot,
        tile_matrix_set: TileMatrixSet,
        min_zoom: int,
        max_zoom: int,
        logger: logging.Logger,
        tile_buffer: int = 1,
        should_cancel: Optional[Callable[[], bool]] = None,
        status_callback: Optional[Callable[[str], None]] = None,
    ):
        self._min_zoom = min_zoom
        self._max_zoom = max_zoom
        self.cancelled = False

        should_cancel = should_cancel or (lambda: False)
        status = status_callback or (lambda _msg: None)

        project = snapshot.project
        dest_crs = snapshot.dest_crs
        export_extent = snapshot.extent
        transform_context = project.transformContext()

        finest = tile_matrix_set.matrix(max_zoom)
        finest_w, finest_h = tile_matrix_set.dims(max_zoom)

        export_range = finest.tileRangeFromExtent(export_extent)
        if not export_range.isValid():
            raise RuntimeError(
                "The export extent does not intersect the tile matrix at all - nothing "
                "could be rendered. Check the requested extent and zoom range."
            )
        clamp = (
            max(0, export_range.startColumn()),
            min(finest_w - 1, export_range.endColumn()),
            max(0, export_range.startRow()),
            min(finest_h - 1, export_range.endRow()),
        )

        occupied = _RowIntervals()
        area_extents: List[QgsRectangle] = []
        usable_layers = 0
        layer_count = 0

        for layer in snapshot.layers:
            if should_cancel():
                self.cancelled = True
                break
            layer_count += 1
            try:
                if layer is None or not layer.isValid():
                    logger.warning(
                        "Occupancy: layer '%s' is invalid, skipping",
                        layer.name() if layer is not None else "?",
                    )
                    continue
                layer_extent = layer.extent()
                if layer_extent is None or layer_extent.isEmpty():
                    logger.warning("Occupancy: layer '%s' has an empty extent, skipping", layer.name())
                    continue

                src_crs = layer.crs()
                to_dest = (
                    QgsCoordinateTransform(src_crs, dest_crs, transform_context)
                    if src_crs != dest_crs
                    else None
                )
                extent_dest = (
                    to_dest.transformBoundingBox(layer_extent) if to_dest is not None else layer_extent
                )

                if isinstance(layer, QgsVectorLayer) and layer.isSpatial():
                    status(f"Scanning features: {layer.name()}")
                    scanned = self._mark_features(
                        layer=layer,
                        to_dest=to_dest,
                        transform_context=transform_context,
                        finest=finest,
                        export_extent=export_extent,
                        clamp=clamp,
                        tile_buffer=tile_buffer,
                        occupied=occupied,
                        logger=logger,
                        should_cancel=should_cancel,
                    )
                    if not scanned:
                        area_extents.append(extent_dest)
                else:
                    area_extents.append(extent_dest)

                usable_layers += 1
            except Exception as exc:
                logger.warning(
                    "Occupancy: skipping layer '%s' after an error: %s",
                    layer.name() if layer is not None else "?",
                    exc,
                )
                logger.debug("%s", traceback.format_exc())

        if should_cancel():
            self.cancelled = True

        occupied.compact()

        if not self.cancelled:
            self._validate(occupied, area_extents, finest, clamp, layer_count, usable_layers, logger)

        # Fold the finest level up to every coarser zoom.
        levels: List[_RowIntervals] = [None] * (max_zoom - min_zoom + 1)  # type: ignore[list-item]
        levels[max_zoom - min_zoom] = occupied
        current = occupied
        for zoom in range(max_zoom - 1, min_zoom - 1, -1):
            current = current.parent()
            levels[zoom - min_zoom] = current

        self._levels = levels
        self.total_tile_count = sum(level.tile_count for level in levels)

        logger.info(
            "Occupancy: %s tile(s) scheduled across zooms %d-%d (%s)",
            f"{self.total_tile_count:,}",
            min_zoom,
            max_zoom,
            ", ".join(
                f"z{z}={levels[z - min_zoom].tile_count:,}" for z in range(min_zoom, max_zoom + 1)
            ),
        )

    def _validate(self, occupied, area_extents, finest, clamp, layer_count, usable_layers, logger):
        if layer_count == 0:
            raise RuntimeError("The project has no renderable layers - nothing would be exported.")
        if usable_layers == 0:
            raise RuntimeError(
                f"None of the {layer_count} project layer(s) could be read. Every tile would be "
                "classified as empty, producing a schema-only GeoPackage. Check layer CRS and "
                "extent validity."
            )
        if not occupied.is_empty():
            return

        logger.warning(
            "Occupancy: no vector layer contributed any tile - falling back to %d area-layer "
            "extent(s) as a last resort",
            len(area_extents),
        )
        for extent_dest in area_extents:
            self._mark_extent(extent_dest, finest, clamp, occupied)
        occupied.compact()
        if occupied.is_empty():
            raise RuntimeError(
                f"No occupied tiles could be derived from any of the {layer_count} project "
                "layer(s), including the area-extent fallback. The output would contain schema "
                "only. Check layer CRS and extent validity."
            )

    @staticmethod
    def _clip(col_start, col_end, row_start, row_end, clamp):
        cmin, cmax, rmin, rmax = clamp
        col_start = cmin if col_start < cmin else col_start
        col_end = cmax if col_end > cmax else col_end
        row_start = rmin if row_start < rmin else row_start
        row_end = rmax if row_end > rmax else row_end
        if col_start > col_end or row_start > row_end:
            return None
        return col_start, col_end, row_start, row_end

    @classmethod
    def _mark_extent(cls, extent_dest, finest, clamp, occupied: _RowIntervals) -> None:
        tile_range = finest.tileRangeFromExtent(extent_dest)
        if not tile_range.isValid():
            return
        clipped = cls._clip(
            tile_range.startColumn(),
            tile_range.endColumn(),
            tile_range.startRow(),
            tile_range.endRow(),
            clamp,
        )
        if clipped is not None:
            occupied.add_block(*clipped)

    @classmethod
    def _mark_features(
        cls,
        layer,
        to_dest: Optional[QgsCoordinateTransform],
        transform_context,
        finest: QgsTileMatrix,
        export_extent: QgsRectangle,
        clamp,
        tile_buffer: int,
        occupied: _RowIntervals,
        logger: logging.Logger,
        should_cancel: Callable[[], bool],
    ) -> bool:
        """Mark the padded, clamped tile block of every feature intersecting
        the export extent. Returns True if a real scan happened."""
        request = QgsFeatureRequest()
        request.setSubsetOfAttributes([])
        try:
            if to_dest is not None:
                back = QgsCoordinateTransform(finest.crs(), layer.crs(), transform_context)
                request.setFilterRect(back.transformBoundingBox(export_extent))
            else:
                request.setFilterRect(export_extent)
        except Exception:
            # Without a usable filter rect we still scan, just unfiltered.
            request = QgsFeatureRequest()
            request.setSubsetOfAttributes([])

        # Hoist every attribute lookup out of the per-feature loop.
        tile_range_from_extent = finest.tileRangeFromExtent
        transform_bbox = to_dest.transformBoundingBox if to_dest is not None else None
        add_block = occupied.add_block
        clip = cls._clip

        processed = False
        counter = 0
        try:
            for feature in layer.getFeatures(request):
                counter += 1
                if (counter & 0x3FF) == 0 and should_cancel():
                    return processed
                geom = feature.geometry()
                if geom is None or geom.isEmpty():
                    continue
                processed = True
                bbox = geom.boundingBox()
                if bbox is None or bbox.isEmpty():
                    continue
                if transform_bbox is not None:
                    bbox = transform_bbox(bbox)
                tile_range = tile_range_from_extent(bbox)
                if not tile_range.isValid():
                    continue
                clipped = clip(
                    tile_range.startColumn() - tile_buffer,
                    tile_range.endColumn() + tile_buffer,
                    tile_range.startRow() - tile_buffer,
                    tile_range.endRow() + tile_buffer,
                    clamp,
                )
                if clipped is not None:
                    add_block(*clipped)
        except Exception as exc:
            logger.warning("Occupancy: feature scan failed for layer '%s': %s", layer.name(), exc)
            return False
        return processed

    def iter_blocks(self, block_tiles: int) -> Iterator[Tuple]:
        """Stream every scheduled metatile block, coarsest zoom first.

        Nothing is materialised beyond one zoom level's interval map, which is
        already resident, so peak memory stays proportional to queue depth
        rather than to job size.
        """
        for zoom in range(self._min_zoom, self._max_zoom + 1):
            yield from self._levels[zoom - self._min_zoom].iter_blocks(zoom, block_tiles)

    def tile_count(self, zoom: int) -> int:
        return self._levels[zoom - self._min_zoom].tile_count


# --------------------------------------------------------------------------
# GeoPackage writer
# --------------------------------------------------------------------------

class GeoPackageWriter:
    """Standards-compliant tiled-raster GeoPackage writer backed by one
    dedicated writer thread.

    The previous design shared a single connection between all render
    workers behind a mutex, so every worker's commit blocked every other
    worker. Here the connection is owned outright by one thread that does
    nothing else; renderers hand it byte-budgeted batches through a bounded
    queue and never touch SQLite or a database lock at all. The bounded
    queue is also the back-pressure mechanism that caps memory when
    rendering outruns disk.

    Row order in the table is irrelevant to the resulting GeoPackage, so
    batching and interleaving across threads never changes output.

    Journalling notes:
      * Rollback journal in memory, never WAL. WAL leaves tile data stranded
        in a `.gpkg-wal` side file if the final checkpoint does not run
        cleanly, producing a main file that looks schema-only.
      * `synchronous=OFF` during bulk load, restored to `FULL` with an
        explicit sync before close. The output file is brand new and is
        discarded on failure, so there is nothing to protect mid-run, and
        this removes an fsync per batch.
      * `locking_mode=EXCLUSIVE` avoids re-acquiring file locks per
        transaction; the file is released at `close()`, before anything else
        opens it.
      * The unique `(zoom, column, row)` index is created *after* the load
        rather than maintained per insert. Tiles are unique by construction
        (occupancy intervals are merged and zoom levels are disjoint), so a
        single sort at the end replaces B-tree maintenance on every row.
    """

    def __init__(
        self,
        path: str,
        table_name: str,
        tile_matrix_set: TileMatrixSet,
        min_zoom: int,
        max_zoom: int,
        matrix_set_extent: QgsRectangle,
        logger: logging.Logger,
        queue_depth: int = WRITE_QUEUE_DEPTH,
    ):
        self._table = table_name
        self._logger = logger
        self._path = path
        self._insert_sql = (
            f"INSERT INTO {table_name} (zoom_level, tile_column, tile_row, tile_data) VALUES (?,?,?,?)"
        )

        self._queue: "queue.Queue[Optional[list]]" = queue.Queue(maxsize=queue_depth)
        self._error: Optional[BaseException] = None
        self._rows_written = 0
        self._thread: Optional[threading.Thread] = None

        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._configure_pragmas()
        self._init_schema(tile_matrix_set, min_zoom, max_zoom, matrix_set_extent)

    # -- setup ------------------------------------------------------------

    def _configure_pragmas(self) -> None:
        conn = self._conn
        conn.execute("PRAGMA journal_mode=MEMORY")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-131072")  # ~128 MB page cache
        for pragma in ("PRAGMA locking_mode=EXCLUSIVE", "PRAGMA mmap_size=268435456"):
            try:
                conn.execute(pragma)
            except sqlite3.Error:
                pass  # best effort; neither affects file content

    def _init_schema(self, tile_matrix_set, min_zoom, max_zoom, extent) -> None:
        conn = self._conn
        conn.executescript(
            """
            BEGIN;
            CREATE TABLE IF NOT EXISTS gpkg_spatial_ref_sys (
                srs_name TEXT NOT NULL,
                srs_id INTEGER NOT NULL PRIMARY KEY,
                organization TEXT NOT NULL,
                organization_coordsys_id INTEGER NOT NULL,
                definition TEXT NOT NULL,
                description TEXT
            );
            CREATE TABLE IF NOT EXISTS gpkg_contents (
                table_name TEXT NOT NULL PRIMARY KEY,
                data_type TEXT NOT NULL,
                identifier TEXT UNIQUE,
                description TEXT DEFAULT '',
                last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE,
                srs_id INTEGER,
                CONSTRAINT fk_gc_r_srs_id FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys(srs_id)
            );
            CREATE TABLE IF NOT EXISTS gpkg_tile_matrix_set (
                table_name TEXT NOT NULL PRIMARY KEY,
                srs_id INTEGER NOT NULL,
                min_x DOUBLE NOT NULL, min_y DOUBLE NOT NULL,
                max_x DOUBLE NOT NULL, max_y DOUBLE NOT NULL,
                CONSTRAINT fk_gtms_srs_id FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys(srs_id)
            );
            CREATE TABLE IF NOT EXISTS gpkg_tile_matrix (
                table_name TEXT NOT NULL,
                zoom_level INTEGER NOT NULL,
                matrix_width INTEGER NOT NULL,
                matrix_height INTEGER NOT NULL,
                tile_width INTEGER NOT NULL,
                tile_height INTEGER NOT NULL,
                pixel_x_size DOUBLE NOT NULL,
                pixel_y_size DOUBLE NOT NULL,
                CONSTRAINT pk_ttm PRIMARY KEY (table_name, zoom_level)
            );
            COMMIT;
            """
        )

        # GeoPackage requires the tiles pk to be INTEGER PRIMARY KEY
        # AUTOINCREMENT, so it stays despite the sqlite_sequence write it
        # costs per insert.
        conn.execute("BEGIN")
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self._table} ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "zoom_level INTEGER NOT NULL, "
            "tile_column INTEGER NOT NULL, "
            "tile_row INTEGER NOT NULL, "
            "tile_data BLOB NOT NULL)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO gpkg_spatial_ref_sys "
            "(srs_name, srs_id, organization, organization_coordsys_id, definition, description) VALUES "
            "('Undefined Cartesian SRS', -1, 'NONE', -1, 'undefined', "
            "'undefined cartesian coordinate reference system'),"
            "('Undefined Geographic SRS', 0, 'NONE', 0, 'undefined', "
            "'undefined geographic coordinate reference system'),"
            "('WGS 84 geodetic', 4326, 'EPSG', 4326, "
            "'GEOGCS[\"WGS 84\",DATUM[\"WGS_1984\",SPHEROID[\"WGS 84\",6378137,298.257223563]],"
            "PRIMEM[\"Greenwich\",0],UNIT[\"degree\",0.0174532925199433]]', "
            "'longitude/latitude WGS 84')"
        )
        bounds = (extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum())
        conn.execute(
            "INSERT OR REPLACE INTO gpkg_tile_matrix_set "
            "(table_name, srs_id, min_x, min_y, max_x, max_y) VALUES (?, 4326, ?, ?, ?, ?)",
            (self._table, *bounds),
        )
        conn.execute(
            "INSERT OR REPLACE INTO gpkg_contents "
            "(table_name, data_type, identifier, description, min_x, min_y, max_x, max_y, srs_id) "
            "VALUES (?, 'tiles', ?, 'XYZ raster export', ?, ?, ?, ?, 4326)",
            (self._table, self._table, *bounds),
        )
        rows = []
        for zoom in range(min_zoom, max_zoom + 1):
            width, height = tile_matrix_set.dims(zoom)
            zoom_extent = tile_matrix_set.extent(zoom)
            rows.append(
                (
                    self._table,
                    zoom,
                    width,
                    height,
                    TILE_SIZE,
                    TILE_SIZE,
                    (zoom_extent.width() / width) / TILE_SIZE,
                    (zoom_extent.height() / height) / TILE_SIZE,
                )
            )
        conn.executemany(
            "INSERT OR REPLACE INTO gpkg_tile_matrix (table_name, zoom_level, matrix_width, "
            "matrix_height, tile_width, tile_height, pixel_x_size, pixel_y_size) VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.execute("COMMIT")

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="xyz-gpkg-writer", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        conn = self._conn
        insert_sql = self._insert_sql
        get = self._queue.get
        try:
            while True:
                batch = get()
                if batch is None:
                    return
                conn.execute("BEGIN")
                conn.executemany(insert_sql, batch)
                conn.execute("COMMIT")
                self._rows_written += len(batch)
        except BaseException as exc:  # noqa: BLE001 - surfaced to the controller
            self._error = exc
            # Keep draining so producers blocked on a full queue can exit.
            try:
                while get() is not None:
                    pass
            except Exception:
                pass

    def submit(self, batch: list, stop: threading.Event) -> None:
        """Hand a batch of `(zoom, col, row, blob)` rows to the writer.

        Blocks (with back-pressure) while the writer is behind, but wakes
        every 100 ms so a cancellation is honoured promptly rather than
        being stuck behind a full queue.
        """
        if self._error is not None:
            raise RuntimeError(f"GeoPackage writer failed: {self._error}")
        while True:
            try:
                self._queue.put(batch, timeout=0.1)
                return
            except queue.Full:
                if stop.is_set():
                    return
                if self._error is not None:
                    raise RuntimeError(f"GeoPackage writer failed: {self._error}")

    def close(self, build_index: bool = True) -> int:
        """Stop the writer thread, finalise the file and return the row count."""
        if self._thread is not None:
            self._queue.put(None)
            self._thread.join()
            self._thread = None
        if self._error is not None:
            raise RuntimeError(f"GeoPackage writer failed: {self._error}") from self._error

        conn = self._conn
        try:
            if build_index:
                self._create_unique_index()
            conn.execute("PRAGMA synchronous=FULL")
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass  # expected: WAL is never enabled here
            conn.execute("PRAGMA optimize")
            row_count = conn.execute(f"SELECT COUNT(*) FROM {self._table}").fetchone()[0]
        finally:
            conn.close()
        self._logger.info("GeoPackage finalised: %s tile row(s) in '%s'", f"{row_count:,}", self._table)
        return row_count

    def _create_unique_index(self) -> None:
        index_sql = (
            f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{self._table}_zyx "
            f"ON {self._table}(zoom_level, tile_column, tile_row)"
        )
        try:
            self._conn.execute(index_sql)
        except sqlite3.IntegrityError:
            # Cannot happen with an intact occupancy index, but a corrupt or
            # externally modified file should degrade rather than abort.
            self._logger.warning(
                "Duplicate tile coordinates found; de-duplicating before indexing."
            )
            self._conn.execute("BEGIN")
            self._conn.execute(
                f"DELETE FROM {self._table} WHERE id NOT IN "
                f"(SELECT MAX(id) FROM {self._table} GROUP BY zoom_level, tile_column, tile_row)"
            )
            self._conn.execute("COMMIT")
            self._conn.execute(index_sql)


# --------------------------------------------------------------------------
# Tile rendering
# --------------------------------------------------------------------------

@dataclass
class _WorkerRenderState:
    """Per-thread render scratch space.

    Allocated once per worker and reused for every metatile it renders. The
    map settings are configured once at thread start-up (layers, DPI, CRS,
    flags, labeling settings, background); the expression context is
    refreshed only when the zoom level changes, the output size only when the
    block dimensions change, and only `setExtent()` varies per block.

    `image` is the full-size metatile target, allocated once. Edge blocks -
    the partial blocks at the right and bottom of a matrix - need a smaller
    target, so they get a short-lived image instead of permanently caching
    every distinct edge size. Edge blocks are a small minority, and this
    keeps per-worker memory at a single predictable figure.
    """

    settings: QgsMapSettings
    image: QImage
    painter: QPainter
    buffer: QBuffer
    context: QgsExpressionContext
    scope: QgsExpressionContextScope
    layers: List[QgsMapLayer] = field(default_factory=list)
    zoom: int = -1
    size: Tuple[int, int] = (0, 0)

    def target(self, width: int, height: int) -> QImage:
        if width == self.image.width() and height == self.image.height():
            return self.image
        return QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)


class TileRenderer:
    """Renders one tile from the captured project snapshot.

    Every worker thread gets its own cloned layer set: neither
    `QgsMapRendererCustomPainterJob` nor most providers are safe to drive
    concurrently through a single `QgsMapLayer`. All clone construction is
    serialised behind one lock during worker start-up. That is strictly
    stronger than the previous per-source-file locking - it removes the
    concurrent first-open race against a shared physical `.gpkg` for every
    provider, not just OGR - and it costs nothing steady-state because it
    only ever runs once per thread, before any tile is rendered.
    """

    def __init__(
        self,
        snapshot: ProjectRenderSnapshot,
        dpi: int,
        tile_format: str,
        quality: int,
        min_zoom: int,
        max_zoom: int,
        logger: logging.Logger,
        metatile_size_px: int = DEFAULT_METATILE_SIZE_PX,
    ):
        self._snapshot = snapshot
        self._dpi = dpi
        self._quality = quality
        self._logger = logger
        self._min_zoom = min_zoom

        self._format = tile_format.upper()
        self._is_native = self._format in QIMAGE_NATIVE_FORMATS
        self._qt_format = "JPG" if self._format == "JPEG" else self._format

        self._clone_lock = threading.Lock()

        self.block_tiles = normalise_metatile_size(metatile_size_px) // TILE_SIZE
        self._block_px = self.block_tiles * TILE_SIZE

        # DPI is fixed for the whole export, so the "paper" width of a single
        # tile is invariant and the zoom-0 scale denominator is computed
        # exactly once; every deeper zoom is that baseline halved, because the
        # matrix is a strict quadtree.
        #
        # Metatiling does not enter this calculation at all, and that is the
        # whole point: a block covers `block_tiles` times the ground width at
        # `block_tiles` times the pixel width, so ground units per pixel - and
        # therefore the scale denominator - is identical to a single tile at
        # the same zoom. Symbology, label and layer-visibility thresholds
        # evaluate exactly as they do without metatiling.
        paper_width_m = (TILE_SIZE / float(dpi)) * INCH_METERS
        scale_zoom0 = SCALE_DENOMINATOR_FACTOR * equatorial_scale_denominator(
            ZOOM0_TILE_SPAN_DEGREES, paper_width_m
        )
        self._scales: Tuple[float, ...] = tuple(
            scale_zoom0 / (1 << zoom) for zoom in range(min_zoom, max_zoom + 1)
        )

    # -- per-thread setup -------------------------------------------------

    def create_state(self) -> _WorkerRenderState:
        """Build and fully configure this thread's render state.

        Called once per worker before it renders anything.
        """
        snapshot = self._snapshot
        layers, style_overrides = self._clone_layers()

        settings = QgsMapSettings()
        settings.setDestinationCrs(snapshot.dest_crs)
        settings.setLayers(layers)
        if style_overrides:
            settings.setLayerStyleOverrides(style_overrides)
        settings.setBackgroundColor(_TRANSPARENT)
        # QgsMapSettings does not inherit the project's labeling engine
        # settings on its own; without this, tiles render with engine
        # defaults and labels are free to cross tile boundaries.
        settings.setLabelingEngineSettings(snapshot.labeling_settings)
        settings.setOutputDpi(self._dpi)
        # Output size is set per block by render_block(), because edge blocks
        # are smaller than a full metatile.
        for flag in (_MS_FLAG_ANTIALIASING, _MS_FLAG_RENDER_MAP_TILE, _MS_FLAG_ADVANCED_EFFECTS):
            if flag is not None:
                settings.setFlag(flag, True)

        scope = QgsExpressionContextScope()
        scope.setVariable("map_scale", 0.0, True)
        context = QgsExpressionContext(snapshot.expression_context)
        context.appendScope(scope)

        buffer = QBuffer()

        return _WorkerRenderState(
            settings=settings,
            image=QImage(self._block_px, self._block_px, QImage.Format.Format_ARGB32_Premultiplied),
            painter=QPainter(),
            buffer=buffer,
            context=context,
            scope=scope,
            layers=layers,
        )

    def _clone_layers(self) -> Tuple[List[QgsMapLayer], Dict[str, str]]:
        """Clone the snapshot layers for exclusive use by the calling thread,
        remapping any map-theme style overrides onto the clone IDs.

        The override remap is a genuine bug fix: `mapThemeStyleOverrides()`
        is keyed by the *original* layer ID, and clones get fresh IDs, so the
        overrides were previously handed to `QgsMapSettings` under keys that
        matched nothing and were silently discarded.
        """
        snapshot = self._snapshot
        source_overrides = snapshot.style_overrides
        clones: List[QgsMapLayer] = []
        overrides: Dict[str, str] = {}

        with self._clone_lock:
            for layer in snapshot.layers:
                original_id = layer.id()
                try:
                    clone = layer.clone()
                    if clone is None or not clone.isValid():
                        raise RuntimeError("clone() returned an invalid layer")
                except Exception as exc:
                    self._logger.warning(
                        "Could not clone layer '%s' for a render thread, sharing the original (%s)",
                        layer.name(),
                        exc,
                    )
                    clone = layer
                clones.append(clone)
                style = source_overrides.get(original_id)
                if style is not None:
                    overrides[clone.id()] = style
        return clones, overrides

    # -- per-block --------------------------------------------------------

    def block_extent(
        self,
        tile_matrix_set: "TileMatrixSet",
        zoom: int,
        col_start: int,
        col_end: int,
        row_start: int,
        row_end: int,
    ) -> QgsRectangle:
        """Ground extent of a tile block, derived only from QgsTileMatrix.

        Built as the union of the block's north-west and south-east corner
        tiles rather than from independent zoom/row/col arithmetic, so
        QgsTileMatrix remains the single source of truth for every bound and
        the block edges coincide exactly with the tile edges inside it.

        Only tiles that actually exist in the matrix are referenced, so this
        never relies on extrapolating past the matrix edge.
        """
        extent = QgsRectangle(tile_matrix_set.tile_extent(zoom, col_start, row_start))
        extent.combineExtentWith(tile_matrix_set.tile_extent(zoom, col_end, row_end))
        return extent

    def render_block(
        self,
        state: _WorkerRenderState,
        tile_matrix_set: "TileMatrixSet",
        zoom: int,
        col_start: int,
        col_end: int,
        row_start: int,
        row_end: int,
    ) -> QImage:
        """Render a whole tile block in one job and return the target image.

        Rows increase southward from the matrix origin and QImage y increases
        downward, so a tile at (col, row) occupies
        `((col - col_start) * TILE_SIZE, (row - row_start) * TILE_SIZE)` in the
        returned image, with no coordinate flip.
        """
        cols = col_end - col_start + 1
        rows = row_end - row_start + 1
        width = cols * TILE_SIZE
        height = rows * TILE_SIZE

        settings = state.settings

        # The scale denominator is a function of zoom only, and work is
        # dispatched zoom-major, so this fires once per zoom per thread.
        # QgsMapSettings copies the context, so it must be re-set whenever the
        # scope changes.
        if state.zoom != zoom:
            state.scope.setVariable("map_scale", self._scales[zoom - self._min_zoom], True)
            settings.setExpressionContext(state.context)
            state.zoom = zoom

        # Only changes for edge blocks, so this is effectively a no-op on the
        # hot path.
        if state.size != (width, height):
            settings.setOutputSize(QSize(width, height))
            state.size = (width, height)

        # The extent's aspect ratio equals the output size's aspect ratio
        # exactly (square tiles, integer block dimensions), so QgsMapSettings
        # performs no aspect adjustment and each tile lands on an exact
        # 256-pixel boundary.
        settings.setExtent(
            self.block_extent(tile_matrix_set, zoom, col_start, col_end, row_start, row_end)
        )

        image = state.target(width, height)
        painter = state.painter
        image.fill(0)  # transparent for ARGB32_Premultiplied
        painter.begin(image)
        try:
            QgsMapRendererCustomPainterJob(settings, painter).renderSynchronously()
        finally:
            painter.end()
        return image

    def encode_tile(self, state: _WorkerRenderState, block_image: QImage, x: int, y: int) -> bytes:
        """Slice one tile out of a rendered block and encode it.

        `QImage.copy()` produces a standalone image in the same
        ARGB32_Premultiplied format the per-tile path used, so the encoder
        input - and therefore the encoded bytes for identical pixels - is
        unchanged.
        """
        return self._encode(state, block_image.copy(x, y, TILE_SIZE, TILE_SIZE))

    def _encode(self, state: _WorkerRenderState, image: QImage) -> bytes:
        if not self._is_native:
            return _encode_via_gdal(image, self._format, self._quality)
        buffer = state.buffer
        buffer.close()
        buffer.buffer().clear()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        try:
            image.save(buffer, self._qt_format, self._quality)
            return bytes(buffer.data())
        finally:
            buffer.close()


def _encode_via_gdal(image: QImage, driver_name: str, quality: int) -> bytes:
    """Encoder for formats QImage cannot write natively (e.g. JPEG2000).

    Not a hot path for the usual PNG/JPEG/WEBP export, but the previous
    version imported gdal and numpy on every single call and issued four
    separate `WriteArray` calls per tile. This writes all four interleaved
    RGBA bands in one pass and keeps the imports local only because GDAL is
    optional at module import time.
    """
    from osgeo import gdal

    rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
    width, height = rgba.width(), rgba.height()
    bits = rgba.constBits()
    bits.setsize(rgba.sizeInBytes())
    raw = bytes(bits)

    mem_ds = gdal.GetDriverByName("MEM").Create("", width, height, 4, gdal.GDT_Byte)
    try:
        mem_ds.WriteRaster(
            0, 0, width, height, raw,
            buf_type=gdal.GDT_Byte,
            band_list=[1, 2, 3, 4],
            buf_pixel_space=4,
            buf_line_space=rgba.bytesPerLine(),
            buf_band_space=1,
        )

        out_driver = "JP2OpenJPEG" if driver_name in ("JPEG2000", "JP2OPENJPEG") else driver_name
        options = [f"QUALITY={max(1, quality)}"] if out_driver == "JP2OpenJPEG" else []

        vsi_path = f"/vsimem/xyz_tile_{threading.get_ident()}_{id(rgba)}.tmp"
        out_ds = gdal.GetDriverByName(out_driver).CreateCopy(vsi_path, mem_ds, options=options)
        if out_ds is None:
            raise RuntimeError(f"GDAL driver '{out_driver}' failed to encode the tile")
        try:
            out_ds.FlushCache()
        finally:
            out_ds = None
        try:
            handle = gdal.VSIFOpenL(vsi_path, "rb")
            gdal.VSIFSeekL(handle, 0, 2)
            size = gdal.VSIFTellL(handle)
            gdal.VSIFSeekL(handle, 0, 0)
            data = gdal.VSIFReadL(1, size, handle)
            gdal.VSIFCloseL(handle)
        finally:
            gdal.Unlink(vsi_path)
        return bytes(data)
    finally:
        mem_ds = None


# --------------------------------------------------------------------------
# Exporter
# --------------------------------------------------------------------------

class _RenderWorker(QThread):
    """A render worker as a QThread so it can be started at low priority.

    `threading.Thread` gives no way to express scheduling priority, and
    priority is the difference between a responsive GUI and a frozen one here:
    the workers hold process-global QGIS/Qt locks (font database, coordinate
    transform cache, SVG cache) for nearly their entire runtime, and the main
    thread needs those same locks to draw anything at all.

    `run()` delegates straight back to the exporter's loop, so the rendering
    logic itself is unchanged and untangled from the threading mechanism.
    """

    def __init__(self, index: int, loop: Callable[..., None], args: tuple):
        super().__init__()
        self._loop = loop
        self._args = args
        self.setObjectName(f"xyz-render-{index}")

    def run(self) -> None:  # noqa: D102 - QThread entry point
        self._loop(*self._args)


@dataclass
class _WorkerStats:
    """Counters owned exclusively by one worker thread.

    Only the owning thread writes; the controller reads them for throttled
    progress. That makes the per-tile hot path completely lock-free, and a
    slightly stale read is harmless for a progress bar.
    """

    __slots__ = ("written", "skipped", "failed")

    def __init__(self) -> None:
        self.written = 0
        self.skipped = 0
        self.failed = 0


class XyzGpkgExporter:
    """Exports the current QGIS project as raster XYZ tiles into one
    GeoPackage.

    Construction captures the project snapshot and therefore **must happen
    on the main/GUI thread** (or be handed a snapshot captured there).
    `export()` performs all heavy work and **must not** run on the main
    thread; use `run_in_background()` from the console, or let the
    Processing algorithm call it from its worker thread.
    """

    def __init__(
        self,
        extent: Optional[QgsRectangle] = None,
        min_zoom: int = 0,
        max_zoom: int = 10,
        dpi: int = 96,
        max_cpu_percent: int = 75,
        tile_format: str = "PNG",
        quality: int = 85,
        output_dir: str = ".",
        progress_callback: Optional[Callable[[int, int], None]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
        occupancy_tile_buffer: int = 1,
        metatile_size_px: int = DEFAULT_METATILE_SIZE_PX,
        status_callback: Optional[Callable[[str], None]] = None,
        snapshot: Optional[ProjectRenderSnapshot] = None,
    ):
        if min_zoom < 0 or max_zoom < min_zoom:
            raise ValueError("Invalid zoom range: max_zoom must be >= min_zoom >= 0")

        self._logger = make_logger()

        self.min_zoom = int(min_zoom)
        self.max_zoom = int(max_zoom)
        self.dpi = int(dpi)
        self.max_cpu_percent = max(1, min(100, int(max_cpu_percent)))
        self.tile_format = tile_format.upper()
        self.quality = max(0, min(100, int(quality)))
        self.occupancy_tile_buffer = max(0, int(occupancy_tile_buffer))
        self.metatile_size_px = normalise_metatile_size(metatile_size_px)

        self.progress_callback = progress_callback
        self.status_callback = status_callback
        self.should_cancel = should_cancel

        # Main-thread work. Doing this in the constructor is deliberate: it
        # is O(1), and it means the caller controls which thread it runs on.
        self.snapshot = snapshot if snapshot is not None else ProjectRenderSnapshot.capture(extent)
        self.extent = self.snapshot.extent
        self.dest_crs = self.snapshot.dest_crs
        self.project = self.snapshot.project

        self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.output_path = os.path.join(self.output_dir, f"xyz_export_{timestamp}.gpkg")

        self.tiles_written = 0
        self.tiles_skipped = 0
        self.tiles_failed = 0
        self.cancelled = False

        block_tiles = self.metatile_size_px // TILE_SIZE
        self._logger.info(
            "Export configured: %d layer(s), zoom %d-%d, %s @ %d dpi, theme=%s, "
            "metatile %dpx (%dx%d tiles per render job)",
            len(self.snapshot.layers),
            self.min_zoom,
            self.max_zoom,
            self.tile_format,
            self.dpi,
            self.snapshot.theme_name or "<none>",
            self.metatile_size_px,
            block_tiles,
            block_tiles,
        )

    # -- small helpers ----------------------------------------------------

    def _cancelled(self) -> bool:
        return bool(self.should_cancel and self.should_cancel())

    def _status(self, message: str) -> None:
        if self.status_callback:
            try:
                self.status_callback(message)
            except Exception:
                pass

    def _report(self, done: int, total: int) -> None:
        if self.progress_callback:
            try:
                self.progress_callback(done, total)
            except Exception:
                pass

    # -- main entry point -------------------------------------------------

    def export(self) -> Optional[str]:
        """Render the export. Returns the GeoPackage path, or None if the
        run was cancelled before anything usable was produced.

        Runs entirely off the GUI thread. The controller loop below never
        blocks for more than `PROGRESS_INTERVAL_S`, so cancellation is
        always acknowledged promptly and progress keeps flowing even when
        the pipeline is saturated.
        """
        started = time.monotonic()
        self._logger.info("Starting export to %s", self.output_path)

        self._status("Building tile matrices...")
        tile_matrix_set = TileMatrixSet(self.dest_crs, self.min_zoom, self.max_zoom)

        self._status("Analysing layer content...")
        occupancy = OccupancyIndex(
            snapshot=self.snapshot,
            tile_matrix_set=tile_matrix_set,
            min_zoom=self.min_zoom,
            max_zoom=self.max_zoom,
            logger=self._logger,
            tile_buffer=self.occupancy_tile_buffer,
            should_cancel=self.should_cancel,
            status_callback=self._status,
        )
        if occupancy.cancelled or self._cancelled():
            self.cancelled = True
            self._logger.info("Export cancelled during content analysis.")
            return None

        total_tiles = occupancy.total_tile_count
        if total_tiles == 0:
            raise RuntimeError(
                "No tiles were scheduled. Check that the export extent intersects the project "
                "layers and that the zoom range is correct."
            )

        worker_count = compute_worker_count(self.max_cpu_percent)
        self._logger.info(
            "Rendering %s tile(s) with %d worker thread(s), %dpx metatiles (~%.1f MB "
            "render target per worker)",
            f"{total_tiles:,}",
            worker_count,
            self.metatile_size_px,
            (self.metatile_size_px ** 2 * 4) / (1024 * 1024),
        )
        self._status(f"Rendering {total_tiles:,} tiles...")

        writer = GeoPackageWriter(
            path=self.output_path,
            table_name="xyz_tiles",
            tile_matrix_set=tile_matrix_set,
            min_zoom=self.min_zoom,
            max_zoom=self.max_zoom,
            matrix_set_extent=tile_matrix_set.extent(self.min_zoom),
            logger=self._logger,
        )
        renderer = TileRenderer(
            snapshot=self.snapshot,
            dpi=self.dpi,
            tile_format=self.tile_format,
            quality=self.quality,
            min_zoom=self.min_zoom,
            max_zoom=self.max_zoom,
            logger=self._logger,
            metatile_size_px=self.metatile_size_px,
        )

        stop = threading.Event()
        work_queue: "queue.Queue[Optional[Tuple[int, int, int, int]]]" = queue.Queue(
            maxsize=worker_count * 4
        )
        stats = [_WorkerStats() for _ in range(worker_count)]
        errors: "queue.Queue[str]" = queue.Queue()

        writer.start()
        # QThread rather than threading.Thread specifically so the workers can
        # be started at LowPriority. See lower_current_thread_priority() for
        # why priority is the lever that actually frees the GUI thread.
        workers = [
            _RenderWorker(
                index=index,
                loop=self._worker_loop,
                args=(index, renderer, writer, tile_matrix_set, work_queue, stats[index], stop, errors),
            )
            for index in range(worker_count)
        ]
        for worker in workers:
            worker.start(QThread.Priority.LowPriority)

        try:
            self._dispatch(occupancy, work_queue, stop, stats, total_tiles, renderer.block_tiles)
        finally:
            # Unblock every worker whatever happened, then wait them out
            # while continuing to tick progress and cancellation.
            for _ in workers:
                self._put_blocking(work_queue, None, stop=None)
            self._join_with_ticks(workers, stats, total_tiles)

            self.cancelled = stop.is_set()
            if not self.cancelled:
                self._status("Finalising GeoPackage (building tile index)...")
            try:
                row_count = writer.close(build_index=not self.cancelled)
            except Exception as exc:
                self._logger.error("Finalising the GeoPackage failed: %s", exc)
                raise

        self.tiles_written = sum(s.written for s in stats)
        self.tiles_skipped = sum(s.skipped for s in stats)
        self.tiles_failed = sum(s.failed for s in stats)
        self._drain_errors(errors)

        elapsed = time.monotonic() - started
        rate = self.tiles_written / elapsed if elapsed > 0 else 0.0
        self._logger.info(
            "Export %s in %.1fs: %s written, %s skipped, %s failed (%.0f tiles/s) -> %s",
            "cancelled" if self.cancelled else "complete",
            elapsed,
            f"{self.tiles_written:,}",
            f"{self.tiles_skipped:,}",
            f"{self.tiles_failed:,}",
            rate,
            self.output_path,
        )

        if self.cancelled:
            return self.output_path if row_count else None
        if self.tiles_written == 0:
            raise RuntimeError(
                f"The export produced no tiles (skipped={self.tiles_skipped}, "
                f"failed={self.tiles_failed}). See the QGIS Log Messages panel for details."
            )
        return self.output_path

    def run(self) -> Optional[str]:
        return self.export()

    # -- controller -------------------------------------------------------

    def _dispatch(self, occupancy, work_queue, stop, stats, total_tiles, block_tiles) -> None:
        """Feed metatile blocks into the pipeline, ticking progress and
        cancellation on a wall clock.

        `Queue.put()` with a timeout is a genuine blocking wait, not a spin:
        the thread sleeps on the queue's condition variable and is woken
        either by a consumer or by the timeout. That gives back-pressure
        (bounded memory) and a guaranteed responsiveness floor at the same
        time.
        """
        last_tick = 0.0
        put = work_queue.put

        for block in occupancy.iter_blocks(block_tiles):
            while True:
                try:
                    put(block, timeout=PROGRESS_INTERVAL_S)
                    break
                except queue.Full:
                    last_tick = self._tick(stats, total_tiles, last_tick, stop)
                    if stop.is_set():
                        return
            now = time.monotonic()
            if now - last_tick >= PROGRESS_INTERVAL_S:
                last_tick = self._tick(stats, total_tiles, now, stop, now=now)
                if stop.is_set():
                    return

    def _tick(self, stats, total_tiles, last_tick, stop, now: Optional[float] = None) -> float:
        """Emit throttled progress and poll for cancellation.

        Progress emission is throttled because `QgsProcessingFeedback` and
        `QgsTask` marshal it to the main thread as a queued signal. Emitting
        per tile - as the old Processing wrapper did - queues events far
        faster than the event loop can drain them, which is precisely what
        made QGIS report "not responding".
        """
        done = 0
        for entry in stats:
            done += entry.written + entry.skipped + entry.failed
        self._report(done, total_tiles)
        # The deepest zoom usually holds the overwhelming majority of tiles,
        # so an honest percentage sits below 0.5% - and renders as "0%" - for a
        # long time. Surfacing absolute counts makes real progress
        # distinguishable from a hang.
        self._status(f"Rendered {done:,} of {total_tiles:,} tiles")
        if not stop.is_set() and self._cancelled():
            self._logger.info("Cancellation requested - draining the render pipeline.")
            stop.set()
        return now if now is not None else time.monotonic()

    def _join_with_ticks(self, workers, stats, total_tiles) -> None:
        """Wait out the render workers while still emitting progress.

        A plain `join()` would go quiet for however long the tail takes;
        joining in short slices keeps the progress bar and the dialog alive
        right through shutdown without any busy waiting - each slice is a
        real timed wait on the thread's completion condition.
        """
        remaining = list(workers)
        timeout_ms = int(PROGRESS_INTERVAL_S * 1000)
        while remaining:
            for worker in tuple(remaining):
                if worker.wait(timeout_ms):
                    remaining.remove(worker)
            self._report(sum(s.written + s.skipped + s.failed for s in stats), total_tiles)

    @staticmethod
    def _put_blocking(work_queue: "queue.Queue", item, stop: Optional[threading.Event]) -> None:
        while True:
            try:
                work_queue.put(item, timeout=PROGRESS_INTERVAL_S)
                return
            except queue.Full:
                if stop is not None and stop.is_set():
                    return

    def _drain_errors(self, errors: "queue.Queue[str]") -> None:
        reported = 0
        while True:
            try:
                message = errors.get_nowait()
            except queue.Empty:
                break
            if reported < MAX_REPORTED_TILE_ERRORS:
                self._logger.error("%s", message)
                reported += 1
        if self.tiles_failed > reported:
            self._logger.error(
                "%s further tile failure(s) were suppressed to avoid flooding the log.",
                f"{self.tiles_failed - reported:,}",
            )

    # -- worker -----------------------------------------------------------

    def _worker_loop(
        self,
        index: int,
        renderer: TileRenderer,
        writer: GeoPackageWriter,
        tile_matrix_set: TileMatrixSet,
        work_queue: "queue.Queue",
        stats: _WorkerStats,
        stop: threading.Event,
        errors: "queue.Queue[str]",
    ) -> None:
        """Render runs until the queue is exhausted or cancellation is set.

        A persistent worker replaces the previous one-`Future`-per-tile
        pipeline, which allocated a Future, a lock and a condition variable
        for every ~5 ms of work and re-installed waiters across all
        outstanding futures on every `wait(FIRST_COMPLETED)` call.
        """
        # Portable QThread priority is a request that Linux ignores; this is
        # the per-thread OS-level fallback that actually takes effect there.
        lower_current_thread_priority()

        try:
            state = renderer.create_state()
        except Exception as exc:
            errors.put(f"Render worker {index} could not initialise: {exc}\n{traceback.format_exc()}")
            stop.set()
            # Still drain so the controller is never blocked on a full queue.
            while work_queue.get() is not None:
                pass
            return

        render_block = renderer.render_block
        encode_tile = renderer.encode_tile
        get = work_queue.get
        pending: list = []
        pending_bytes = 0
        error_budget = MAX_REPORTED_TILE_ERRORS

        try:
            while True:
                block = get()
                if block is None:
                    return
                if stop.is_set():
                    continue  # keep draining; the controller sends sentinels

                zoom, block_row, block_col, members, _count = block

                # Bounds of the occupied part of this block. Rendering only as
                # far as real content extends means a block whose occupied
                # tiles sit in one corner does not rasterise the whole
                # metatile - the occupancy index still governs how much work
                # happens, exactly as it does without metatiling.
                row_start = members[0][0]
                row_end = members[-1][0]
                col_start = min(run[1] for run in members)
                col_end = max(run[2] for run in members)

                try:
                    block_image = render_block(
                        state, tile_matrix_set, zoom, col_start, col_end, row_start, row_end
                    )
                except Exception as exc:
                    # A whole block failed: count every tile it owned as
                    # failed rather than silently dropping them.
                    failed = sum(run[2] - run[1] + 1 for run in members)
                    stats.failed += failed
                    if error_budget > 0:
                        error_budget -= 1
                        errors.put(
                            f"Metatile z={zoom} block=({block_row},{block_col}) "
                            f"cols {col_start}-{col_end} rows {row_start}-{row_end} "
                            f"failed, losing {failed} tile(s): {exc}"
                        )
                    continue

                for row, run_start, run_end in members:
                    if stop.is_set():
                        break
                    y = (row - row_start) * TILE_SIZE
                    for col in range(run_start, run_end + 1):
                        try:
                            data = encode_tile(
                                state, block_image, (col - col_start) * TILE_SIZE, y
                            )
                            if not data:
                                stats.skipped += 1
                                continue
                            pending.append((zoom, col, row, data))
                            pending_bytes += len(data)
                            stats.written += 1
                        except Exception as exc:
                            stats.failed += 1
                            if error_budget > 0:
                                error_budget -= 1
                                errors.put(f"Tile z={zoom} x={col} y={row} failed: {exc}")

                if pending_bytes >= WRITE_BATCH_BYTES or len(pending) >= WRITE_BATCH_TILES:
                    writer.submit(pending, stop)
                    pending = []
                    pending_bytes = 0
        except Exception as exc:  # pragma: no cover - defensive
            errors.put(f"Render worker {index} aborted: {exc}\n{traceback.format_exc()}")
            stop.set()
        finally:
            if pending:
                try:
                    writer.submit(pending, stop)
                except Exception as exc:
                    errors.put(f"Render worker {index} could not flush its final batch: {exc}")
            # Free the Qt scratch objects deterministically rather than
            # waiting for interpreter shutdown.
            try:
                state.buffer.close()
                state.layers.clear()
            except Exception:
                pass

    # -- console convenience ----------------------------------------------

    @classmethod
    def run_in_background(
        cls,
        on_finished: Optional[Callable[[bool, Optional[str], Optional[str]], None]] = None,
        **kwargs,
    ) -> "XyzGpkgExporterTask":
        """Build an exporter and submit it to the QGIS task manager.

        Call this from the main thread (e.g. the Python console). The
        constructor performs only the O(1) main-thread snapshot; every slow
        phase runs inside the task, so this returns almost immediately and
        the GUI stays fully usable.
        """
        if QgsTask is None or QgsApplication is None:  # pragma: no cover
            raise RuntimeError(
                "QgsTask/QgsApplication are unavailable here - call export() directly instead."
            )
        exporter = cls(**kwargs)
        task = XyzGpkgExporterTask(exporter, on_finished=on_finished)
        QgsApplication.taskManager().addTask(task)
        return task


# --------------------------------------------------------------------------
# QgsTask wrapper (console / non-Processing use)
# --------------------------------------------------------------------------

class XyzGpkgExporterTask(QgsTask if QgsTask is not None else object):  # type: ignore[misc]
    """Runs `XyzGpkgExporter.export()` on a QgsTask worker thread.

    Progress is throttled here as well as in the exporter, because
    `QgsTask.setProgress()` emits a queued cross-thread signal into the task
    manager UI.
    """

    def __init__(
        self,
        exporter: XyzGpkgExporter,
        on_finished: Optional[Callable[[bool, Optional[str], Optional[str]], None]] = None,
    ):
        super().__init__(
            f"Export XYZ tiles to GeoPackage (zoom {exporter.min_zoom}-{exporter.max_zoom})",
            QgsTask.Flag.CanCancel,
        )
        self._exporter = exporter
        self._on_finished = on_finished
        self._error: Optional[str] = None
        self._output_path: Optional[str] = None
        self._last_emit = 0.0

        exporter.progress_callback = self._on_progress
        exporter.should_cancel = self.isCanceled

    def _on_progress(self, done: int, total: int) -> None:
        if total <= 0:
            return
        now = time.monotonic()
        if done < total and (now - self._last_emit) < PROGRESS_INTERVAL_S:
            return
        self._last_emit = now
        self.setProgress(min(100.0, 100.0 * done / total))

    def run(self) -> bool:
        try:
            self._output_path = self._exporter.export()
            return not self.isCanceled() and self._output_path is not None
        except Exception as exc:
            self._error = f"{exc}\n{traceback.format_exc()}"
            return False

    def finished(self, result: bool) -> None:
        # Called back on the main thread by QgsTaskManager.
        if not result and self._error:
            QgsMessageLog.logMessage(
                f"XYZ tile export failed: {self._error}", LOG_TAG, Qgis.MessageLevel.Critical
            )
        elif not result:
            QgsMessageLog.logMessage("XYZ tile export was cancelled", LOG_TAG, Qgis.MessageLevel.Info)
        if self._on_finished:
            self._on_finished(result, self._output_path, self._error)
