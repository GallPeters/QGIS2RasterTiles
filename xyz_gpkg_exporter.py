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
  * EmptyTileDetector short-circuits on a single cheap bounding-box test
    against the union of all layer extents before falling back to the
    full per-layer intersection loop, so the (typically dominant, at low
    zoom levels covering large regions of no data) fully-empty case costs
    O(1) instead of O(layer count).
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
from qgis.utils import iface
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
)
from qgis.PyQt.QtCore import QSize, QBuffer, QIODevice
from qgis.PyQt.QtGui import QImage, QPainter, QColor

TILE_SIZE = 256
EARTH_CIRCUMFERENCE_M = 40075016.6856
METERS_PER_DEGREE_EQUATOR = EARTH_CIRCUMFERENCE_M / 360.0
INCH_METERS = 0.0254
QIMAGE_NATIVE_FORMATS = {"PNG", "JPEG", "JPG", "WEBP"}
LOG_TAG = "XyzGpkgExporter"

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
        self._tile_origin = QgsPointXY(-180.0, 90.0)
        # z0Dimension is the side length (map units) of ONE root tile at zoom 0,
        # not a tile count. 180.0 degrees x 2 columns x 1 row = full globe
        # (-180,-90 : 180,90) at every zoom level, with every tile perfectly
        # square (strict 1:1 aspect ratio), matching GoogleCRS84Quad.
        self._z0_dimension = 180.0
        self._root_matrix_width = 2
        self._root_matrix_height = 1
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

        # Pre-compute the union bounding box of every layer extent once.
        # On the per-tile hot path this lets us reject the overwhelmingly
        # common "tile has no data at all" case (especially at low zoom
        # levels, where most of a multi-million-tile export's tiles cover
        # ocean/empty space) with a single O(1) rectangle test, instead of
        # unconditionally looping over every layer extent.
        self._single_extent = self._layer_extents[0] if len(self._layer_extents) == 1 else None
        self._union_extent: Optional[QgsRectangle] = None
        for extent in self._layer_extents:
            if self._union_extent is None:
                self._union_extent = QgsRectangle(extent)
            else:
                self._union_extent.combineExtentWith(extent)

    def is_empty(self, tile_extent: QgsRectangle) -> bool:
        # Common special case: a single source layer - the union extent
        # *is* that layer's extent, so skip the redundant duplicate test.
        single = self._single_extent
        if single is not None:
            return not single.intersects(tile_extent)

        union = self._union_extent
        if union is None or not union.intersects(tile_extent):
            return True

        for extent in self._layer_extents:
            if extent.intersects(tile_extent):
                return False
        return True


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
        # dpi-dependent half of the equator-scale formula - the "paper"
        # width in metres - is invariant and computed once here, instead
        # of on every single tile. Combined with `_scale_cache` below, the
        # per-tile scale computation degrades from "float division on
        # every tile" to "one dict lookup per tile, one float division per
        # zoom level" - every tile at a given zoom is the same width in
        # this uniform, non-adaptive tile grid, so the scale is provably
        # identical for every tile sharing a zoom level.
        self._paper_width_m = (TILE_SIZE / float(dpi)) * INCH_METERS
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

    def _scale_for_zoom(self, zoom: int, extent: QgsRectangle) -> float:
        cached = self._scale_cache.get(zoom)
        if cached is not None:
            return cached
        value = _equatorial_scale_denominator(extent.width(), self._paper_width_m)
        # Deterministic value - a benign race between threads computing
        # the same zoom's scale for the first time simultaneously is safe
        # (last write wins, both writes are equal), and avoids needing a
        # lock on this per-tile-adjacent hot path.
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

        # Equator-based scale: identical for every tile within a zoom
        # level (uniform grid), so this is a cache lookup after the first
        # tile of each zoom rather than a fresh computation every time.
        equator_scale = self._scale_for_zoom(zoom, extent)
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
        self.dest_crs = QgsCoordinateReferenceSystem("EPSG:4326")
        screen = iface.mainWindow().windowHandle().screen()
        self.dpi = dpi*screen.devicePixelRatio()

        if extent is None:
            extent = self._canvas_extent_or_none()
        self.extent = extent if extent is not None else QgsRectangle(-180.0, -90.0, 180.0, 90.0)

        if min_zoom < 0 or max_zoom < min_zoom:
            raise ValueError("Invalid zoom range: max_zoom must be >= min_zoom >= 0")
        self.min_zoom = min_zoom
        self.max_zoom = max_zoom
        
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

        self.tile_matrix_set = TileMatrixSet(self.dest_crs, self.min_zoom, self.max_zoom)
        for zoom in range(self.min_zoom, self.max_zoom + 1):
            self._logger.info(
                "Tile matrix zoom %d: extent=%s dims=%s",
                zoom, self.tile_matrix_set.matrix(zoom).extent().toString(),
                self.tile_matrix_set.matrix_dims(zoom),
            )

        self.empty_detector = EmptyTileDetector(self.project, self.dest_crs, self._logger)
        self._resolve_layers_and_theme()
        self._logger.info("Resolved %d layer(s) for rendering", len(self.layers))

        expr_context = QgsExpressionContext()
        expr_context.appendScope(QgsExpressionContextUtils.globalScope())
        expr_context.appendScope(QgsExpressionContextUtils.projectScope(self.project))
        self.expr_context = expr_context

        self.progress_callback = progress_callback
        self.should_cancel = should_cancel

        # Per-thread stats, aggregated once at the end of export() instead
        # of taking a shared lock on every single tile. See `_ThreadStats`
        # and `_thread_stats()`.
        self._stats_registry_lock = threading.Lock()
        self._stats_registry: List[_ThreadStats] = []
        self._stats_local = threading.local()
        self._tiles_written = 0
        self._tiles_skipped_empty = 0
        self._tiles_failed = 0

    def _canvas_extent_or_none(self) -> Optional[QgsRectangle]:
        try:
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

    def _estimate_total_tiles(self) -> int:
        total = 0
        for zoom in range(self.min_zoom, self.max_zoom + 1):
            matrix_w, matrix_h = self.tile_matrix_set.matrix_dims(zoom)
            min_col, min_row, max_col, max_row = self._tile_range_for_extent(zoom, matrix_w, matrix_h)
            if max_col >= min_col and max_row >= min_row:
                total += (max_col - min_col + 1) * (max_row - min_row + 1)
        return max(1, total)

    def _iter_tile_coords(self):
        """Lazily yields (zoom, col, row) tuples for every tile to render.
        Kept as a generator (rather than materializing the full list) so
        that, combined with the bounded-in-flight submission pipeline in
        export(), peak memory never scales with total tile count."""
        for zoom in range(self.min_zoom, self.max_zoom + 1):
            matrix_w, matrix_h = self.tile_matrix_set.matrix_dims(zoom)
            min_col, min_row, max_col, max_row = self._tile_range_for_extent(zoom, matrix_w, matrix_h)
            if max_col < min_col or max_row < min_row:
                self._logger.warning("Zoom %d: extent does not intersect this zoom's tile matrix", zoom)
                continue
            for row in range(min_row, max_row + 1):
                if self.should_cancel and self.should_cancel():
                    return
                for col in range(min_col, max_col + 1):
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
        stats = self._thread_stats()
        try:
            tile_extent = self.tile_matrix_set.tile_extent(zoom, col, row)
            if self.empty_detector.is_empty(tile_extent):
                stats.skipped += 1
                return
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
        total_estimate = self._estimate_total_tiles()
        self._logger.info("Estimated total tiles to process: %d", total_estimate)
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
                tile_iter = self._iter_tile_coords()
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
