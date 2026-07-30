"""
xyz_gpkg_exporter.py

Core, console/standalone exporter that renders the currently loaded QGIS
project into a single GeoPackage of raster XYZ tiles (EPSG:4326).

Key correctness guarantees:
  * All tile bounds are derived exclusively from QgsTileMatrix (single
    source of truth) - never from independent zoom/row/col arithmetic -
    to avoid gapless/overlap ("puzzle") tile defects.
  * Per-tile render scale is recomputed fresh from the actual
    QgsMapSettings extent/output-size/output-dpi used for that tile, using
    an explicit equator-based formula, so rule-based symbology,
    scale-dependent labels, and scale-dependent layer visibility evaluate
    identically to what the live map canvas would show at an equivalent
    view - regardless of the project's "scale calculation method" setting.
  * The GeoPackage is written without WAL (avoiding tile data being
    stranded in a .gpkg-wal side file that never gets checkpointed into
    the main file), and every diagnostic/error is routed through
    QgsMessageLog so it is actually visible in the QGIS Log Messages
    panel, including when running inside a background QgsTask thread.
  * Each worker thread renders with its own cloned layer objects, since
    QgsMapRendererCustomPainterJob / provider read paths are not
    guaranteed safe to drive concurrently from multiple threads against
    the same shared QgsMapLayer instances.
  * When several project layers physically live inside the same
    GeoPackage (e.g. after running QGIS's "Package Layers" tool), the
    first-time clone/open of each of those layers is serialized per
    source file (see `_source_lock_key` / `TileRenderer._get_clone_lock`)
    so that many worker threads never call OGR/SQLite's first connection
    setup against the same physical .gpkg file at the same instant -
    that concurrent-first-open race is what previously produced a
    lock/deadlock that never resolved. This only guards the one-time
    per-thread clone step, so it does not serialize per-tile rendering.
  * Label truncation at tile edges is suppressed by building a single
    QgsLabelingEngineSettings once, on the main thread, from the
    project's own settings (with UsePartialCandidates forced off) and
    handing that already-built, immutable-for-our-purposes value object
    to every tile's QgsMapSettings. It is never (re)constructed on a
    worker thread, since building label-engine/font-related Qt objects
    off the main thread is not safe and was the cause of every tile
    failing (producing a schema-only GeoPackage) when this was
    previously attempted inside render_tile().

Performance design (this module is expected to render MILLIONS of tiles in
a single run, so per-tile overhead is optimised aggressively):
  * Every object whose value does not vary per tile - the QgsMapSettings
    instance, the QImage/QPainter render target, the QIODevice used to
    encode it, and even the QgsExpressionContext/Scope used to expose
    map_scale to expressions - is allocated exactly once per worker
    thread (thread-local) and reset/reused for every subsequent tile,
    instead of being reconstructed from scratch millions of times. Every
    property the renderer actually reads is still explicitly re-set on
    every single tile before use, so a reused instance is behaviourally
    identical to a freshly constructed one.
  * The equator-based scale denominator is only a function of a tile's
    *zoom level* (every tile in this custom, non-adaptive tile matrix is
    square and uniform-width within a zoom), so it is computed once per
    zoom level and cached, rather than recomputed with float division on
    every tile.
  * GeoPackage writes are batched per-thread and only touch the shared
    SQLite connection/lock once per batch, not once per tile, eliminating
    per-tile lock contention on the write path. Export statistics
    (written/skipped/failed counters) use the same per-thread-then-
    aggregate pattern, so the per-tile hot path never takes a lock at all.
  * Tile tasks are submitted to the thread pool through a bounded
    in-flight pipeline instead of enqueueing every tile up front, so peak
    memory (outstanding Future objects, queued call args) stays
    proportional to worker count rather than to total tile count - a
    multi-million-tile export no longer needs to hold millions of Future
    objects in memory simultaneously.
  * The scale denominator at zoom 0 is computed exactly once (it is a
    function of dpi/tile size only, both fixed for the whole export), then
    every other zoom's scale is simply that value divided by 2**zoom - no
    per-zoom trip through the equatorial-degrees-to-metres formula, since
    this tile matrix is an exact quadtree where tile width at zoom z is by
    definition the zoom-0 tile width halved z times.
  * OccupancyIndex precomputes exactly which tiles contain feature content
    - from each vector feature's own geometry bounding box, not just each
    layer's overall extent - once at the finest requested zoom level, then
    folds that up to every coarser zoom via exact quadtree parent
    aggregation. Raster/basemap layers (e.g. an orthophoto background) are
    deliberately NOT allowed to force their whole extent into the occupied
    set: only vector layers drive which tiles get scheduled, and a raster
    is treated purely as content painted *into* whichever tiles the vector
    data already earned - never as a reason to schedule additional tiles.
    (The whole-extent fallback is still used, but only as a last resort if
    literally no vector layer contributed anything, so a pure-basemap
    export still produces something.) The tile scheduler iterates *only*
    this precomputed occupied-tile set: there is no walk over the full
    zoom/col/row grid and no per-tile emptiness check at render time. This
    is what actually keeps runtime proportional to real work rather than
    to extent size - a bbox-only (or full-grid-walk-plus-filter, or an
    occupancy set that a full-coverage raster is allowed to inflate back
    up to nearly the whole grid) approach gets *proportionally* worse at
    high zoom levels for sparse vector data over a wide background, since a
    shrinking fraction of each tile within that bbox is genuinely occupied
    as tiles get smaller. See the OccupancyIndex class docstring for full
    details.
  * The whole export can be run inside a QgsTask (XyzGpkgExporter.
    run_in_background()) instead of being called synchronously from the
    caller's own thread. XyzGpkgExporter already parallelizes tile
    rendering across a ThreadPoolExecutor internally, but that only kept
    *rendering* off any single thread - if export() were still invoked
    directly from the main/GUI thread, that thread blocked synchronously
    waiting on the pool for the whole run, freezing the QGIS GUI/event
    loop for however long the export took. Running the whole export() call
    inside a QgsTask fixes that: QgsTask.run() executes on a Qt-managed
    background thread, so the GUI stays fully responsive (panning,
    zooming, other plugins, a real Cancel button) for the entire run.
"""

import os
import math
import sqlite3
import logging
import traceback
import threading
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from qgis.core import (
    Qgis,
    QgsProject,
    QgsRectangle,
    QgsPointXY,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsMapSettings,
    QgsMapRendererCustomPainterJob,
    QgsMessageLog,
    QgsTileMatrix,
    QgsTileXYZ,
    QgsExpressionContext,
    QgsExpressionContextScope,
    QgsExpressionContextUtils,
    QgsLabelingEngineSettings,
    QgsVectorLayer,
    QgsFeatureRequest,
)
try:
    from qgis.core import QgsTask, QgsApplication
except ImportError:  # pragma: no cover - only missing in non-QGIS test environments
    QgsTask = None
    QgsApplication = None
from qgis.PyQt.QtCore import QSize, QBuffer, QIODevice
from qgis.PyQt.QtGui import QImage, QPainter, QColor

TILE_SIZE = 256
EARTH_CIRCUMFERENCE_M = 40075016.6856
METERS_PER_DEGREE_EQUATOR = EARTH_CIRCUMFERENCE_M / 360.0
INCH_METERS = 0.0254
QIMAGE_NATIVE_FORMATS = {"PNG", "JPEG", "JPG", "WEBP"}
LOG_TAG = "XyzGpkgExporter"

# This custom tile matrix matches GoogleCRS84Quad: a zoom-0 matrix is 2
# columns x 1 row spanning the full globe (-180,-90 : 180,90), so a single
# zoom-0 tile is exactly 90 degrees wide/tall, and every deeper zoom's tile
# width is that 90 degrees halved once per zoom level (strict quadtree).
# TileMatrixSet uses these to build each QgsTileMatrix; TileRenderer uses
# ZOOM0_TILE_WIDTH_DEGREES once to seed its scale-at-zoom-0 baseline, then
# only ever divides by 2**zoom afterwards - see TileRenderer._scale_for_zoom.
TILE_MATRIX_TILE_ORIGIN = QgsPointXY(-180.0, 90.0)
TILE_MATRIX_Z0_DIMENSION_DEGREES = 180.0
TILE_MATRIX_ROOT_WIDTH = 2
TILE_MATRIX_ROOT_HEIGHT = 1
ZOOM0_TILE_WIDTH_DEGREES = TILE_MATRIX_Z0_DIMENSION_DEGREES / TILE_MATRIX_ROOT_WIDTH

# Constant value-objects reused across every tile render instead of being
# reconstructed millions of times. Both are read-only from this module's
# point of view (only ever passed into Qt setters, which copy the value),
# so sharing a single instance across threads is safe.
_TILE_QSIZE = QSize(TILE_SIZE, TILE_SIZE)
_TRANSPARENT_COLOR = QColor(0, 0, 0, 0)


class _QgsMessageLogHandler(logging.Handler):
    """Routes standard `logging` records into QGIS's Log Messages panel so
    diagnostics are visible even when the exporter runs on a background
    QgsTask thread (where a plain StreamHandler to stderr is invisible)."""

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.ERROR:
            level = Qgis.MessageLevel.Critical
        elif record.levelno >= logging.WARNING:
            level = Qgis.MessageLevel.Warning
        else:
            level = Qgis.MessageLevel.Info
        try:
            QgsMessageLog.logMessage(self.format(record), LOG_TAG, level)
        except Exception:
            pass


def _make_logger() -> logging.Logger:
    logger = logging.getLogger(LOG_TAG)
    if not any(isinstance(h, _QgsMessageLogHandler) for h in logger.handlers):
        handler = _QgsMessageLogHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def _equatorial_scale_denominator(extent_width_degrees: float, paper_width_m: float) -> float:
    """Equator-based scale denominator, computed independently of the
    project's "scale calculation method" setting so that rule-based
    symbology / label / layer visibility thresholds evaluate identically
    for every tile regardless of the latitude band it covers.

    `paper_width_m` is passed in pre-computed (output width in metres at
    the fixed tile size/dpi for this export) rather than derived here,
    since it is invariant for the whole export and was previously
    recomputed with the same float division on every single tile."""
    if paper_width_m <= 0:
        return 0.0
    return (extent_width_degrees * METERS_PER_DEGREE_EQUATOR) / paper_width_m


def _source_lock_key(layer) -> str:
    """Identify the physical file backing a layer, so that concurrent
    first-opens of the *same* GeoPackage file (typical after running
    QGIS's "Package Layers" tool, which puts every project layer into one
    physical .gpkg) can be serialized against each other without
    serializing unrelated layers/files.

    GeoPackage/OGR source URIs look like '/path/to/file.gpkg|layername=x';
    everything after the first '|' is provider metadata, not part of the
    file path, so it is stripped before using the path as a lock key.
    """
    try:
        provider = layer.dataProvider()
        uri = provider.dataSourceUri() if provider is not None else layer.source()
    except Exception:
        uri = layer.source()
    path = (uri or "").split("|", 1)[0].strip()
    if not path:
        return uri or repr(id(layer))
    return os.path.normcase(os.path.normpath(path))


def _compute_worker_count(max_cpu_percent: int) -> int:
    """Derive a ThreadPoolExecutor worker count from a CPU percentage
    ceiling, maximizing throughput while avoiding excessive contention on
    the single serialized GeoPackage write path."""
    pct = max(1, min(100, max_cpu_percent))
    cpu_count = os.cpu_count() or 1
    workers = max(1, math.floor(cpu_count * (pct / 100.0)))
    return workers


def _encode_via_gdal(image: QImage, driver_name: str, quality: int) -> bytes:
    """Fallback encoder for raster formats not natively supported by
    QImage (e.g. JPEG2000), routed through an in-memory GDAL dataset.
    Not a hot path for the common PNG/JPEG/WEBP export case."""
    from osgeo import gdal
    import numpy as np

    img = image.convertToFormat(QImage.Format.Format_RGBA8888)
    width, height = img.width(), img.height()
    ptr = img.bits()
    ptr.setsize(img.sizeInBytes())
    arr = np.frombuffer(bytes(ptr), dtype=np.uint8).reshape((height, width, 4))

    mem_ds = gdal.GetDriverByName("MEM").Create("", width, height, 4, gdal.GDT_Byte)
    for band_index in range(4):
        mem_ds.GetRasterBand(band_index + 1).WriteArray(arr[:, :, band_index])

    out_driver_name = "JP2OpenJPEG" if driver_name in ("JPEG2000", "JP2OPENJPEG") else driver_name
    creation_options = []
    if out_driver_name == "JP2OpenJPEG":
        creation_options = [f"QUALITY={max(1, quality)}"]

    vsi_path = f"/vsimem/xyz_tile_{threading.get_ident()}_{id(img)}.tmp"
    out_ds = gdal.GetDriverByName(out_driver_name).CreateCopy(vsi_path, mem_ds, options=creation_options)
    if out_ds is None:
        mem_ds = None
        raise RuntimeError(f"GDAL driver '{out_driver_name}' failed to encode tile")
    out_ds.FlushCache()

    handle = gdal.VSIFOpenL(vsi_path, "rb")
    gdal.VSIFSeekL(handle, 0, 2)
    size = gdal.VSIFTellL(handle)
    gdal.VSIFSeekL(handle, 0, 0)
    data = gdal.VSIFReadL(1, size, handle)
    gdal.VSIFCloseL(handle)
    gdal.Unlink(vsi_path)
    out_ds = None
    mem_ds = None
    return bytes(data)


class TileMatrixSet:
    """Builds and caches one QgsTileMatrix per zoom level. This is the
    single source of truth for every tile bound calculation in this file -
    never recompute tile extents via independent zoom/row/col math.

    Matrices are stored in a flat list indexed by `zoom - min_zoom` rather
    than a dict, since the zoom range is always contiguous: list indexing
    is a direct array access (no hashing), which is both faster and more
    cache-friendly than a dict lookup, and this is on the per-tile hot
    path via `tile_extent()`."""

    def __init__(self, crs: QgsCoordinateReferenceSystem, min_zoom: int, max_zoom: int):
        self._crs = crs
        self._min_zoom = min_zoom
        self._tile_origin = TILE_MATRIX_TILE_ORIGIN
        # z0Dimension is the side length (map units) of ONE root tile at zoom 0,
        # not a tile count. 180.0 degrees x 2 columns x 1 row = full globe
        # (-180,-90 : 180,90) at every zoom level, with every tile perfectly
        # square (strict 1:1 aspect ratio), matching GoogleCRS84Quad.
        self._z0_dimension = TILE_MATRIX_Z0_DIMENSION_DEGREES
        self._root_matrix_width = TILE_MATRIX_ROOT_WIDTH
        self._root_matrix_height = TILE_MATRIX_ROOT_HEIGHT
        self._matrices: List[QgsTileMatrix] = [
            QgsTileMatrix.fromCustomDef(
                zoom, self._crs, self._tile_origin,
                self._z0_dimension, self._root_matrix_width, self._root_matrix_height,
            )
            for zoom in range(min_zoom, max_zoom + 1)
        ]
        # Each matrix's dimensions are invariant once built; cache them
        # once instead of re-invoking matrixWidth()/matrixHeight() (sip
        # method calls) on every access.
        self._dims: List[Tuple[int, int]] = [(m.matrixWidth(), m.matrixHeight()) for m in self._matrices]

    def matrix(self, zoom: int) -> QgsTileMatrix:
        return self._matrices[zoom - self._min_zoom]

    def matrix_dims(self, zoom: int) -> Tuple[int, int]:
        return self._dims[zoom - self._min_zoom]

    def tile_extent(self, zoom: int, col: int, row: int) -> QgsRectangle:
        # tileExtent() takes a QgsTileXYZ (column, row, zoom) rather than
        # separate ints.
        return self._matrices[zoom - self._min_zoom].tileExtent(QgsTileXYZ(col, row, zoom))


class OccupancyIndex:
    """Precomputed set of tile coordinates that actually contain rendered
    content, at every requested zoom level. The tile scheduler in
    XyzGpkgExporter iterates *only* these tiles - there is no walk over
    the full zoom/col/row grid and no per-tile emptiness check at render
    time, so tiles with nothing to render are never even iterated, let
    alone rendered.

    A cheaper whole-layer-bounding-box test was tried first and rejected:
    for a large export extent containing few, scattered features (e.g. a
    handful of polygon outlines/lines/labels over many square kilometres),
    a layer's overall bounding box covers almost the entire export area
    even though the actual rendered content only touches a small fraction
    of it, and that mismatch gets worse every zoom level (tile count grows
    4x per zoom, but the genuinely-occupied fraction of the bbox does
    not) - this is what turned "a few minutes" into "hours" going from one
    zoom level to the next.

    Instead, this builds an exact occupied-tile set from every feature's
    own geometry bounding box (not the layer's overall extent), computed
    once at the *finest* requested zoom level via
    QgsTileMatrix.tileRangeFromExtent() (the same single-source-of-truth
    tile math used everywhere else in this file), padded outward by
    `tile_buffer` tiles to account for rendered content - symbol size,
    line width, label placement - extending beyond a feature's bare
    geometry bounds, and clamped to the requested export extent. Coarser
    zoom levels are then derived for free by mapping each occupied (col,
    row) down to its parent (col // 2, row // 2): this tile matrix is a
    strict quadtree (each zoom's matrix dimensions are exactly double the
    previous one in both directions), so a coarser tile is occupied iff at
    least one of its finer descendants is - this is exact, not an
    approximation, and costs O(occupied tile count) once rather than
    O(feature count) per zoom level.

    Layers without per-feature geometry to inspect (rasters, or any vector
    layer whose feature scan fails or finds nothing within the export
    extent) do NOT get their whole extent folded into the occupied set as
    part of the normal per-layer pass. A raster background (e.g. an
    orthophoto) legitimately covers its entire extent with real content,
    but letting that force every tile in that extent into the occupied set
    would defeat the entire point of this class for the common "sparse
    vector overlay on a wide raster basemap" project - the raster would
    single-handedly reinstate the "render nearly every tile in the bbox"
    behaviour this class exists to avoid. Instead, area-covering layers are
    only used as a last-resort fallback, applied once, after every layer
    has been scanned, and only if literally nothing else contributed any
    occupied tiles at all (e.g. a project with no vector layers) - so a
    pure-basemap export still produces something, but a mixed project's
    tile count is driven entirely by where the vector data actually is.

    Fails loudly (rather than silently) if no usable occupied tile could
    be built at all, since that previously caused every tile in the
    export to be misclassified as empty, producing a schema-only
    GeoPackage."""

    def __init__(
        self,
        project: QgsProject,
        layers: List,
        dest_crs: QgsCoordinateReferenceSystem,
        tile_matrix_set: "TileMatrixSet",
        export_extent: QgsRectangle,
        min_zoom: int,
        max_zoom: int,
        logger: logging.Logger,
        tile_buffer: int = 1,
    ):
        self._min_zoom = min_zoom
        self._max_zoom = max_zoom
        transform_context = project.transformContext()
        finest_matrix = tile_matrix_set.matrix(max_zoom)
        finest_w, finest_h = tile_matrix_set.matrix_dims(max_zoom)

        # Clamp bounds derived once from the caller's actual export
        # extent (not the full -180/-90/180/90 tile matrix): every mark
        # operation below clips into this box, so the resulting occupied
        # set can never contain a tile outside what was actually
        # requested, and the scheduler can iterate it directly with no
        # further extent filtering needed.
        export_range = finest_matrix.tileRangeFromExtent(export_extent)
        if not export_range.isValid():
            raise RuntimeError(
                "OccupancyIndex: the export extent does not intersect the tile matrix at all - "
                "nothing could possibly be rendered. Check the requested extent/zoom range."
            )
        clamp_col_min = max(0, export_range.startColumn())
        clamp_col_max = min(finest_w - 1, export_range.endColumn())
        clamp_row_min = max(0, export_range.startRow())
        clamp_row_max = min(finest_h - 1, export_range.endRow())
        clamp_bounds = (clamp_col_min, clamp_col_max, clamp_row_min, clamp_row_max)

        occupied: set = set()
        area_extents: List[QgsRectangle] = []
        usable_layers = 0
        layer_count = 0

        for layer in layers:
            layer_count += 1
            try:
                if layer is None or not layer.isValid():
                    logger.warning("OccupancyIndex: layer '%s' invalid or None, skipping",
                                   layer.name() if layer else "?")
                    continue
                layer_extent = layer.extent()
                if layer_extent is None or layer_extent.isEmpty():
                    logger.warning("OccupancyIndex: layer '%s' has empty extent, skipping", layer.name())
                    continue

                src_crs = layer.crs()
                transform = QgsCoordinateTransform(src_crs, dest_crs, transform_context) if src_crs != dest_crs else None
                layer_extent_dest = transform.transformBoundingBox(layer_extent) if transform is not None else layer_extent

                if isinstance(layer, QgsVectorLayer) and layer.isSpatial():
                    added = self._mark_features(
                        layer, transform, transform_context, finest_matrix,
                        export_extent, clamp_bounds, tile_buffer, occupied, logger,
                    )
                    if not added:
                        # No features intersected the export extent (or
                        # the scan failed) - this vector layer contributes
                        # nothing here. Its extent is still recorded as a
                        # candidate area extent (below), in case NOTHING
                        # in the whole project ends up contributing any
                        # occupied tile at all.
                        area_extents.append(layer_extent_dest)
                else:
                    # Raster / non-geometry layer: deliberately NOT folded
                    # into the occupied set here - see class docstring.
                    # It only becomes a fallback candidate, used at most
                    # once, after every layer has been scanned.
                    area_extents.append(layer_extent_dest)

                usable_layers += 1
            except Exception as exc:
                logger.warning(
                    "OccupancyIndex: skipping layer '%s' due to error: %s\n%s",
                    layer.name() if layer else "?", exc, traceback.format_exc(),
                )

        if layer_count == 0:
            logger.warning("OccupancyIndex: project has no layers at all - nothing will be rendered.")
        elif usable_layers == 0:
            raise RuntimeError(
                "OccupancyIndex: no usable layers could be read from any of the "
                f"{layer_count} project layer(s). Every tile would be misclassified as empty, "
                "producing a schema-only GeoPackage. Check the layers' CRS/extent validity."
            )
        elif not occupied:
            # No vector layer contributed a single occupied tile (e.g. a
            # purely raster/basemap project, or one where every vector
            # layer's feature scan failed) - fall back to the union of
            # area-layer extents just this once, so a legitimate
            # raster-only export still produces something instead of
            # raising or silently producing nothing.
            logger.warning(
                "OccupancyIndex: no vector layer contributed any occupied tile - falling back to "
                "%d area-layer extent(s) as a last resort", len(area_extents),
            )
            for area_extent in area_extents:
                self._mark_extent(area_extent, finest_matrix, clamp_bounds, occupied)
            if not occupied:
                raise RuntimeError(
                    "OccupancyIndex: no usable occupied tiles could be built from any of the "
                    f"{layer_count} project layer(s), including the area-extent fallback. Every "
                    "tile would be misclassified as empty, producing a schema-only GeoPackage. "
                    "Check the layers' CRS/extent validity."
                )
            logger.info(
                "OccupancyIndex: %d occupied tile(s) at zoom %d built from area-extent fallback only",
                len(occupied), max_zoom,
            )
        else:
            logger.info(
                "OccupancyIndex: %d occupied tile(s) at zoom %d built from vector feature geometry "
                "(%d area/raster layer(s) present but excluded from occupancy - see class docstring)",
                len(occupied), max_zoom, len(area_extents),
            )

        # Fold the finest-zoom occupied set up to every coarser zoom by
        # quadtree parent aggregation - see class docstring. Stored as a
        # list indexed by `zoom - min_zoom`, matching TileMatrixSet, so the
        # scheduler can iterate `occupied_tiles(zoom)` directly for every
        # zoom without recomputing anything.
        levels = [None] * (max_zoom - min_zoom + 1)
        levels[max_zoom - min_zoom] = occupied
        current = occupied
        for zoom in range(max_zoom - 1, min_zoom - 1, -1):
            current = {(c >> 1, r >> 1) for (c, r) in current}
            levels[zoom - min_zoom] = current
        self._occupied_by_zoom: List[set] = levels
        self.total_tile_count = sum(len(s) for s in levels)
        for zoom in range(min_zoom, max_zoom + 1):
            logger.info("Zoom %d: %d occupied tile(s) scheduled", zoom, len(levels[zoom - min_zoom]))

    @staticmethod
    def _clip_to_bounds(start_col: int, end_col: int, start_row: int, end_row: int, clamp_bounds: Tuple[int, int, int, int]):
        clamp_col_min, clamp_col_max, clamp_row_min, clamp_row_max = clamp_bounds
        start_col = max(clamp_col_min, start_col)
        end_col = min(clamp_col_max, end_col)
        start_row = max(clamp_row_min, start_row)
        end_row = min(clamp_row_max, end_row)
        if start_col > end_col or start_row > end_row:
            return None
        return start_col, end_col, start_row, end_row

    @classmethod
    def _mark_extent(cls, extent_dest: QgsRectangle, finest_matrix: QgsTileMatrix, clamp_bounds: Tuple[int, int, int, int], occupied: set) -> None:
        tile_range = finest_matrix.tileRangeFromExtent(extent_dest)
        if not tile_range.isValid():
            return
        clipped = cls._clip_to_bounds(
            tile_range.startColumn(), tile_range.endColumn(), tile_range.startRow(), tile_range.endRow(), clamp_bounds,
        )
        if clipped is None:
            return
        start_col, end_col, start_row, end_row = clipped
        for col in range(start_col, end_col + 1):
            for row in range(start_row, end_row + 1):
                occupied.add((col, row))

    def _mark_features(
        self,
        layer,
        transform: Optional[QgsCoordinateTransform],
        transform_context,
        finest_matrix: QgsTileMatrix,
        export_extent: QgsRectangle,
        clamp_bounds: Tuple[int, int, int, int],
        tile_buffer: int,
        occupied: set,
        logger: logging.Logger,
    ) -> bool:
        """Scans every feature intersecting the export extent, marking the
        (padded, clamped) occupied tile block for each feature's own
        geometry bounding box. Returns True if at least one feature was
        processed (even if it produced zero occupied tiles, e.g. a
        degenerate geometry) so the caller knows a real scan happened."""
        src_crs = layer.crs()
        try:
            request = QgsFeatureRequest()
            request.setSubsetOfAttributes([])
            # Restrict the scan to the actual export extent (not the whole
            # globe-spanning tile matrix), in the layer's own CRS, so the
            # provider's spatial index does the heavy lifting and we never
            # even look at features far outside what's being exported.
            if transform is not None:
                reverse_transform = QgsCoordinateTransform(finest_matrix.crs(), src_crs, transform_context)
                request.setFilterRect(reverse_transform.transformBoundingBox(export_extent))
            else:
                request.setFilterRect(export_extent)
        except Exception:
            request = QgsFeatureRequest()
            request.setSubsetOfAttributes([])

        processed = False
        try:
            for feature in layer.getFeatures(request):
                geom = feature.geometry()
                if geom is None or geom.isEmpty():
                    continue
                processed = True
                feat_extent = geom.boundingBox()
                if feat_extent is None or feat_extent.isEmpty():
                    continue
                feat_extent_dest = transform.transformBoundingBox(feat_extent) if transform is not None else feat_extent
                tile_range = finest_matrix.tileRangeFromExtent(feat_extent_dest)
                if not tile_range.isValid():
                    continue
                clipped = self._clip_to_bounds(
                    tile_range.startColumn() - tile_buffer, tile_range.endColumn() + tile_buffer,
                    tile_range.startRow() - tile_buffer, tile_range.endRow() + tile_buffer,
                    clamp_bounds,
                )
                if clipped is None:
                    continue
                start_col, end_col, start_row, end_row = clipped
                for col in range(start_col, end_col + 1):
                    for row in range(start_row, end_row + 1):
                        occupied.add((col, row))
        except Exception as exc:
            logger.warning(
                "OccupancyIndex: feature scan failed for layer '%s', falling back to whole-layer extent (%s)",
                layer.name(), exc,
            )
            return False
        return processed

    def occupied_tiles(self, zoom: int) -> set:
        """The set of (col, row) tuples with real content at this zoom."""
        return self._occupied_by_zoom[zoom - self._min_zoom]


class GeoPackageWriter:
    """Writes a standards-compliant gpkg_tile_matrix / gpkg_tile_matrix_set
    tiled raster GeoPackage.

    Tile inserts are batched *per worker thread*: each thread accumulates
    rendered tiles into its own pending list and only touches the shared
    SQLite connection (which must be single-writer-at-a-time, hence the
    lock) once that thread's pending list reaches `batch_size`, instead of
    acquiring the writer's lock once per tile as before. This cuts lock
    acquisitions on the hot path from O(tile count) to O(tile count /
    batch_size), which matters a great deal once thread counts and tile
    counts both scale up. Row order within the table is irrelevant to the
    final GeoPackage content (unique zoom/col/row index + INSERT OR
    REPLACE), so batching/interleaving across threads never changes output.

    Uses rollback-journal mode (not WAL): with batched, lock-serialized
    writes there is no concurrent-writer scenario WAL is needed for, and
    WAL leaves tile data stranded in a `.gpkg-wal` side file whenever the
    final checkpoint doesn't run cleanly - producing a schema-only-sized
    main `.gpkg` file. `close()` still issues an explicit checkpoint as a
    safety net in case WAL was left enabled by an external PRAGMA."""

    def __init__(
        self,
        path: str,
        table_name: str,
        tile_matrix_set: TileMatrixSet,
        min_zoom: int,
        max_zoom: int,
        matrix_set_extent: QgsRectangle,
        logger: logging.Logger,
        batch_size: int = 2000,
    ):
        self._table = table_name
        self._lock = threading.Lock()
        self._batch_size = batch_size
        self._logger = logger

        # Per-thread pending-tile lists. Each worker thread lazily creates
        # its own list (registered once, under `_registry_lock`, into
        # `_pending_lists` so `flush()`/`close()` can drain any partial
        # batches left over at the end of the export); after that one-time
        # setup, appending to it from `write_tile()` needs no lock at all.
        self._registry_lock = threading.Lock()
        self._pending_lists: List[list] = []
        self._thread_local = threading.local()

        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=DELETE")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA temp_store=MEMORY")
        # Larger page cache and memory-mapped I/O reduce disk round-trips
        # for the large batched commits this writer performs; neither
        # changes the resulting file content, only write throughput.
        self._conn.execute("PRAGMA cache_size=-131072")   # ~128MB page cache
        try:
            self._conn.execute("PRAGMA mmap_size=268435456")  # 256MB, best-effort
        except sqlite3.Error:
            pass
        self._insert_sql = f"INSERT OR REPLACE INTO {self._table} (zoom_level, tile_column, tile_row, tile_data) VALUES (?,?,?,?)"
        self._init_schema(tile_matrix_set, min_zoom, max_zoom, matrix_set_extent)

    def _init_schema(self, tile_matrix_set, min_zoom, max_zoom, matrix_set_extent) -> None:
        cur = self._conn.cursor()
        cur.executescript(
            """
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
            """
        )
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {self._table} ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "zoom_level INTEGER NOT NULL, "
            "tile_column INTEGER NOT NULL, "
            "tile_row INTEGER NOT NULL, "
            "tile_data BLOB NOT NULL)"
        )
        cur.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{self._table}_zyx "
            f"ON {self._table}(zoom_level, tile_column, tile_row)"
        )

        cur.execute(
            "INSERT OR IGNORE INTO gpkg_spatial_ref_sys "
            "(srs_name, srs_id, organization, organization_coordsys_id, definition, description) VALUES "
            "('Undefined Cartesian SRS', -1, 'NONE', -1, 'undefined', 'undefined cartesian coordinate reference system'),"
            "('Undefined Geographic SRS', 0, 'NONE', 0, 'undefined', 'undefined geographic coordinate reference system'),"
            "('WGS 84 geodetic', 4326, 'EPSG', 4326, "
            "'GEOGCS[\"WGS 84\",DATUM[\"WGS_1984\",SPHEROID[\"WGS 84\",6378137,298.257223563]],"
            "PRIMEM[\"Greenwich\",0],UNIT[\"degree\",0.0174532925199433]]', "
            "'longitude/latitude WGS 84')"
        )

        cur.execute(
            "INSERT OR REPLACE INTO gpkg_tile_matrix_set (table_name, srs_id, min_x, min_y, max_x, max_y) "
            "VALUES (?, 4326, ?, ?, ?, ?)",
            (
                self._table,
                matrix_set_extent.xMinimum(),
                matrix_set_extent.yMinimum(),
                matrix_set_extent.xMaximum(),
                matrix_set_extent.yMaximum(),
            ),
        )
        cur.execute(
            "INSERT OR REPLACE INTO gpkg_contents "
            "(table_name, data_type, identifier, description, min_x, min_y, max_x, max_y, srs_id) "
            "VALUES (?, 'tiles', ?, 'XYZ raster export', ?, ?, ?, ?, 4326)",
            (
                self._table,
                self._table,
                matrix_set_extent.xMinimum(),
                matrix_set_extent.yMinimum(),
                matrix_set_extent.xMaximum(),
                matrix_set_extent.yMaximum(),
            ),
        )

        tile_matrix_rows = []
        for zoom in range(min_zoom, max_zoom + 1):
            matrix_w, matrix_h = tile_matrix_set.matrix_dims(zoom)
            zoom_extent = tile_matrix_set.matrix(zoom).extent()
            pixel_x_size = (zoom_extent.width() / matrix_w) / TILE_SIZE
            pixel_y_size = (zoom_extent.height() / matrix_h) / TILE_SIZE
            tile_matrix_rows.append(
                (self._table, zoom, matrix_w, matrix_h, TILE_SIZE, TILE_SIZE, pixel_x_size, pixel_y_size)
            )
        cur.executemany(
            "INSERT OR REPLACE INTO gpkg_tile_matrix "
            "(table_name, zoom_level, matrix_width, matrix_height, tile_width, tile_height, pixel_x_size, pixel_y_size) "
            "VALUES (?,?,?,?,?,?,?,?)",
            tile_matrix_rows,
        )
        self._conn.commit()

    def _thread_pending(self) -> list:
        tl = self._thread_local
        pending = getattr(tl, "pending", None)
        if pending is None:
            pending = []
            tl.pending = pending
            with self._registry_lock:
                self._pending_lists.append(pending)
        return pending

    def write_tile(self, zoom: int, col: int, row: int, data: bytes) -> None:
        # Rendering stays fully parallel; appending to this thread's own
        # pending list needs no lock at all. The shared connection/lock is
        # only touched once every `batch_size` tiles (see `_drain`).
        pending = self._thread_pending()
        pending.append((zoom, col, row, data))
        if len(pending) >= self._batch_size:
            self._drain(pending)

    def _drain(self, pending: list) -> None:
        if not pending:
            return
        with self._lock:
            cur = self._conn.cursor()
            cur.executemany(self._insert_sql, pending)
            self._conn.commit()
        batch_len = len(pending)
        pending.clear()
        self._logger.info("GeoPackageWriter: flushed batch of %d tile(s) to disk", batch_len)

    def flush(self) -> None:
        # Only reached after all worker threads have finished submitting
        # tiles (called from export()'s finally block once the thread pool
        # has fully drained), so it is safe to iterate `_pending_lists`
        # without the registry lock here.
        for pending in self._pending_lists:
            self._drain(pending)

    def close(self) -> None:
        self.flush()
        # Safety net: force any residual WAL content (e.g. if journal_mode
        # was overridden externally) to be checkpointed into the main file
        # before closing, so the .gpkg is never left schema-only-sized
        # while data sits stranded in a .gpkg-wal side file.
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as exc:
            self._logger.warning("GeoPackageWriter: wal_checkpoint failed (likely no WAL active): %s", exc)
        self._conn.commit()
        row_count = self._conn.execute(f"SELECT COUNT(*) FROM {self._table}").fetchone()[0]
        self._logger.info("GeoPackageWriter: final tile row count in '%s' = %d", self._table, row_count)
        self._conn.close()


class TileRenderer:
    """Renders a single tile from the live QGIS project, rebuilding
    QgsMapSettings extent/size/dpi per tile and reading back the resulting
    scale fresh every time (never reusing a stale cross-zoom scale).

    Each worker thread gets its own cloned set of layers via thread-local
    storage: QgsMapRendererCustomPainterJob and most data providers are
    not guaranteed safe to drive concurrently from multiple threads
    against the same shared QgsMapLayer instances, and sharing them was a
    prior cause of every tile silently failing to render.

    Every other per-render object (QgsMapSettings, the QImage/QPainter
    render target, the QIODevice used for in-format encoding, and the
    QgsExpressionContext/Scope carrying `map_scale`) is likewise allocated
    once per thread and reused/reset for every tile: everything the
    renderer reads from these objects is still explicitly re-set on every
    single tile before use, so reuse is behaviourally identical to fresh
    construction - it only removes the repeated allocation/deallocation
    cost, which matters at multi-million-tile scale."""

    def __init__(
        self,
        project: QgsProject,
        dpi: int,
        tile_format: str,
        quality: int,
        layers: List,
        style_overrides: Dict[str, str],
        expr_context: QgsExpressionContext,
        labeling_settings: QgsLabelingEngineSettings,
        logger: logging.Logger,
    ):
        self._project = project
        self._dpi = dpi
        self._format = tile_format.upper()
        self._quality = quality
        self._base_layers = layers
        self._style_overrides = style_overrides
        self._expr_context = expr_context
        # Built once on the main thread by XyzGpkgExporter and only ever
        # read (never reconstructed) from worker threads - see module
        # docstring. Assigning this pre-built value object onto each
        # tile's QgsMapSettings is cheap and thread-safe.
        self._labeling_settings = labeling_settings
        self._logger = logger
        self._thread_local = threading.local()

        # Guards the *registry* of per-source-file locks below (not the
        # clone itself); building/looking up a lock is effectively
        # instantaneous, so this never becomes a contention point.
        self._clone_lock_registry_guard = threading.Lock()
        self._clone_locks: Dict[str, threading.Lock] = {}

        # Encoding path is fully determined by the (fixed, per-export)
        # output format, so resolve it once instead of branching and
        # re-uppercasing an already-uppercase string on every tile.
        self._is_native_format = self._format in QIMAGE_NATIVE_FORMATS
        self._qt_save_format = "JPG" if self._format == "JPEG" else self._format

        # Output size/dpi are fixed for the whole export, so the
        # dpi-dependent "paper" width in metres is invariant and computed
        # once here. The scale denominator at zoom 0 is then computed
        # exactly ONCE from that (the only place `_equatorial_scale_denominator`
        # / EARTH_CIRCUMFERENCE_M are used at all) - every other zoom's
        # scale is just that single number divided by 2**zoom, since this
        # tile matrix is an exact quadtree: a tile at zoom z is by
        # definition the zoom-0 tile halved z times, so its scale
        # denominator (which is directly proportional to tile width) halves
        # right along with it. This replaces the old "recompute the
        # equatorial formula per zoom" with "one baseline calculation for
        # the whole export, then integer-power-of-two division" - the
        # actual per-tile/per-zoom cost was already O(1) either way (both
        # are cached in `_scale_cache`), so this is a clarity/redundancy
        # fix rather than a hot-path optimisation.
        self._paper_width_m = (TILE_SIZE / float(dpi)) * INCH_METERS
        self._scale_zoom0 = _equatorial_scale_denominator(ZOOM0_TILE_WIDTH_DEGREES, self._paper_width_m)
        self._scale_cache: Dict[int, float] = {}

        # Cache flag enum members once: three fewer nested attribute
        # lookups (QgsMapSettings.Flag.X) per tile.
        self._flag_antialiasing = QgsMapSettings.Flag.Antialiasing
        self._flag_render_tile = QgsMapSettings.Flag.RenderMapTile
        self._flag_advanced_effects = QgsMapSettings.Flag.UseAdvancedEffects

    def _get_clone_lock(self, source_key: str) -> threading.Lock:
        with self._clone_lock_registry_guard:
            lock = self._clone_locks.get(source_key)
            if lock is None:
                lock = threading.Lock()
                self._clone_locks[source_key] = lock
            return lock

    def _thread_layers(self) -> List:
        tl = self._thread_local
        layers = getattr(tl, "layers", None)
        if layers is None:
            layers = []
            for lyr in self._base_layers:
                # Serialize only the first-time clone/open of layers that
                # share the same physical GeoPackage file. QGIS's
                # Package Layers tool commonly produces a project where
                # many/all layers point at one .gpkg; if several worker
                # threads clone (and thereby open a fresh provider
                # connection to) that same file at the same instant, OGR's
                # SQLite backend can hit lock contention that never
                # resolves and the export hangs forever. Locking here only
                # affects this one-time per-thread setup step - never the
                # per-tile render loop - so steady-state throughput is
                # unaffected. Different source files still clone fully in
                # parallel, since each gets its own lock.
                lock = self._get_clone_lock(_source_lock_key(lyr))
                with lock:
                    try:
                        layers.append(lyr.clone())
                    except Exception as exc:
                        self._logger.warning(
                            "TileRenderer: failed to clone layer '%s' for thread %s, using shared instance (%s)",
                            lyr.name(), threading.get_ident(), exc,
                        )
                        layers.append(lyr)
            tl.layers = layers
        return layers

    def _thread_map_settings(self) -> QgsMapSettings:
        tl = self._thread_local
        settings = getattr(tl, "map_settings", None)
        if settings is None:
            settings = QgsMapSettings()
            tl.map_settings = settings
        return settings

    def _thread_render_target(self):
        tl = self._thread_local
        image = getattr(tl, "image", None)
        if image is None:
            image = QImage(TILE_SIZE, TILE_SIZE, QImage.Format.Format_ARGB32_Premultiplied)
            painter = QPainter()
            tl.image = image
            tl.painter = painter
        return image, tl.painter

    def _thread_expr_context(self):
        tl = self._thread_local
        ctx = getattr(tl, "expr_ctx", None)
        if ctx is None:
            scope = QgsExpressionContextScope()
            scope.setVariable("map_scale", 0.0, True)
            ctx = QgsExpressionContext(self._expr_context)
            ctx.appendScope(scope)
            tl.expr_scope = scope
            tl.expr_ctx = ctx
        return ctx, tl.expr_scope

    def _thread_buffer(self) -> QBuffer:
        tl = self._thread_local
        buf = getattr(tl, "buffer", None)
        if buf is None:
            buf = QBuffer()
        else:
            buf.close()
            buf.buffer().clear()
        tl.buffer = buf
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        return buf

    def _scale_for_zoom(self, zoom: int) -> float:
        cached = self._scale_cache.get(zoom)
        if cached is not None:
            return cached
        # Pure quadtree halving from the single zoom-0 baseline - no
        # extent/degrees lookup, no repeated call into the equatorial
        # formula. Deterministic value - a benign race between threads
        # computing the same zoom's scale for the first time
        # simultaneously is safe (last write wins, both writes are equal),
        # and avoids needing a lock on this per-tile-adjacent hot path.
        value = self._scale_zoom0 / (2 ** zoom)
        self._scale_cache[zoom] = value
        return value

    def _encode(self, image: QImage) -> bytes:
        if self._is_native_format:
            buf = self._thread_buffer()
            image.save(buf, self._qt_save_format, self._quality)
            data = bytes(buf.data())
            buf.close()
            return data
        return _encode_via_gdal(image, self._format, self._quality)

    def render_tile(self, zoom: int, extent: QgsRectangle, dest_crs: QgsCoordinateReferenceSystem) -> bytes:
        settings = self._thread_map_settings()
        settings.setDestinationCrs(dest_crs)
        settings.setLayers(self._thread_layers())
        if self._style_overrides:
            settings.setLayerStyleOverrides(self._style_overrides)
        settings.setBackgroundColor(_TRANSPARENT_COLOR)
        # Apply the shared, pre-built labeling engine settings (built once,
        # on the main thread, from the project's own settings with
        # UsePartialCandidates forced off) to this tile's map settings.
        # QgsMapSettings does not otherwise inherit the project's labeling
        # engine settings on its own, so without this, tiles would always
        # render with default engine settings regardless of the project
        # configuration - which is why labels were previously able to
        # cross tile boundaries even though the project-level flag was set.
        settings.setLabelingEngineSettings(self._labeling_settings)
        settings.setOutputSize(_TILE_QSIZE)
        settings.setOutputDpi(self._dpi)
        settings.setExtent(extent)
        settings.setFlag(self._flag_antialiasing, True)
        settings.setFlag(self._flag_render_tile, True)
        settings.setFlag(self._flag_advanced_effects, True)

        # Zoom-0-baseline-halved scale: identical for every tile within a
        # zoom level (uniform grid), a cache lookup after the first tile
        # of each zoom.
        equator_scale = self._scale_for_zoom(zoom)
        ctx, scale_scope = self._thread_expr_context()
        scale_scope.setVariable("map_scale", equator_scale, True)
        settings.setExpressionContext(ctx)

        image, painter = self._thread_render_target()
        image.fill(_TRANSPARENT_COLOR)
        painter.begin(image)
        try:
            job = QgsMapRendererCustomPainterJob(settings, painter)
            job.renderSynchronously()
        finally:
            painter.end()

        return self._encode(image)


class XyzGpkgExporter:
    """Exports the currently loaded QGIS project as raster XYZ tiles into a
    single GeoPackage. Single public entry point: `export()` / `run()` -
    prefer `run_in_background()` (a classmethod on this class) over
    calling either of those directly if you want the GUI to stay usable.

    Construction (`__init__`) only performs the small pieces of setup that
    must run on the main/GUI thread (resolving the active map theme/
    checked layers via `iface`, and constructing/mutating the project's
    QgsLabelingEngineSettings) - both fast, O(1) operations. The genuinely
    slow part - building the TileMatrixSet and, especially, the
    OccupancyIndex, which walks every feature of every rendered layer and
    gets slower the deeper the requested max zoom goes - is deferred to
    `_prepare()`, called at the very start of `export()` rather than in
    `__init__`. That split is what lets `run_in_background()` keep the GUI
    responsive even for a full zoom-0-through-18 export: constructing the
    exporter (on the calling/GUI thread) stays cheap, and all of the
    expensive work happens inside `export()`, which only ever runs on a
    QgsTask worker thread when reached via `run_in_background()`."""

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
    ):
        self._logger = _make_logger()
        self.project = QgsProject.instance()
        self.dest_crs = QgsCoordinateReferenceSystem("EPSG:4326")

        if extent is None:
            extent = self._canvas_extent_or_none()
        self.extent = extent if extent is not None else QgsRectangle(-180.0, -90.0, 180.0, 90.0)

        if min_zoom < 0 or max_zoom < min_zoom:
            raise ValueError("Invalid zoom range: max_zoom must be >= min_zoom >= 0")
        self.min_zoom = min_zoom
        self.max_zoom = max_zoom
        self.dpi = dpi
        self.max_cpu_percent = max(1, min(100, max_cpu_percent))
        self.tile_format = tile_format.upper()
        self.quality = max(0, min(100, quality))
        self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.output_path = os.path.join(self.output_dir, f"xyz_export_{timestamp}.gpkg")

        # Normalize the project's legacy scale-calc entry to Equator so any
        # residual canvas-side computation stays consistent with the
        # explicit per-tile equator scale computed in TileRenderer.
        self.project.writeEntry("Measure", "/ScaleCalcMethod", "AtEquator")

        # Build the labeling engine settings exactly once, here on the main
        # thread (constructing/mutating QgsLabelingEngineSettings touches
        # font/text-format resolution and is not safe to do from a worker
        # thread - see module docstring). Start from the project's own
        # settings, mirroring the tuple that already works when run from
        # the Python console, force partial/edge-truncated label
        # candidates off, and persist it back onto the project so the
        # project stays internally consistent. The resulting value object
        # is handed unchanged to every TileRenderer tile.
        labeling_settings = self.project.labelingEngineSettings()
        try:
            partial_candidates_flag = QgsLabelingEngineSettings.Flag.UsePartialCandidates
        except AttributeError:
            # Some QGIS point releases hoist the flag onto the class
            # itself rather than a nested Flag enum; this is the form
            # confirmed working from the Python console.
            partial_candidates_flag = QgsLabelingEngineSettings.UsePartialCandidates
        labeling_settings.setFlag(partial_candidates_flag, False)
        self.project.setLabelingEngineSettings(labeling_settings)
        self.labeling_settings = labeling_settings

        self._resolve_layers_and_theme()
        self._logger.info("Resolved %d layer(s) for rendering", len(self.layers))

        expr_context = QgsExpressionContext()
        expr_context.appendScope(QgsExpressionContextUtils.globalScope())
        expr_context.appendScope(QgsExpressionContextUtils.projectScope(self.project))
        self.expr_context = expr_context

        self.progress_callback = progress_callback
        self.should_cancel = should_cancel
        self._occupancy_tile_buffer = occupancy_tile_buffer

        # Deliberately NOT built here - see _prepare(). Everything above
        # this point touches iface/mapCanvas or QgsLabelingEngineSettings
        # and must run on the main/GUI thread, but is all O(1)/cheap, so
        # doing it synchronously in __init__ is not what was freezing the
        # GUI. TileMatrixSet/OccupancyIndex construction below, by
        # contrast, walks every feature of every rendered layer and is
        # genuinely slow at deep zoom levels (more, smaller tiles per
        # feature) - that is what must NOT run on the calling thread.
        self.tile_matrix_set: Optional["TileMatrixSet"] = None
        self.occupancy_index: Optional["OccupancyIndex"] = None
        self._prepared = False

        # Per-thread stats, aggregated once at the end of export() instead
        # of taking a shared lock on every single tile. See `_ThreadStats`
        # and `_thread_stats()`.
        self._stats_registry_lock = threading.Lock()
        self._stats_registry: List[_ThreadStats] = []
        self._stats_local = threading.local()
        self._tiles_written = 0
        self._tiles_skipped_empty = 0
        self._tiles_failed = 0

    def _prepare(self) -> None:
        """Builds the TileMatrixSet and OccupancyIndex - the genuinely
        slow, but thread-safe-to-defer, part of setting up an export.

        This is deliberately NOT part of __init__. `XyzGpkgExporter(...)`
        is what `run_in_background()` (and code calling it directly) runs
        synchronously on the calling thread; if this feature-scanning work
        lived in __init__, a deep zoom range (more, smaller tiles per
        feature) would still block the GUI for however long it takes to
        finish, even though the actual tile *rendering* was already
        correctly backgrounded. Calling this from the top of export()
        instead means it runs on whatever thread calls export() - the
        QgsTask worker thread when using run_in_background(), which is
        exactly where expensive, GUI-independent feature/geometry work
        belongs. Reading vector features and building plain Python/QGIS
        core geometry objects (no widget or labeling-engine-settings
        access) is safe from a background thread; this deferral relies on
        that split staying accurate, so do not add GUI/iface/canvas access
        into OccupancyIndex or TileMatrixSet."""
        if self._prepared:
            return

        self.tile_matrix_set = TileMatrixSet(self.dest_crs, self.min_zoom, self.max_zoom)
        for zoom in range(self.min_zoom, self.max_zoom + 1):
            self._logger.info(
                "Tile matrix zoom %d: extent=%s dims=%s",
                zoom, self.tile_matrix_set.matrix(zoom).extent().toString(),
                self.tile_matrix_set.matrix_dims(zoom),
            )

        # Built from the *actual* per-feature geometry of the layers that
        # will be rendered (not just their bounding boxes) - see
        # OccupancyIndex docstring for why this matters at high zoom
        # levels. `occupancy_tile_buffer` pads every feature's occupied
        # tile block by this many tiles in each direction to account for
        # rendered content (symbol size, line width, label placement)
        # extending beyond the bare geometry bounds; raise it if a project
        # uses unusually large point markers/label offsets and tiles near
        # real content start turning up unexpectedly empty.
        self.occupancy_index = OccupancyIndex(
            self.project, self.layers, self.dest_crs, self.tile_matrix_set, self.extent,
            self.min_zoom, self.max_zoom, self._logger, tile_buffer=self._occupancy_tile_buffer,
        )
        self._prepared = True

    def _canvas_extent_or_none(self) -> Optional[QgsRectangle]:
        try:
            from qgis.utils import iface

            if iface is None or iface.mapCanvas() is None:
                return None
            canvas = iface.mapCanvas()
            canvas_extent = canvas.extent()
            canvas_crs = canvas.mapSettings().destinationCrs()
            if canvas_crs != self.dest_crs:
                transform = QgsCoordinateTransform(canvas_crs, self.dest_crs, self.project.transformContext())
                canvas_extent = transform.transformBoundingBox(canvas_extent)
            return canvas_extent
        except Exception:
            return None

    def _resolve_layers_and_theme(self) -> None:
        theme_collection = self.project.mapThemeCollection()
        active_theme_name = None
        try:
            from qgis.utils import iface

            if iface is not None:
                canvas = iface.mapCanvas()
                bridge_theme = canvas.theme() if hasattr(canvas, "theme") else ""
                if bridge_theme and theme_collection.hasMapTheme(bridge_theme):
                    active_theme_name = bridge_theme
        except Exception:
            active_theme_name = None

        self.style_overrides: Dict[str, str] = {}
        if active_theme_name:
            self.layers = theme_collection.mapThemeVisibleLayers(active_theme_name)
            self.style_overrides = theme_collection.mapThemeStyleOverrides(active_theme_name)
        else:
            root = self.project.layerTreeRoot()
            self.layers = root.checkedLayers()

    def _iter_occupied_tiles(self):
        """Yields (zoom, col, row) directly from the precomputed
        OccupancyIndex - the only tiles ever produced are ones already
        known to contain real content. There is no walk over the full
        zoom/col/row grid and no per-tile emptiness check: for a large
        export extent with sparse features, the occupied set can be many
        orders of magnitude smaller than the full grid, so this scheduling
        step now costs O(occupied tiles) instead of O(every tile in the
        extent).

        Kept as a generator (rather than materializing one combined list)
        so that, combined with the bounded-in-flight submission pipeline
        in export(), peak memory stays proportional to worker count."""
        cancel_check_every = 256
        counter = 0
        for zoom in range(self.min_zoom, self.max_zoom + 1):
            for col, row in self.occupancy_index.occupied_tiles(zoom):
                counter += 1
                if self.should_cancel and counter % cancel_check_every == 0 and self.should_cancel():
                    return
                yield zoom, col, row

    def _thread_stats(self) -> "_ThreadStats":
        tl = self._stats_local
        stats = getattr(tl, "stats", None)
        if stats is None:
            stats = _ThreadStats()
            tl.stats = stats
            with self._stats_registry_lock:
                self._stats_registry.append(stats)
        return stats

    def _process_tile(self, renderer: TileRenderer, writer: GeoPackageWriter, zoom: int, col: int, row: int) -> None:
        # No emptiness check here: only tiles the OccupancyIndex already
        # knows are occupied are ever submitted (see _iter_occupied_tiles),
        # so every call here does real, necessary work.
        stats = self._thread_stats()
        try:
            tile_extent = self.tile_matrix_set.tile_extent(zoom, col, row)
            data = renderer.render_tile(zoom, tile_extent, self.dest_crs)
            if not data:
                self._logger.warning("Tile z=%d x=%d y=%d rendered but produced no encoded bytes", zoom, col, row)
                stats.skipped += 1
                return
            writer.write_tile(zoom, col, row, data)
            stats.written += 1
        except Exception as exc:
            stats.failed += 1
            self._logger.error(
                "Failed tile z=%d x=%d y=%d: %s\n%s", zoom, col, row, exc, traceback.format_exc()
            )

    def export(self) -> str:
        """Runs the export and returns the path to the produced GeoPackage."""
        self._logger.info("Starting export to %s", self.output_path)
        # Heavy, deferred setup - see _prepare() docstring for why this is
        # not in __init__. When called via run_in_background(), this
        # entire method (including this call) runs on a QgsTask worker
        # thread, never the GUI thread.
        self._prepare()
        writer = GeoPackageWriter(
            self.output_path,
            table_name="xyz_tiles",
            tile_matrix_set=self.tile_matrix_set,
            min_zoom=self.min_zoom,
            max_zoom=self.max_zoom,
            matrix_set_extent=self.tile_matrix_set.matrix(self.min_zoom).extent(),
            logger=self._logger,
        )
        renderer = TileRenderer(
            project=self.project,
            dpi=self.dpi,
            tile_format=self.tile_format,
            quality=self.quality,
            layers=self.layers,
            style_overrides=self.style_overrides,
            expr_context=self.expr_context,
            labeling_settings=self.labeling_settings,
            logger=self._logger,
        )

        worker_count = _compute_worker_count(self.max_cpu_percent)
        self._logger.info("Using %d worker threads", worker_count)
        # Exact, not an estimate: this is precisely the number of tiles
        # that will be submitted, since we now only ever iterate the
        # OccupancyIndex's occupied-tile sets.
        total_estimate = max(1, self.occupancy_index.total_tile_count)
        self._logger.info(
            "Tiles scheduled for rendering after occupancy filtering (across all zoom levels %d-%d): %d",
            self.min_zoom, self.max_zoom, total_estimate,
        )
        completed = 0
        # Bound how many tiles may be in flight (submitted-but-not-yet-
        # processed) at once, rather than submitting every tile up front.
        # With millions of tiles, eagerly submitting all of them would
        # hold millions of Future objects (and their queued call args) in
        # memory simultaneously; bounding the pipeline keeps peak memory
        # proportional to worker_count instead of total tile count.
        pipeline_depth = worker_count * 4
        future_level_failure = False

        try:
            with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="xyztile") as pool:
                tile_iter = self._iter_occupied_tiles()
                in_flight = set()

                def _submit_next() -> bool:
                    for zoom, col, row in tile_iter:
                        in_flight.add(pool.submit(self._process_tile, renderer, writer, zoom, col, row))
                        return True
                    return False

                submitted_any = False
                for _ in range(pipeline_depth):
                    if _submit_next():
                        submitted_any = True
                    else:
                        break

                if not submitted_any:
                    self._logger.error(
                        "No tiles were submitted for processing at all - check that the export extent "
                        "intersects the project layers and the configured zoom range."
                    )

                cancelled = False
                while in_flight:
                    done, in_flight = wait(in_flight, return_when=FIRST_COMPLETED)
                    for future in done:
                        try:
                            future.result()
                        except Exception as exc:
                            future_level_failure = True
                            self._logger.error("Tile task raised: %s\n%s", exc, traceback.format_exc())
                        completed += 1
                        if self.progress_callback:
                            self.progress_callback(completed, total_estimate)

                    if not cancelled and self.should_cancel and self.should_cancel():
                        cancelled = True
                        for pending in in_flight:
                            pending.cancel()
                        break

                    if not cancelled:
                        for _ in range(len(done)):
                            _submit_next()
        finally:
            writer.close()

        self._tiles_written = sum(s.written for s in self._stats_registry)
        self._tiles_skipped_empty = sum(s.skipped for s in self._stats_registry)
        self._tiles_failed = sum(s.failed for s in self._stats_registry) + (1 if future_level_failure else 0)

        self._logger.info(
            "Export complete: %d written, %d skipped (empty), %d failed -> %s",
            self._tiles_written,
            self._tiles_skipped_empty,
            self._tiles_failed,
            self.output_path,
        )
        if self._tiles_written == 0:
            self._logger.error(
                "Export produced ZERO written tiles (skipped=%d, failed=%d). The output GeoPackage "
                "will contain only schema, no tile data. Check the Log Messages panel above for the "
                "specific cause (empty-tile detector, render failures, or zero tiles submitted).",
                self._tiles_skipped_empty, self._tiles_failed,
            )
        return self.output_path

    def run(self) -> str:
        return self.export()

    @classmethod
    def run_in_background(
        cls,
        on_finished: Optional[Callable[[bool, Optional[str], Optional[str]], None]] = None,
        **kwargs,
    ) -> "XyzGpkgExporterTask":
        """Builds an XyzGpkgExporter from `kwargs` (same arguments as
        `__init__`) and submits it to QGIS's global task manager,
        returning immediately.

        `cls(**kwargs)` here only runs __init__'s cheap, main-thread-bound
        setup (theme/layer resolution, labeling engine settings) - the
        expensive per-feature occupancy build is deferred until export()
        actually starts (see XyzGpkgExporter._prepare()), which happens
        entirely inside the QgsTask below. So unlike calling
        `XyzGpkgExporter(**kwargs).export()` directly - which blocks
        whatever thread calls it, GUI included, for the *entire* export,
        occupancy build and all - this call returns to the caller almost
        immediately, and every slow part of the run (building the occupied-
        tile set, then rendering it) happens on a background thread. The
        GUI (canvas, other plugins, the task manager's own Cancel button)
        stays fully usable for the whole run, at every zoom range.

        `on_finished(success, output_path, error)`, if given, is invoked on
        the main thread once the task completes, fails, or is cancelled."""
        if QgsTask is None or QgsApplication is None:
            raise RuntimeError(
                "QgsTask/QgsApplication are unavailable in this environment - "
                "cannot run in the background here. Call export()/run() directly instead."
            )
        exporter = cls(**kwargs)
        task = XyzGpkgExporterTask(exporter, on_finished=on_finished)
        QgsApplication.taskManager().addTask(task)
        return task


class XyzGpkgExporterTask(QgsTask if QgsTask is not None else object):
    """Runs XyzGpkgExporter.export() on a QgsTask background thread.

    XyzGpkgExporter already parallelizes tile *rendering* across a
    ThreadPoolExecutor internally, but that alone does not stop the GUI
    from freezing: if `export()` is still called synchronously from the
    main/GUI thread, that thread blocks in its own wait-loop for the whole
    run, and a blocked main thread means a blocked Qt event loop - no
    canvas redraw, no other plugin interaction, no way to click Cancel -
    for however long the export takes (minutes to hours). QgsTask.run()
    executes on a Qt-managed worker thread instead, so the main thread's
    event loop is never touched and the GUI stays fully responsive for the
    whole export. Prefer `XyzGpkgExporter.run_in_background(...)` over
    constructing this directly - it wires everything up for you."""

    def __init__(
        self,
        exporter: "XyzGpkgExporter",
        on_finished: Optional[Callable[[bool, Optional[str], Optional[str]], None]] = None,
    ):
        description = f"Export XYZ tiles to GeoPackage (zoom {exporter.min_zoom}-{exporter.max_zoom})"
        super().__init__(description, QgsTask.Flag.CanCancel)
        self._exporter = exporter
        self._on_finished = on_finished
        self._error: Optional[str] = None
        self._output_path: Optional[str] = None

        # Wire the exporter's own progress/cancel hooks straight into this
        # task's QgsTask progress reporting / isCanceled(), so QGIS's
        # built-in task manager UI (progress bar, Cancel button in the
        # status bar) drives and reflects the real export state - no
        # separate polling loop needed on either side.
        exporter.progress_callback = self._on_progress
        exporter.should_cancel = self.isCanceled

    def _on_progress(self, completed: int, total: int) -> None:
        if total > 0:
            self.setProgress(min(100.0, 100.0 * completed / total))

    def run(self) -> bool:
        # This executes on a QgsTask worker thread, never the main/GUI
        # thread - see class docstring.
        try:
            self._output_path = self._exporter.export()
            return not self.isCanceled()
        except Exception as exc:
            self._error = f"{exc}\n{traceback.format_exc()}"
            return False

    def finished(self, result: bool) -> None:
        # finished() is called back on the main thread by QgsTaskManager,
        # so it is safe to touch GUI-adjacent objects here if `on_finished`
        # needs to (e.g. showing a message bar item).
        if not result and self._error:
            QgsMessageLog.logMessage(
                f"XYZ tile export failed: {self._error}", LOG_TAG, Qgis.MessageLevel.Critical,
            )
        elif not result:
            QgsMessageLog.logMessage("XYZ tile export was cancelled", LOG_TAG, Qgis.MessageLevel.Info)
        if self._on_finished:
            self._on_finished(result, self._output_path, self._error)


class _ThreadStats:
    """Per-thread tile counters. Only ever written to by the thread that
    owns the instance (see `XyzGpkgExporter._thread_stats`), so no lock is
    needed on the per-tile hot path; instances are aggregated under a lock
    exactly once, after the thread pool has fully drained."""

    __slots__ = ("written", "skipped", "failed")

    def __init__(self):
        self.written = 0
        self.skipped = 0
        self.failed = 0
