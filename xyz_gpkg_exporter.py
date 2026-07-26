"""
xyz_gpkg_exporter.py

Core, console/standalone exporter that renders the currently loaded QGIS
project into a single GeoPackage of raster XYZ tiles (EPSG:4326).

Key correctness guarantees:
  * All tile bounds are derived exclusively from QgsTileMatrix (single
    source of truth) - never from independent zoom/row/col arithmetic -
    to avoid gapless/overlap ("puzzle") tile defects.
  * @map_scale and @zoom_level are exposed to labels/expressions using an
    explicit equator-based formula, computed ONCE PER ZOOM LEVEL (not per
    tile): every tile at a given zoom has an identical extent width on
    this equirectangular grid, so recomputing per tile was redundant.
    This makes rule-based symbology, scale-dependent labels, and
    scale-dependent layer visibility evaluate identically to what the
    live map canvas would show at an equivalent view - regardless of the
    project's "scale calculation method" setting.
  * The GeoPackage is written without WAL (avoiding tile data being
    stranded in a .gpkg-wal side file that never gets checkpointed into
    the main file), and every diagnostic/error is routed through
    QgsMessageLog so it is actually visible in the QGIS Log Messages
    panel, including when running inside a background QgsTask thread.
  * Each worker thread renders with its own cloned layer objects, since
    QgsMapRendererCustomPainterJob / provider read paths are not
    guaranteed safe to drive concurrently from multiple threads against
    the same shared QgsMapLayer instances.
  * On success, the finished GeoPackage's tile table is added back to
    the current QgsProject as a raster layer.
  * Recommended usage from a plugin/GUI action is start_xyz_gpkg_export(),
    which runs the whole export on a QgsTask background thread so the
    QGIS GUI is never blocked - not during setup, not while a
    max_cpu_percent=100 render is under way. Progress/log output is
    deliberately sparse: one line for the estimated tile count up front,
    then only a final summary - never a per-tile "processed N/M" message.
"""

import os
import math
import sqlite3
import logging
import traceback
import threading
import time
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsProject,
    QgsRectangle,
    QgsPointXY,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsMapSettings,
    QgsMapRendererCustomPainterJob,
    QgsMessageLog,
    QgsRasterLayer,
    QgsTask,
    QgsTileMatrix,
    QgsTileXYZ,
    QgsExpressionContext,
    QgsExpressionContextScope,
    QgsExpressionContextUtils,
)
from qgis.PyQt.QtCore import QSize, QBuffer, QIODevice
from qgis.PyQt.QtGui import QImage, QPainter, QColor

TILE_SIZE = 256
EARTH_CIRCUMFERENCE_M = 40075016.6856
METERS_PER_DEGREE_EQUATOR = EARTH_CIRCUMFERENCE_M / 360.0
INCH_METERS = 0.0254
QIMAGE_NATIVE_FORMATS = {"PNG", "JPEG", "JPG", "WEBP"}
LOG_TAG = "XyzGpkgExporter"

# ---------------------------------------------------------------------------
# Tile matrix scheme (GoogleCRS84Quad) - the single source of truth for the
# tiling grid. Change these to adapt the exporter to a different CRS/grid;
# every extent, scale and row/col calculation derives from these values via
# QgsTileMatrix, never from independent constants scattered through the code.
# ---------------------------------------------------------------------------
TILE_MATRIX_CRS_AUTHID = "EPSG:4326"
TILE_MATRIX_ORIGIN = QgsPointXY(-180.0, 90.0)  # top-left corner of the grid
TILE_MATRIX_Z0_DIMENSION = 180.0  # side length (map units) of one zoom-0 tile
TILE_MATRIX_ROOT_WIDTH = 2   # columns at zoom 0
TILE_MATRIX_ROOT_HEIGHT = 1  # rows at zoom 0


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


def _equatorial_scale_denominator(extent_width_degrees: float, output_width_px: int, dpi: float) -> float:
    """Equator-based scale denominator, computed independently of the
    project's "scale calculation method" setting so that rule-based
    symbology / label / layer visibility thresholds evaluate identically
    for every tile regardless of the latitude band it covers."""
    ground_width_m = extent_width_degrees * METERS_PER_DEGREE_EQUATOR
    paper_width_m = (output_width_px / float(dpi)) * INCH_METERS
    if paper_width_m <= 0:
        return 0.0
    return ground_width_m / paper_width_m


def _compute_worker_count(max_cpu_percent: int) -> int:
    """Derive a ThreadPoolExecutor worker count from a CPU percentage
    ceiling, maximizing throughput while avoiding excessive contention on
    the single serialized GeoPackage write path."""
    pct = max(1, min(100, max_cpu_percent))
    cpu_count = os.cpu_count() or 1
    workers = max(1, math.floor(cpu_count * (pct / 100.0)))
    return workers


def _encode_image(image: QImage, tile_format: str, quality: int) -> bytes:
    """Encode a rendered tile image into the requested raster format."""
    fmt_upper = tile_format.upper()
    if fmt_upper in QIMAGE_NATIVE_FORMATS:
        qt_format = "JPG" if fmt_upper == "JPEG" else fmt_upper
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, qt_format, quality)
        data = bytes(buffer.data())
        buffer.close()
        return data
    return _encode_via_gdal(image, fmt_upper, quality)


def _encode_via_gdal(image: QImage, driver_name: str, quality: int) -> bytes:
    """Fallback encoder for raster formats not natively supported by
    QImage (e.g. JPEG2000), routed through an in-memory GDAL dataset."""
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
    never recompute tile extents via independent zoom/row/col math."""

    def __init__(self, crs: QgsCoordinateReferenceSystem, min_zoom: int, max_zoom: int):
        self._crs = crs
        self._matrices: Dict[int, QgsTileMatrix] = {}
        for zoom in range(min_zoom, max_zoom + 1):
            self._matrices[zoom] = QgsTileMatrix.fromCustomDef(
                zoom, self._crs, TILE_MATRIX_ORIGIN,
                TILE_MATRIX_Z0_DIMENSION, TILE_MATRIX_ROOT_WIDTH, TILE_MATRIX_ROOT_HEIGHT,
            )

    def matrix(self, zoom: int) -> QgsTileMatrix:
        return self._matrices[zoom]

    def matrix_dims(self, zoom: int) -> Tuple[int, int]:
        m = self._matrices[zoom]
        return m.matrixWidth(), m.matrixHeight()

    def tile_extent(self, zoom: int, col: int, row: int) -> QgsRectangle:
        # tileExtent() takes a QgsTileXYZ (column, row, zoom) rather than
        # separate ints.
        return self._matrices[zoom].tileExtent(QgsTileXYZ(col, row, zoom))


class EmptyTileDetector:
    """Cheap pre-render extent-based intersection test used to skip tiles
    that cannot contain any visible content, avoiding wasted rendering and
    empty rows in the output GeoPackage.

    Fails loudly (rather than silently) if no usable layer extent could be
    built at all, since that previously caused every tile in the export to
    be misclassified as empty, producing a schema-only GeoPackage."""

    def __init__(self, project: QgsProject, dest_crs: QgsCoordinateReferenceSystem, logger: logging.Logger):
        self._layer_extents: List[QgsRectangle] = []
        transform_context = project.transformContext()
        layer_count = 0
        for layer in project.mapLayers().values():
            layer_count += 1
            try:
                if layer is None or not layer.isValid():
                    logger.warning("EmptyTileDetector: layer '%s' invalid or None, skipping",
                                   layer.name() if layer else "?")
                    continue
                extent = layer.extent()
                if extent is None or extent.isEmpty():
                    logger.warning("EmptyTileDetector: layer '%s' has empty extent, skipping", layer.name())
                    continue
                src_crs = layer.crs()
                if src_crs != dest_crs:
                    transform = QgsCoordinateTransform(src_crs, dest_crs, transform_context)
                    extent = transform.transformBoundingBox(extent)
                self._layer_extents.append(extent)
            except Exception as exc:
                logger.warning(
                    "EmptyTileDetector: skipping layer '%s' due to error: %s\n%s",
                    layer.name() if layer else "?", exc, traceback.format_exc(),
                )

        if layer_count == 0:
            logger.warning("EmptyTileDetector: project has no layers at all - every tile will be treated as empty.")
        elif not self._layer_extents:
            raise RuntimeError(
                "EmptyTileDetector: no usable layer extents could be built from any of the "
                f"{layer_count} project layer(s). Every tile would be misclassified as empty, "
                "producing a schema-only GeoPackage. Check the layers' CRS/extent validity."
            )
        else:
            logger.info("EmptyTileDetector: built %d usable layer extent(s) out of %d layer(s)",
                        len(self._layer_extents), layer_count)

    def is_empty(self, tile_extent: QgsRectangle) -> bool:
        for extent in self._layer_extents:
            if extent.intersects(tile_extent):
                return False
        return True


class GeoPackageWriter:
    """Writes a standards-compliant gpkg_tile_matrix / gpkg_tile_matrix_set
    tiled raster GeoPackage, batching tile inserts into periodic
    transactions rather than committing per tile.

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
        batch_size: int = 500,
    ):
        self._table = table_name
        self._lock = threading.Lock()
        self._batch: List[Tuple[int, int, int, bytes]] = []
        self._batch_size = batch_size
        self._logger = logger
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=DELETE")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA temp_store=MEMORY")
        self._rows_written = 0
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

    def write_tile(self, zoom: int, col: int, row: int, data: bytes) -> None:
        # Rendering stays fully parallel; only this batched write path is serialized.
        with self._lock:
            self._batch.append((zoom, col, row, data))
            if len(self._batch) >= self._batch_size:
                self._flush_locked()

    def _flush_locked(self) -> None:
        if not self._batch:
            return
        cur = self._conn.cursor()
        cur.executemany(
            f"INSERT OR REPLACE INTO {self._table} (zoom_level, tile_column, tile_row, tile_data) VALUES (?,?,?,?)",
            self._batch,
        )
        self._conn.commit()
        batch_len = len(self._batch)
        self._rows_written += batch_len
        self._batch.clear()

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

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
        # Uses INSERT OR REPLACE, so this count only tracks rows *sent* to
        # write_tile(), not distinct (zoom,col,row) keys post-dedup; a
        # SELECT COUNT(*) would be exact but forces a full-table scan on
        # every export, which is wasted I/O on a large GeoPackage.
        self._logger.info("GeoPackageWriter: %d tile row(s) written to '%s'", self._rows_written, self._table)
        self._conn.close()


class TileRenderer:
    """Renders a single tile from the live QGIS project, rebuilding
    QgsMapSettings extent/size/dpi per tile. The equator-based scale (and
    zoom level) exposed to expressions is precomputed once per zoom level
    in __init__ - not per tile - since every tile at a given zoom has an
    identical extent width on this equirectangular grid.

    Each worker thread gets its own cloned set of layers via thread-local
    storage: QgsMapRendererCustomPainterJob and most data providers are
    not guaranteed safe to drive concurrently from multiple threads
    against the same shared QgsMapLayer instances, and sharing them was a
    prior cause of every tile silently failing to render."""

    def __init__(
        self,
        project: QgsProject,
        dpi: int,
        tile_format: str,
        quality: int,
        layers: List,
        style_overrides: Dict[str, str],
        expr_context: QgsExpressionContext,
        logger: logging.Logger,
        tile_matrix_set: "TileMatrixSet",
        min_zoom: int,
        max_zoom: int,
    ):
        self._project = project
        self._dpi = dpi
        self._format = tile_format.upper()
        self._quality = quality
        self._base_layers = layers
        self._style_overrides = style_overrides
        self._expr_context = expr_context
        self._logger = logger
        self._thread_local = threading.local()

        # map_scale and zoom_level are both zoom-only quantities on this
        # equirectangular grid (every tile at a given zoom has the same
        # width in degrees, regardless of row/column), so build one
        # expression context per zoom level up front - built once here,
        # single-threaded, before the render pool starts - instead of
        # rebuilding an identical scope/context for every single tile.
        self._zoom_contexts: Dict[int, QgsExpressionContext] = {}
        for zoom in range(min_zoom, max_zoom + 1):
            matrix_w, _ = tile_matrix_set.matrix_dims(zoom)
            tile_deg_w = tile_matrix_set.matrix(zoom).extent().width() / matrix_w
            scale = _equatorial_scale_denominator(tile_deg_w, TILE_SIZE, dpi)
            scope = QgsExpressionContextScope()
            scope.setVariable("map_scale", scale, True)
            scope.setVariable("zoom_level", zoom, True)
            ctx = QgsExpressionContext(expr_context)
            ctx.appendScope(scope)
            self._zoom_contexts[zoom] = ctx

    def _thread_layers(self) -> List:
        if not hasattr(self._thread_local, "layers"):
            cloned = []
            for lyr in self._base_layers:
                try:
                    cloned.append(lyr.clone())
                except Exception as exc:
                    self._logger.warning(
                        "TileRenderer: failed to clone layer '%s' for thread %s, using shared instance (%s)",
                        lyr.name(), threading.get_ident(), exc,
                    )
                    cloned.append(lyr)
            self._thread_local.layers = cloned
        return self._thread_local.layers

    def render_tile(self, extent: QgsRectangle, dest_crs: QgsCoordinateReferenceSystem, zoom: int) -> bytes:
        settings = QgsMapSettings()
        settings.setDestinationCrs(dest_crs)
        settings.setLayers(self._thread_layers())
        if self._style_overrides:
            settings.setLayerStyleOverrides(self._style_overrides)
        settings.setBackgroundColor(QColor(0, 0, 0, 0))
        settings.setOutputSize(QSize(TILE_SIZE, TILE_SIZE))
        settings.setOutputDpi(self._dpi)
        settings.setExtent(extent)
        settings.setFlag(QgsMapSettings.Flag.Antialiasing, True)
        settings.setFlag(QgsMapSettings.Flag.RenderMapTile, True)
        settings.setFlag(QgsMapSettings.Flag.UseAdvancedEffects, True)

        # map_scale/zoom_level: cheap copy (Qt implicit sharing) of the
        # context built once for this zoom in __init__ - no per-tile scope
        # construction or scale recomputation.
        settings.setExpressionContext(QgsExpressionContext(self._zoom_contexts[zoom]))

        image = QImage(TILE_SIZE, TILE_SIZE, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor(0, 0, 0, 0))
        painter = QPainter(image)
        try:
            job = QgsMapRendererCustomPainterJob(settings, painter)
            job.renderSynchronously()
        finally:
            painter.end()

        return _encode_image(image, self._format, self._quality)


class XyzGpkgExporter:
    """Exports the currently loaded QGIS project as raster XYZ tiles into a
    single GeoPackage. Single public entry point: `export()` / `run()`."""

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
    ):
        self._logger = _make_logger()
        self.project = QgsProject.instance()
        self.dest_crs = QgsCoordinateReferenceSystem(TILE_MATRIX_CRS_AUTHID)

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

        self.tile_matrix_set = TileMatrixSet(self.dest_crs, self.min_zoom, self.max_zoom)
        self.empty_detector = EmptyTileDetector(self.project, self.dest_crs, self._logger)
        self._resolve_layers_and_theme()

        expr_context = QgsExpressionContext()
        expr_context.appendScope(QgsExpressionContextUtils.globalScope())
        expr_context.appendScope(QgsExpressionContextUtils.projectScope(self.project))
        self.expr_context = expr_context

        self.progress_callback = progress_callback
        self.should_cancel = should_cancel

        self._stats_lock = threading.Lock()
        self._tiles_written = 0
        self._tiles_skipped_empty = 0
        self._tiles_failed = 0

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

    def _tile_range_for_extent(self, zoom: int, matrix_w: int, matrix_h: int) -> Tuple[int, int, int, int]:
        # Used only to bound the iteration range; final tile bounds for
        # rendering/writing always come from TileMatrixSet.tile_extent().
        matrix_extent = self.tile_matrix_set.matrix(zoom).extent()
        if not self.extent.intersects(matrix_extent):
            return (0, 0, -1, -1)
        clipped = self.extent.intersect(matrix_extent)
        if clipped is None or clipped.isEmpty():
            return (0, 0, -1, -1)

        tile_deg_w = matrix_extent.width() / matrix_w
        tile_deg_h = matrix_extent.height() / matrix_h

        min_col = int(math.floor((clipped.xMinimum() - matrix_extent.xMinimum()) / tile_deg_w))
        max_col = int(math.floor((clipped.xMaximum() - matrix_extent.xMinimum()) / tile_deg_w))
        # row 0 at top (north), increasing southward - standard XYZ/GPKG order.
        min_row = int(math.floor((matrix_extent.yMaximum() - clipped.yMaximum()) / tile_deg_h))
        max_row = int(math.floor((matrix_extent.yMaximum() - clipped.yMinimum()) / tile_deg_h))

        min_col = max(0, min_col)
        min_row = max(0, min_row)
        max_col = min(matrix_w - 1, max_col)
        max_row = min(matrix_h - 1, max_row)
        return (min_col, min_row, max_col, max_row)

    def _build_zoom_ranges(self) -> Dict[int, Tuple[int, int, int, int]]:
        # Computed once and reused for both the tile-count estimate and the
        # submission loop below, instead of recomputing the same
        # intersect/floor arithmetic for every zoom level twice.
        ranges: Dict[int, Tuple[int, int, int, int]] = {}
        for zoom in range(self.min_zoom, self.max_zoom + 1):
            matrix_w, matrix_h = self.tile_matrix_set.matrix_dims(zoom)
            ranges[zoom] = self._tile_range_for_extent(zoom, matrix_w, matrix_h)
        return ranges

    @staticmethod
    def _total_tiles(zoom_ranges: Dict[int, Tuple[int, int, int, int]]) -> int:
        total = 0
        for min_col, min_row, max_col, max_row in zoom_ranges.values():
            if max_col >= min_col and max_row >= min_row:
                total += (max_col - min_col + 1) * (max_row - min_row + 1)
        return max(1, total)

    def _process_tile(self, renderer: TileRenderer, writer: GeoPackageWriter, zoom: int, col: int, row: int) -> None:
        try:
            tile_extent = self.tile_matrix_set.tile_extent(zoom, col, row)
            if self.empty_detector.is_empty(tile_extent):
                with self._stats_lock:
                    self._tiles_skipped_empty += 1
                return
            data = renderer.render_tile(tile_extent, self.dest_crs, zoom)
            if not data:
                self._logger.warning("Tile z=%d x=%d y=%d rendered but produced no encoded bytes", zoom, col, row)
                with self._stats_lock:
                    self._tiles_skipped_empty += 1
                return
            writer.write_tile(zoom, col, row, data)
            with self._stats_lock:
                self._tiles_written += 1
        except Exception as exc:
            with self._stats_lock:
                self._tiles_failed += 1
            self._logger.error(
                "Failed tile z=%d x=%d y=%d: %s\n%s", zoom, col, row, exc, traceback.format_exc()
            )

    def add_result_layer(self, table_name: str = "xyz_tiles") -> None:
        """Loads the finished GeoPackage's tile table back into the current
        QgsProject as a raster layer. Touches QgsProject/the layer tree, so
        only call this from the main/GUI thread - e.g. from a QgsTask's
        finished() callback, never from run()/export() itself."""
        uri = f"GPKG:{self.output_path}:{table_name}"
        layer_name = os.path.splitext(os.path.basename(self.output_path))[0]
        try:
            raster_layer = QgsRasterLayer(uri, layer_name, "gdal")
            if raster_layer.isValid():
                self.project.addMapLayer(raster_layer)
                self._logger.info("Added '%s' to the project as layer '%s'", self.output_path, layer_name)
            else:
                self._logger.warning(
                    "Export finished but the output GeoPackage could not be loaded as a layer (%s)", uri
                )
        except Exception as exc:
            self._logger.warning("Failed to add output GeoPackage to the project: %s", exc)

    def export(self) -> str:
        """Runs the export and returns the path to the produced GeoPackage."""
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
            logger=self._logger,
            tile_matrix_set=self.tile_matrix_set,
            min_zoom=self.min_zoom,
            max_zoom=self.max_zoom,
        )

        worker_count = _compute_worker_count(self.max_cpu_percent)
        zoom_ranges = self._build_zoom_ranges()
        total_estimate = self._total_tiles(zoom_ranges)
        self._logger.info(
            "Export starting: %d worker thread(s), %d tile(s) estimated -> %s",
            worker_count, total_estimate, self.output_path,
        )

        def _tile_coords():
            for zoom in range(self.min_zoom, self.max_zoom + 1):
                min_col, min_row, max_col, max_row = zoom_ranges[zoom]
                if max_col < min_col or max_row < min_row:
                    continue
                for row in range(min_row, max_row + 1):
                    for col in range(min_col, max_col + 1):
                        yield (zoom, col, row)

        coords_iter = iter(_tile_coords())
        # Bound how many futures exist at once instead of submitting every
        # tile up front: for multi-million-tile exports that upfront
        # submission loop is itself a long, uninterruptible stretch of
        # Python code, and it makes cancellation/backpressure cheap since
        # only a small window of in-flight work ever needs to be tracked.
        window_size = max(worker_count * 4, worker_count)
        completed = 0
        last_report_time = time.monotonic()
        report_interval_sec = 0.5

        try:
            with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="xyztile") as pool:
                in_flight = set()

                def _submit_next() -> bool:
                    try:
                        zoom, col, row = next(coords_iter)
                    except StopIteration:
                        return False
                    in_flight.add(pool.submit(self._process_tile, renderer, writer, zoom, col, row))
                    return True

                for _ in range(window_size):
                    if not _submit_next():
                        break

                if not in_flight:
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
                            with self._stats_lock:
                                self._tiles_failed += 1
                            self._logger.error("Tile task raised: %s\n%s", exc, traceback.format_exc())
                        completed += 1
                        if not cancelled:
                            _submit_next()

                    now = time.monotonic()
                    if self.progress_callback and (
                        completed >= total_estimate or (now - last_report_time) >= report_interval_sec
                    ):
                        self.progress_callback(completed, total_estimate)
                        last_report_time = now

                    if not cancelled and self.should_cancel and self.should_cancel():
                        cancelled = True
                        for pending in in_flight:
                            pending.cancel()
        finally:
            writer.close()

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


class XyzGpkgExportTask(QgsTask):
    """Runs XyzGpkgExporter.export() on a QgsTask background thread.

    QgsTask.run() executes on a worker thread managed by QGIS's own task
    manager - never the GUI thread - so the tile-submission loop and every
    render/write call happen without blocking the QGIS UI, regardless of
    how many tiles there are or how high max_cpu_percent is set.

    finished() is invoked back on the main/GUI thread by the task manager
    once run() returns, which is the only safe place to touch QgsProject -
    so the output layer is added there, never from inside export() itself.

    Progress is surfaced through QgsTask.setProgress(), which the QGIS
    task manager already throttles/coalesces for UI updates; the exporter
    itself only calls back at most twice a second regardless of tile
    count, so neither the log nor the progress bar gets flooded.
    """

    def __init__(self, exporter: "XyzGpkgExporter", description: str = "Exporting XYZ tiles to GeoPackage"):
        super().__init__(description, QgsTask.Flag.CanCancel)
        self._exporter = exporter
        self._exporter.progress_callback = self._on_progress
        self._exporter.should_cancel = self.isCanceled
        self.output_path: Optional[str] = None
        self.error: Optional[str] = None

    def _on_progress(self, completed: int, total: int) -> None:
        self.setProgress(100.0 * completed / total if total else 0.0)

    def run(self) -> bool:
        try:
            self.output_path = self._exporter.export()
            return self._exporter._tiles_written > 0
        except Exception as exc:
            self.error = f"{exc}\n{traceback.format_exc()}"
            return False

    def finished(self, result: bool) -> None:
        # Runs on the main/GUI thread - safe to touch QgsProject here.
        if result and self.output_path:
            try:
                self._exporter.add_result_layer()
            except Exception as exc:
                QgsMessageLog.logMessage(
                    f"XyzGpkgExporter: failed to add result layer: {exc}", LOG_TAG, Qgis.MessageLevel.Warning
                )
            QgsMessageLog.logMessage(
                f"XYZ export finished: {self.output_path}", LOG_TAG, Qgis.MessageLevel.Info
            )
        elif self.isCanceled():
            QgsMessageLog.logMessage("XYZ export cancelled", LOG_TAG, Qgis.MessageLevel.Warning)
        else:
            QgsMessageLog.logMessage(
                f"XYZ export failed: {self.error or 'no tiles written'}", LOG_TAG, Qgis.MessageLevel.Critical
            )


def start_xyz_gpkg_export(**exporter_kwargs) -> XyzGpkgExportTask:
    """Recommended entry point for plugin/GUI code: builds the exporter and
    hands it to QGIS's task manager, returning immediately. The GUI is
    never blocked - not during setup, not during rendering - regardless of
    tile count or max_cpu_percent. For non-interactive/console use where
    blocking is fine, construct XyzGpkgExporter directly and call
    .export() (then .add_result_layer() if desired) instead."""
    exporter = XyzGpkgExporter(**exporter_kwargs)
    task = XyzGpkgExportTask(exporter)
    QgsApplication.taskManager().addTask(task)
    return task