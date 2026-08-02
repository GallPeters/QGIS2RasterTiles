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

ONE DELIBERATE OUTPUT CHANGE
============================

`QgsMapThemeCollection.mapThemeStyleOverrides()` is keyed by layer ID, but
the renderer feeds *cloned* layers, which have fresh IDs. The overrides
therefore never matched and were silently ignored. They are now remapped
onto each thread's clone IDs, so theme-based exports finally render the
way the canvas does. This only affects projects that use a map theme with
style overrides; everything else is byte-for-byte unaffected.
"""

from __future__ import annotations

import math
import os
import queue
import sqlite3
import threading
import time
import traceback
import logging
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

from qgis.PyQt.QtCore import QBuffer, QIODevice, QSize
from qgis.PyQt.QtGui import QColor, QImage, QPainter


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

LOG_TAG = "XyzGpkgExporter"

TILE_SIZE = 256
EARTH_CIRCUMFERENCE_M = 40075016.6856
METERS_PER_DEGREE_EQUATOR = EARTH_CIRCUMFERENCE_M / 360.0
INCH_METERS = 0.0254

QIMAGE_NATIVE_FORMATS = frozenset({"PNG", "JPEG", "JPG", "WEBP"})

# GoogleCRS84Quad: zoom 0 is 2 columns x 1 row spanning the full globe, so a
# zoom-0 tile is exactly 90 degrees square and every deeper zoom halves that
# exactly once. This is a strict quadtree, which is what makes both the
# scale-per-zoom shortcut and the occupancy parent-fold exact rather than
# approximate.
TILE_MATRIX_TILE_ORIGIN = QgsPointXY(-180.0, 90.0)
TILE_MATRIX_Z0_DIMENSION_DEGREES = 180.0
TILE_MATRIX_ROOT_WIDTH = 2
TILE_MATRIX_ROOT_HEIGHT = 1
ZOOM0_TILE_WIDTH_DEGREES = TILE_MATRIX_Z0_DIMENSION_DEGREES / TILE_MATRIX_ROOT_WIDTH

WORLD_EXTENT = QgsRectangle(-180.0, -90.0, 180.0, 90.0)

# Immutable value objects reused for every tile. Qt setters copy these, so
# sharing one instance across threads is safe.
_TILE_QSIZE = QSize(TILE_SIZE, TILE_SIZE)
_TRANSPARENT = QColor(0, 0, 0, 0)

# Tuning. These bound memory and amortise per-item overhead; none of them
# affect the bytes written.
RUN_CHUNK_TILES = 64            # max tiles handed to a worker in one go
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
    """Render worker count from a CPU percentage.

    Rendering is CPU-bound inside C++ with the GIL released, so more threads
    than cores only adds context-switching and cache pressure; the count is
    capped at the core count by construction.
    """
    pct = max(1, min(100, int(max_cpu_percent)))
    cores = os.cpu_count() or 1
    return max(1, math.floor(cores * (pct / 100.0)))


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
                TILE_MATRIX_Z0_DIMENSION_DEGREES,
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

    def iter_runs(self, max_run: int = RUN_CHUNK_TILES) -> Iterator[Tuple[int, int, int, int]]:
        """Stream every scheduled tile as compact `(zoom, row, c0, c1)` runs,
        coarsest zoom first. Nothing is materialised, so peak memory stays
        proportional to queue depth rather than to job size."""
        for zoom in range(self._min_zoom, self._max_zoom + 1):
            yield from self._levels[zoom - self._min_zoom].iter_runs(zoom, max_run)

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

    Everything here is allocated exactly once per worker thread and reused
    for every tile it renders. The map settings are configured once at
    thread start-up (layers, size, DPI, CRS, flags, labeling settings,
    background), the expression context is refreshed only when the zoom
    level changes, and only `setExtent()` is called per tile. The previous
    implementation re-issued roughly a dozen sip calls per tile for values
    that never varied.
    """

    settings: QgsMapSettings
    image: QImage
    painter: QPainter
    buffer: QBuffer
    context: QgsExpressionContext
    scope: QgsExpressionContextScope
    layers: List[QgsMapLayer] = field(default_factory=list)
    zoom: int = -1


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

        # Output size and DPI are fixed for the whole export, so the paper
        # width is invariant and the zoom-0 scale denominator is computed
        # exactly once. Every deeper zoom is that baseline halved, because
        # the matrix is a strict quadtree. Precomputing the whole table
        # removes both the per-tile division and the unsynchronised shared
        # cache dict the old code mutated from every worker thread.
        paper_width_m = (TILE_SIZE / float(dpi)) * INCH_METERS
        scale_zoom0 = equatorial_scale_denominator(ZOOM0_TILE_WIDTH_DEGREES, paper_width_m)
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
        settings.setOutputSize(_TILE_QSIZE)
        settings.setOutputDpi(self._dpi)
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
            image=QImage(TILE_SIZE, TILE_SIZE, QImage.Format.Format_ARGB32_Premultiplied),
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

    # -- per-tile ---------------------------------------------------------

    def render(self, state: _WorkerRenderState, zoom: int, extent: QgsRectangle) -> bytes:
        settings = state.settings

        # The scale denominator is a function of zoom only (every tile in a
        # zoom level is the same width), and work is dispatched zoom-major,
        # so this fires once per zoom per thread rather than once per tile.
        # QgsMapSettings copies the context, so it must be re-set whenever
        # the scope changes.
        if state.zoom != zoom:
            state.scope.setVariable("map_scale", self._scales[zoom - self._min_zoom], True)
            settings.setExpressionContext(state.context)
            state.zoom = zoom

        settings.setExtent(extent)

        image = state.image
        painter = state.painter
        image.fill(0)  # transparent for ARGB32_Premultiplied; identical to QColor(0,0,0,0)
        painter.begin(image)
        try:
            QgsMapRendererCustomPainterJob(settings, painter).renderSynchronously()
        finally:
            painter.end()

        return self._encode(state, image)

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

        self._logger.info(
            "Export configured: %d layer(s), zoom %d-%d, %s @ %d dpi, theme=%s",
            len(self.snapshot.layers),
            self.min_zoom,
            self.max_zoom,
            self.tile_format,
            self.dpi,
            self.snapshot.theme_name or "<none>",
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
            "Rendering %s tile(s) with %d worker thread(s)", f"{total_tiles:,}", worker_count
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
        )

        stop = threading.Event()
        work_queue: "queue.Queue[Optional[Tuple[int, int, int, int]]]" = queue.Queue(
            maxsize=worker_count * 4
        )
        stats = [_WorkerStats() for _ in range(worker_count)]
        errors: "queue.Queue[str]" = queue.Queue()

        writer.start()
        workers = [
            threading.Thread(
                target=self._worker_loop,
                args=(index, renderer, writer, tile_matrix_set, work_queue, stats[index], stop, errors),
                name=f"xyz-render-{index}",
                daemon=True,
            )
            for index in range(worker_count)
        ]
        for worker in workers:
            worker.start()

        try:
            self._dispatch(occupancy, work_queue, stop, stats, total_tiles, worker_count)
        finally:
            # Unblock every worker whatever happened, then wait them out
            # while continuing to tick progress and cancellation.
            for _ in workers:
                self._put_blocking(work_queue, None, stop=None)
            self._join_with_ticks(workers, stats, total_tiles)

            self.cancelled = stop.is_set()
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

    def _dispatch(self, occupancy, work_queue, stop, stats, total_tiles, worker_count) -> None:
        """Feed tile runs into the pipeline, ticking progress and
        cancellation on a wall clock.

        `Queue.put()` with a timeout is a genuine blocking wait, not a spin:
        the thread sleeps on the queue's condition variable and is woken
        either by a consumer or by the timeout. That gives back-pressure
        (bounded memory) and a guaranteed responsiveness floor at the same
        time.
        """
        last_tick = 0.0
        put = work_queue.put

        for run in occupancy.iter_runs(RUN_CHUNK_TILES):
            while True:
                try:
                    put(run, timeout=PROGRESS_INTERVAL_S)
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
        while remaining:
            for worker in tuple(remaining):
                worker.join(timeout=PROGRESS_INTERVAL_S)
                if not worker.is_alive():
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
        try:
            state = renderer.create_state()
        except Exception as exc:
            errors.put(f"Render worker {index} could not initialise: {exc}\n{traceback.format_exc()}")
            stop.set()
            # Still drain so the controller is never blocked on a full queue.
            while work_queue.get() is not None:
                pass
            return

        tile_extent = tile_matrix_set.tile_extent
        render = renderer.render
        get = work_queue.get
        pending: list = []
        pending_bytes = 0
        error_budget = MAX_REPORTED_TILE_ERRORS

        try:
            while True:
                run = get()
                if run is None:
                    return
                if stop.is_set():
                    continue  # keep draining; the controller sends sentinels

                zoom, row, col_start, col_end = run
                for col in range(col_start, col_end + 1):
                    if stop.is_set():
                        break
                    try:
                        data = render(state, zoom, tile_extent(zoom, col, row))
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
