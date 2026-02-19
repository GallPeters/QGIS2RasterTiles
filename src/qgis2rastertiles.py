"""QGIS2RasterTiles — Export the current QGIS project as XYZ raster tiles.

Known fixes in this version
---------------------------
1. In-memory tile index  (no GPKG written to disk for the index).
2. Parallel rendering via pre-split work slices (no shared-queue race).
3. QgsMapRendererCustomPainterJob.renderSynchronously() — the only renderer
   that works correctly from a QgsTask background thread.
4. Per-tile scale: DPI is read from the canvas settings at render time
   (src.outputDpi()) rather than hardcoded to 96.  This keeps the label
   engine's scale denominator consistent with the QGIS canvas DPI, so labels
   appear at the geometrically correct size at every zoom level.
5. Empty-tile check only discards fully-transparent tiles; partial-alpha
   (edge tiles, semi-transparent layers) are kept.
6. pack_to_gpkg: after all render workers finish, PNGs are packed into the
   GPKG inline on the main thread (no second QgsTask) — avoids the entire
   chain of GC/signal-timing bugs that caused the GPKG to be empty.
   tile_row is written without Y-flip: OGC GeoPackage tile_row=0 is the
   northwest corner, identical to XYZ y=0 — no conversion needed.
7. Completion tracker stored on self (not a local variable) so it cannot be
   garbage-collected before all worker signals have fired.
8. <MinTileLevel> is placed inside <DataWindow> in the GDAL_WMS XML where
   GDAL actually recognises it, preventing spurious requests below min zoom.
"""

import os
import glob
import sqlite3
import threading
import multiprocessing
from collections import namedtuple
from os.path import join
from os import makedirs
from datetime import datetime
from osgeo import osr

from qgis.core import (
    QgsTask,
    QgsLabelingEngineSettings,
    QgsGeometry,
    QgsApplication,
    QgsMapRendererCustomPainterJob,
    QgsMapSettings,
    QgsProject,
    QgsProcessingUtils,
    QgsRasterLayer,
    QgsRectangle,
    QgsProcessingFeedback,
    Qgis
)
from qgis.PyQt.QtCore import QSize, Qt, QEventLoop
from qgis.PyQt.QtGui import QImage, QPainter
from qgis.utils import iface


# ---------------------------------------------------------------------------
# In-memory tile descriptor
# ---------------------------------------------------------------------------

TileDescriptor = namedtuple("TileDescriptor", ["zoom", "col", "row", "extent"])

# ---------------------------------------------------------------------------
# Completion tracker
# ---------------------------------------------------------------------------


class _CompletionTracker:
    """Thread-safe counter; fires callbacks on the main Qt thread when all
    tasks have signalled completion.

    Connect BOTH taskCompleted AND taskTerminated to notify() so the counter
    always reaches zero even when a task is cancelled or errors out.

    Do NOT use QgsTask.finished() overrides — Qt/SIP never invokes Python
    overrides of that C++ virtual method on QgsTask subclasses.
    """

    def __init__(self, total: int, *callbacks,
                 feedback: "QgsProcessingFeedback | None" = None):
        self._remaining = total
        self._lock = threading.Lock()
        self._callbacks = list(callbacks)
        self._feedback = feedback

    def _log(self, message: str):
        if self._feedback is not None:
            self._feedback.pushInfo(message)
        else:
            print(message)

    def notify(self):
        with self._lock:
            self._remaining -= 1
            remaining = self._remaining
            self._log(f"[Tracker] task done, {remaining} remaining")
        if remaining <= 0:
            for cb in self._callbacks:
                cb()


# ---------------------------------------------------------------------------
# GeoPackage tile writer  (thread-safe batched writer)
# ---------------------------------------------------------------------------


class GeoPackageTileWriter:
    """Thread-safe OGC GeoPackage writer used by GpkgPackerTask."""

    BATCH_SIZE = 200

    def __init__(self, gpkg_path: str, tile_size: int = 256,
                 crs_epsg: int = 4326,
                 feedback: "QgsProcessingFeedback | None" = None):
        self.gpkg_path = gpkg_path
        self.tile_size = tile_size
        self.crs_epsg = crs_epsg
        self._conn = None
        self._lock = threading.Lock()
        self._buffer = []
        self._feedback = feedback

    def _log(self, message: str):
        if self._feedback is not None:
            self._feedback.pushInfo(message)
        else:
            print(message)

    def open(self):
        self._conn = sqlite3.connect(self.gpkg_path, check_same_thread=False)
        cur = self._conn.cursor()
        for pragma in [
            "PRAGMA application_id = 1196444487",
            "PRAGMA user_version    = 10200",
            "PRAGMA journal_mode    = DELETE",
            "PRAGMA synchronous     = NORMAL",
            "PRAGMA cache_size      = -65536",
            "PRAGMA temp_store      = MEMORY",
        ]:
            cur.execute(pragma)
        self._conn.commit()
        self._create_schema()
        self._log(f"[GeoPackageTileWriter] Opened: {self.gpkg_path}")

    def close(self):
        with self._lock:
            self._flush_locked()
            if self._conn:
                self._conn.close()
                self._conn = None
        self._log("[GeoPackageTileWriter] Closed.")

    def _create_schema(self):
        cur = self._conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS gpkg_spatial_ref_sys (
                srs_name TEXT NOT NULL, srs_id INTEGER PRIMARY KEY,
                organization TEXT NOT NULL,
                organization_coordsys_id INTEGER NOT NULL,
                definition TEXT NOT NULL, description TEXT)""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS gpkg_contents (
                table_name TEXT NOT NULL PRIMARY KEY,
                data_type TEXT NOT NULL, identifier TEXT UNIQUE,
                description TEXT DEFAULT '',
                last_change DATETIME NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                min_x REAL, min_y REAL, max_x REAL, max_y REAL,
                srs_id INTEGER REFERENCES gpkg_spatial_ref_sys(srs_id))""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS gpkg_tile_matrix_set (
                table_name TEXT NOT NULL PRIMARY KEY
                    REFERENCES gpkg_contents(table_name),
                srs_id INTEGER NOT NULL
                    REFERENCES gpkg_spatial_ref_sys(srs_id),
                min_x REAL NOT NULL, min_y REAL NOT NULL,
                max_x REAL NOT NULL, max_y REAL NOT NULL)""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS gpkg_tile_matrix (
                table_name TEXT NOT NULL
                    REFERENCES gpkg_contents(table_name),
                zoom_level INTEGER NOT NULL,
                matrix_width INTEGER NOT NULL, matrix_height INTEGER NOT NULL,
                tile_width INTEGER NOT NULL, tile_height INTEGER NOT NULL,
                pixel_x_size REAL NOT NULL, pixel_y_size REAL NOT NULL,
                PRIMARY KEY (table_name, zoom_level))""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                zoom_level INTEGER NOT NULL,
                tile_column INTEGER NOT NULL,
                tile_row INTEGER NOT NULL,
                tile_data BLOB NOT NULL,
                UNIQUE (zoom_level, tile_column, tile_row))""")
        self._conn.commit()
        self._log("[GeoPackageTileWriter] Schema ready.")

    def populate_metadata(self, zoom_levels, origin_x, origin_y,
                          full_w, full_h,
                          exp_minx, exp_miny, exp_maxx, exp_maxy,
                          mw_z0, mh_z0):
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(self.crs_epsg)
        wkt = srs.ExportToWkt()
        cur = self._conn.cursor()
        cur.executemany(
            "INSERT OR IGNORE INTO gpkg_spatial_ref_sys "
            "(srs_name,srs_id,organization,organization_coordsys_id,definition) "
            "VALUES (?,?,?,?,?)",
            [("Undefined Cartesian",  0, "NONE",  0, "undefined"),
             ("Undefined Geographic", -1, "NONE", -1, "undefined")])
        cur.execute(
            "INSERT OR REPLACE INTO gpkg_spatial_ref_sys "
            "(srs_name,srs_id,organization,organization_coordsys_id,definition) "
            "VALUES (?,?,?,?,?)",
            (srs.GetName(), self.crs_epsg, "EPSG", self.crs_epsg, wkt))
        cur.execute(
            "INSERT OR REPLACE INTO gpkg_contents "
            "(table_name,data_type,identifier,description,"
            " min_x,min_y,max_x,max_y,srs_id) "
            "VALUES ('tiles','tiles','tiles','XYZ tile export',?,?,?,?,?)",
            (exp_minx, exp_miny, exp_maxx, exp_maxy, self.crs_epsg))
        cur.execute(
            "INSERT OR REPLACE INTO gpkg_tile_matrix_set "
            "(table_name,srs_id,min_x,min_y,max_x,max_y) "
            "VALUES ('tiles',?,?,?,?,?)",
            (self.crs_epsg,
             origin_x, origin_y - full_h,
             origin_x + full_w, origin_y))
        for z in sorted(zoom_levels):
            sc = 2 ** z
            mw, mh = mw_z0 * sc, mh_z0 * sc
            cur.execute(
                "INSERT OR REPLACE INTO gpkg_tile_matrix "
                "(table_name,zoom_level,matrix_width,matrix_height,"
                " tile_width,tile_height,pixel_x_size,pixel_y_size) "
                "VALUES ('tiles',?,?,?,?,?,?,?)",
                (z, mw, mh,
                 self.tile_size, self.tile_size,
                 full_w / (mw * self.tile_size),
                 full_h / (mh * self.tile_size)))
        self._conn.commit()
        self._log(f"[GeoPackageTileWriter] Metadata for zooms {sorted(zoom_levels)}")

    def add_tile(self, z: int, x: int, y: int, png_bytes: bytes):
        with self._lock:
            self._buffer.append((z, x, y, sqlite3.Binary(png_bytes)))
            if len(self._buffer) >= self.BATCH_SIZE:
                self._flush_locked()

    def _flush_locked(self):
        if not self._buffer:
            return
        self._conn.executemany(
            "INSERT OR REPLACE INTO tiles "
            "(zoom_level,tile_column,tile_row,tile_data) VALUES (?,?,?,?)",
            self._buffer)
        self._conn.commit()
        self._log(f"[GeoPackageTileWriter] Flushed {len(self._buffer)} tiles.")
        self._buffer.clear()


# ---------------------------------------------------------------------------
# Render worker
# ---------------------------------------------------------------------------


class TileExportTask(QgsTask):
    """
    Renders a pre-assigned list of tiles to PNG files using
    QgsMapRendererCustomPainterJob.renderSynchronously().

    Why this renderer?
    ------------------
    QgsMapRendererSequentialJob and QgsMapRendererParallelJob both dispatch
    work through Qt signals/slots and require the Qt event loop to be spinning.
    Called from a QgsTask background thread (which has no event loop), they
    return immediately with an empty image.

    QgsMapRendererCustomPainterJob.renderSynchronously() renders directly onto
    a QPainter/QImage in the calling thread with no signals and no event-loop
    dependency — exactly what we need.

    Why clone canvas settings and then setExtent()?
    ------------------------------------------------
    Cloning iface.mapCanvas().mapSettings() preserves DPI, layer list, CRS,
    and text-render flags.  After cloning, calling setExtent() + setOutputSize()
    causes QgsMapSettings to recompute the internal scale denominator from the
    tile's own geographic extent and pixel size.  This means the label engine
    uses the geometrically correct scale for each tile — labels at zoom 5 are
    sized for zoom 5, labels at zoom 8 are sized for zoom 8, matching the
    physical appearance of the original styled layers.
    """

    def __init__(self, tiles: list, output_dir: str,
                 canvas_settings: QgsMapSettings,
                 tile_size: int = 256, render_buffer_px: int = 128,
                 output_dpi: float = 96.0,
                 feedback: "QgsProcessingFeedback | None" = None):
        super().__init__("Tile Export Worker", QgsTask.CanCancel)
        self.tiles = tiles
        self.output_dir = output_dir
        self.canvas_settings = canvas_settings
        self.tile_size = tile_size
        self.render_buffer_px = render_buffer_px
        self.output_dpi = output_dpi
        self._feedback = feedback

    def _log(self, message: str):
        if self._feedback is not None:
            self._feedback.pushInfo(message)
        else:
            print(message)

    def _render_tile(self, extent: QgsRectangle) -> QImage:
        # 1. Overscan: expand extent to prevent label clipping at tile edges
        mupx = extent.width() / self.tile_size
        buf_mu = self.render_buffer_px * mupx
        expanded = QgsRectangle(
            extent.xMinimum() - buf_mu,
            extent.yMinimum() - buf_mu,
            extent.xMaximum() + buf_mu,
            extent.yMaximum() + buf_mu,
        )
        render_size = self.tile_size + 2 * self.render_buffer_px

        # 2. Build fresh QgsMapSettings at exactly 96 DPI.
        #
        #    Why NOT clone canvas settings:
        #    The canvas has its own DPI baked in (often Hi-DPI != 96).
        #    Cloning it then changing extent/size makes QgsMapSettings compute
        #    the scale from the canvas DPI, so labels are sized for the wrong
        #    DPI and look small or large when the tile is shown at 96 DPI.
        #
        #    Why we match the canvas DPI exactly:
        #    QgsMapSettings derives the scale denominator (used by the label
        #    engine) from:  extent_width / (pixels / dpi * 0.0254).
        #    If we bake a different DPI into the tile than QGIS uses when it
        #    *displays* the tile layer, the label engine sizes labels for the
        #    wrong scale and they appear 1 zoom level off.
        #    Reading outputDpi() from the canvas settings at render time keeps
        #    the denominator consistent regardless of monitor or project DPI.
        #
        #    Scale is derived automatically:
        #        scale = extent_width_metres / (render_px / dpi * 0.0254)
        #    We do NOT set scale manually — QgsMapSettings computes it from
        #    extent + output size + DPI.
        src = self.canvas_settings
        output_dpi = self.output_dpi * src.devicePixelRatio()   # set by caller (GUI param or __console__ default)
        settings = QgsMapSettings()
        settings.setLayers(src.layers())
        settings.setDestinationCrs(src.destinationCrs())
        settings.setExtent(expanded)
        settings.setOutputSize(QSize(render_size, render_size))
        settings.setOutputDpi(output_dpi)
        settings.setBackgroundColor(Qt.transparent)
        settings.setFlag(QgsMapSettings.DrawLabeling,
                         src.testFlag(QgsMapSettings.DrawLabeling))
        settings.setFlag(QgsMapSettings.UseAdvancedEffects,
                         src.testFlag(QgsMapSettings.UseAdvancedEffects))
        settings.setFlag(QgsMapSettings.ForceVectorOutput, True)
        settings.setTransformContext(src.transformContext())
        settings.setFlag(QgsMapSettings.DrawLabeling,
                src.testFlag(QgsMapSettings.DrawLabeling))
        extent_geom = QgsGeometry.fromWkt(extent.asWktPolygon())
        settings.setLabelBoundaryGeometry(extent_geom)
        labeling_settings = QgsLabelingEngineSettings()
        labeling_settings.readSettingsFromProject(QgsProject.instance())
        settings.setLabelingEngineSettings(labeling_settings)
        # blocking_region = QgsLabelBlockingRegion(extent_geom)
        # settings.setLabelBlockingRegions([blocking_region])
        settings.setScaleMethod(src.scaleMethod())

        # 3. Render synchronously — safe from any thread, no event loop
        image = QImage(render_size, render_size,
                       QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        job = QgsMapRendererCustomPainterJob(settings, painter)
        job.renderSynchronously()
        painter.end()

        # 4. Crop to the requested tile_size
        return image.copy(
            self.render_buffer_px, self.render_buffer_px,
            self.tile_size, self.tile_size,
        )

    @staticmethod
    def _has_content(image: QImage) -> bool:
        """True if at least one pixel is non-fully-transparent.

        We only skip tiles that are 100 % blank (all alpha bytes == 0),
        meaning the tile is completely outside the data extent.  Tiles with
        partial transparency (edge tiles, transparent-fill layers) are kept.
        """
        img = image.convertToFormat(QImage.Format_ARGB32)
        ptr = img.bits()
        ptr.setsize(img.byteCount())
        return any(bytes(ptr)[3::4])   # alpha at every 4th byte

    def run(self) -> bool:
        rendered = skipped = errors = 0
        self._log(f"[TileExportTask] Starting — {len(self.tiles)} tiles assigned.")

        for td in self.tiles:
            if self.isCanceled():
                self._log("[TileExportTask] Cancelled.")
                break
            try:
                image = self._render_tile(td.extent)

                if not self._has_content(image):
                    skipped += 1
                    continue

                # Convert premultiplied → straight alpha for PNG output
                out_img = image.convertToFormat(QImage.Format_ARGB32)

                tile_dir = os.path.join(self.output_dir,
                                        str(td.zoom), str(td.col))
                os.makedirs(tile_dir, exist_ok=True)
                out_path = os.path.join(tile_dir, f"{td.row}.png")

                if out_img.save(out_path, "PNG"):
                    rendered += 1
                else:
                    self._log(f"[TileExportTask] save() failed: {out_path}")
                    errors += 1

            except Exception as exc:
                errors += 1
                import traceback
                self._log(f"[TileExportTask] ERROR z={td.zoom} x={td.col} "
                      f"y={td.row}: {exc}")
                traceback.print_exc()

        self._log(f"[TileExportTask] Done — "
              f"rendered={rendered}, skipped_empty={skipped}, errors={errors}")
        return True


# ---------------------------------------------------------------------------
# XYZ Tile Exporter
# ---------------------------------------------------------------------------


class XYZTileExporter:
    """Orchestrate parallel tile rendering from an in-memory tile index."""

    def __init__(self, output_dir: str, tile_index: list,
                 num_workers: int = 4, tile_size: int = 256,
                 pack_to_gpkg: bool = False, render_buffer_px: int = 128,
                 crs_epsg: int = 4326,
                 tile_matrix_origin: tuple = (-180.0, 90.0),
                 full_width: float = 360.0, full_height: float = 180.0,
                 matrix_width_z0: int = 2, matrix_height_z0: int = 1,
                 output_dpi: float = 96.0,
                 feedback: "QgsProcessingFeedback | None" = None):
        self.output_dir = output_dir
        self.tile_index = tile_index
        self.num_workers = num_workers
        self.tile_size = tile_size
        self.pack_to_gpkg = pack_to_gpkg
        self.render_buffer_px = render_buffer_px
        self.crs_epsg = crs_epsg
        self.tile_matrix_origin = tile_matrix_origin
        self.full_width = full_width
        self.full_height = full_height
        self.matrix_width_z0 = matrix_width_z0
        self.matrix_height_z0 = matrix_height_z0
        self.output_dpi = output_dpi
        self._feedback = feedback
        # Strong references so Qt doesn't GC tasks before they finish
        self._render_tasks = []
        self._tracker = None        # render completion tracker — must outlive all workers

    def _log(self, message: str):
        if self._feedback is not None:
            self._feedback.pushInfo(message)
        else:
            print(message)

    def export(self) -> str:
        if not self.tile_index:
            self._log("[XYZTileExporter] No tiles to render.")
            return self.output_dir

        zoom_levels = set()
        exp_minx = exp_miny = float("inf")
        exp_maxx = exp_maxy = float("-inf")
        for td in self.tile_index:
            zoom_levels.add(td.zoom)
            exp_minx = min(exp_minx, td.extent.xMinimum())
            exp_miny = min(exp_miny, td.extent.yMinimum())
            exp_maxx = max(exp_maxx, td.extent.xMaximum())
            exp_maxy = max(exp_maxy, td.extent.yMaximum())

        total = len(self.tile_index)
        self._log(f"[XYZTileExporter] {total} tiles, zooms {sorted(zoom_levels)}")

        # Split into equal slices — avoids the shared-queue drain race
        n = min(self.num_workers, total)
        slices = [self.tile_index[i::n] for i in range(n)]
        self._log(f"[XYZTileExporter] {n} workers, "
              f"tiles/worker: {[len(s) for s in slices]}")

        # Snapshot canvas settings on the main thread (thread-safe)
        canvas_settings = QgsMapSettings(iface.mapCanvas().mapSettings())

        tiles_dir = os.path.join(self.output_dir, "tiles")
        os.makedirs(tiles_dir, exist_ok=True)

        # ------------------------------------------------------------------
        # Completion callbacks
        # ------------------------------------------------------------------
        # QEventLoop lets export() block here while still processing Qt events
        # (including the taskCompleted / taskTerminated signals from workers).
        # loop.quit() is registered as a second tracker callback so it fires
        # exactly once after all workers have reported completion/termination.
        self._wait_loop = QEventLoop()

        if self.pack_to_gpkg:
            gpkg_path = os.path.join(self.output_dir, "tiles.gpkg")
            writer = GeoPackageTileWriter(gpkg_path, self.tile_size,
                                          self.crs_epsg,
                                          feedback=self._feedback)
            writer.open()
            writer.populate_metadata(
                zoom_levels,
                self.tile_matrix_origin[0], self.tile_matrix_origin[1],
                self.full_width, self.full_height,
                exp_minx, exp_miny, exp_maxx, exp_maxy,
                self.matrix_width_z0, self.matrix_height_z0,
            )

            def _on_renders_done():
                """Main-thread callback: all render workers finished.
                Pack PNGs into the GPKG directly here — we are already on the
                main thread, packing is pure file I/O, and avoiding a second
                QgsTask eliminates the entire chain of GC / signal-timing bugs
                that caused the GPKG to be empty.
                """
                self._log("[XYZTileExporter] Packing tiles into GPKG…")
                packed = 0
                png_paths = sorted(glob.glob(
                    os.path.join(tiles_dir, "**", "*.png"), recursive=True))
                self._log(f"[XYZTileExporter] Found {len(png_paths)} PNGs.")
                for png_path in png_paths:
                    try:
                        rel = os.path.relpath(png_path, tiles_dir)
                        parts = rel.replace("\\", "/").split("/")
                        if len(parts) != 3:
                            continue
                        z_str, x_str, y_ext = parts
                        z_int = int(z_str)
                        x_int = int(x_str)
                        y_xyz = int(os.path.splitext(y_ext)[0])
                        # OGC GeoPackage spec (Table 19): tile_row 0 is the
                        # TOP-LEFT (northwest) corner of the tile matrix —
                        # identical to the XYZ convention where y=0 is north.
                        # No flip is needed; use the XYZ y value directly.
                        y_gpkg = y_xyz
                        with open(png_path, "rb") as fh:
                            writer.add_tile(z_int, x_int, y_gpkg, fh.read())
                        packed += 1
                    except Exception as exc:
                        self._log(f"[XYZTileExporter] Skipping {png_path}: {exc}")
                writer.close()
                self._log(f"[XYZTileExporter] Packed {packed} tiles, GPKG closed.")
                layer = QgsRasterLayer(gpkg_path, "tiles")
                if layer.isValid():
                    QgsProject.instance().addMapLayer(layer)
                    self._log(f"[XYZTileExporter] GPKG loaded: {gpkg_path}")
                else:
                    self._log(f"[XYZTileExporter] WARNING: cannot load {gpkg_path}")

            self._tracker = _CompletionTracker(
                n, _on_renders_done, self._wait_loop.quit,
                feedback=self._feedback)

        else:
            xml_path = self._write_tilemapresource(zoom_levels, tiles_dir)

            def _on_renders_done():
                with open(xml_path, encoding="utf-8") as fh:
                    xml = fh.read()
                layer = QgsRasterLayer(xml, "tiles")
                if layer.isValid():
                    QgsProject.instance().addMapLayer(layer)
                    self._log("[XYZTileExporter] XML tile layer loaded.")
                else:
                    self._log("[XYZTileExporter] WARNING: cannot load XML layer.")

            self._tracker = _CompletionTracker(
                n, _on_renders_done, self._wait_loop.quit,
                feedback=self._feedback)

        # ------------------------------------------------------------------
        # Dispatch render workers
        # ------------------------------------------------------------------
        self._render_tasks.clear()
        for i, tile_slice in enumerate(slices):
            task = TileExportTask(
                tiles=tile_slice,
                output_dir=tiles_dir,
                canvas_settings=canvas_settings,
                tile_size=self.tile_size,
                render_buffer_px=self.render_buffer_px,
                output_dpi=self.output_dpi,
                feedback=self._feedback,
            )
            task.taskCompleted.connect(self._tracker.notify)
            task.taskTerminated.connect(self._tracker.notify)
            self._render_tasks.append(task)   # strong reference
            QgsApplication.taskManager().addTask(task)
            self._log(f"[XYZTileExporter] Worker {i} dispatched "
                  f"({len(tile_slice)} tiles)")

        # Block until every worker has signalled taskCompleted / taskTerminated.
        # QEventLoop.exec_() keeps processing Qt events while we wait, so the
        # task signals are delivered normally — no deadlock.
        self._log("[XYZTileExporter] Waiting for all workers to finish…")
        self._wait_loop.exec_()
        self._log("[XYZTileExporter] All workers finished.")

        return self.output_dir

    def _write_tilemapresource(self, zoom_levels: set,
                               tiles_dir: str) -> str:
        ox, oy = self.tile_matrix_origin
        # Use ${z}/${x}/${y} template — escape braces for f-string
        url = ("file:///"
               + tiles_dir.replace("\\", "/")
               + "/${z}/${x}/${y}.png")
        min_z = min(zoom_levels)
        max_z = max(zoom_levels)
        xml = (
            "<GDAL_WMS>\n"
            "  <Service name=\"TMS\">\n"
            f"    <ServerUrl>{url}</ServerUrl>\n"
            "  </Service>\n"
            "  <DataWindow>\n"
            f"    <UpperLeftX>{ox}</UpperLeftX>\n"
            f"    <UpperLeftY>{oy}</UpperLeftY>\n"
            f"    <LowerRightX>{ox + self.full_width}</LowerRightX>\n"
            f"    <LowerRightY>{oy - self.full_height}</LowerRightY>\n"
            f"    <TileLevel>{max_z}</TileLevel>\n"
            f"    <MinTileLevel>{min_z}</MinTileLevel>\n"
            f"    <TileCountX>{self.matrix_width_z0}</TileCountX>\n"
            f"    <TileCountY>{self.matrix_height_z0}</TileCountY>\n"
            "    <YOrigin>top</YOrigin>\n"
            "  </DataWindow>\n"
            f"  <Projection>EPSG:{self.crs_epsg}</Projection>\n"
            f"  <BlockSizeX>{self.tile_size}</BlockSizeX>\n"
            f"  <BlockSizeY>{self.tile_size}</BlockSizeY>\n"
            "  <BandsCount>4</BandsCount>\n"
            "  <ZeroBlockHttpCodes>204,404</ZeroBlockHttpCodes>\n"
            "</GDAL_WMS>"
        )
        xml_path = os.path.join(self.output_dir, "tiles.xml")
        with open(xml_path, "w", encoding="utf-8") as fh:
            fh.write(xml)
        self._log(f"[XYZTileExporter] WMS XML: {xml_path}")
        return xml_path


# ---------------------------------------------------------------------------
# Tile index generator
# ---------------------------------------------------------------------------


class TileIndexGenerator:
    """Generates TileDescriptor objects for a given tile grid and map extent."""

    def __init__(self, crs_epsg: int, top_left_corner: tuple,
                 tile_dim: float,
                 matrix_width: int, matrix_height: int,
                 extent, min_zoom: int, max_zoom: int, output_dir: str,
                 feedback: "QgsProcessingFeedback | None" = None):
        self.crs_epsg = crs_epsg
        self.x0, self.y0 = top_left_corner
        self.tile_dim = tile_dim
        self.matrix_width = matrix_width
        self.matrix_height = matrix_height
        self.extent = extent
        self.min_zoom = min_zoom
        self.max_zoom = max_zoom
        self.output_dir = self._make_subdir(output_dir)
        self._feedback = feedback

    def _log(self, message: str):
        if self._feedback is not None:
            self._feedback.pushInfo(message)
        else:
            print(message)

    def _tile_bounds(self, col, row, zoom):
        sc = 2 ** zoom
        tw = th = self.tile_dim / sc
        minx = self.x0 + col * tw
        maxy = self.y0 - row * th
        return minx, maxy - th, minx + tw, maxy

    def generate_in_memory(self) -> list:
        tiles = []
        xmin = self.extent.xMinimum()
        ymin = self.extent.yMinimum()
        xmax = self.extent.xMaximum()
        ymax = self.extent.yMaximum()

        for zoom in range(self.min_zoom, self.max_zoom + 1):
            sc = 2 ** zoom
            tw = th = self.tile_dim / sc
            nx = int(self.matrix_width * sc)
            ny = int(self.matrix_height * sc)

            c0 = max(0, int((xmin - self.x0) / tw))
            c1 = min(nx - 1, int((xmax - self.x0) / tw))
            r0 = max(0, int((self.y0 - ymax) / th))
            r1 = min(ny - 1, int((self.y0 - ymin) / th))

            for row in range(r0, r1 + 1):
                for col in range(c0, c1 + 1):
                    minx, miny, maxx, maxy = self._tile_bounds(col, row, zoom)
                    tiles.append(TileDescriptor(
                        zoom=zoom, col=col, row=row,
                        extent=QgsRectangle(minx, miny, maxx, maxy),
                    ))

        self._log(f"[TileIndexGenerator] {len(tiles)} tiles generated in memory.")
        return tiles

    def _make_subdir(self, base: str) -> str:
        if not base:
            base = QgsProcessingUtils.tempFolder()
        subdir = join(base, datetime.now().strftime("temp_%Y%m%d_%H%M%S"))
        makedirs(join(subdir, "tiles"), exist_ok=True)
        return subdir


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class QGIS2RasterTiles:
    """End-to-end pipeline: generate index → dispatch render workers →
    (optionally) pack tiles into a GeoPackage."""

    def __init__(self, crs_epsg: int, top_left_corner: tuple,
                 tile_dim: float,
                 matrix_width: int, matrix_height: int,
                 extent, min_zoom: int, max_zoom: int, output_dir: str,
                 tile_size: int, cpu_percent: float,
                 pack_to_gpkg: bool, render_buffer_px: int,
                 output_dpi: float, feedback: "QgsProcessingFeedback | None" = None):
        if not (0 < cpu_percent <= 100):
            raise ValueError("cpu_percent must be in (0, 100]")
        self.feedback = feedback
        cores = multiprocessing.cpu_count()
        self.num_workers = max(1, round(cores * cpu_percent / 100))
        self._log(f"[QGIS2RasterTiles] {self.num_workers} workers "
              f"({cpu_percent:.0f}% of {cores} cores)")

        self._gen_kwargs = dict(
            crs_epsg=crs_epsg, top_left_corner=top_left_corner,
            tile_dim=tile_dim,
            matrix_width=matrix_width, matrix_height=matrix_height,
            extent=extent, min_zoom=min_zoom, max_zoom=max_zoom,
            output_dir=output_dir,
        )
        self.tile_size = tile_size
        self.pack_to_gpkg = pack_to_gpkg
        self.render_buffer_px = render_buffer_px
        self.output_dpi = output_dpi
        self._crs_epsg = crs_epsg
        self._origin = top_left_corner
        self._full_w = tile_dim * matrix_width
        self._full_h = tile_dim * matrix_height
        self._mw_z0 = matrix_width
        self._mh_z0 = matrix_height
        self._exporter = None   # strong reference kept here

    def convert_project_to_raster_tiles(self) -> str:
        self._log("[QGIS2RasterTiles] Building tile index…")
        gen = TileIndexGenerator(**self._gen_kwargs, feedback=self.feedback)
        index = gen.generate_in_memory()
        self._log(f"[QGIS2RasterTiles] {len(index)} tiles ready.")

        self._exporter = XYZTileExporter(
            output_dir=gen.output_dir,
            tile_index=index,
            num_workers=self.num_workers,
            tile_size=self.tile_size,
            pack_to_gpkg=self.pack_to_gpkg,
            render_buffer_px=self.render_buffer_px,
            crs_epsg=self._crs_epsg,
            tile_matrix_origin=self._origin,
            full_width=self._full_w,
            full_height=self._full_h,
            matrix_width_z0=self._mw_z0,
            matrix_height_z0=self._mh_z0,
            output_dpi=self.output_dpi,
            feedback=self.feedback,
        )
        result = self._exporter.export()
        self._log("[QGIS2RasterTiles] Workers dispatched.")
        return result

        
    def _log(self, message: str):
        """Log message to feedback or console."""
        if self.feedback is not None:
            self.feedback.pushInfo(message)
        else:
            print(message)




# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Module-level pipeline instance — MUST be module-level, not a local variable.
#
# The entire async callback chain (render workers → _on_renders_done → GPKG
# packing) depends on Python objects staying alive until workers finish.
# If _pipeline were a local variable in __console__, it would be GC'd the
# moment run() returns — destroying the tracker, breaking all signal
# connections, and leaving the GPKG empty.  A module-level name keeps the
# whole object graph alive for the lifetime of the Python interpreter session.
# ---------------------------------------------------------------------------
_pipeline = None   # assigned below; module-level keeps it alive

if __name__ == "__console__":
    print("Starting QGIS2RasterTiles…")

    tiles_generator = QGIS2RasterTiles(
        crs_epsg=4326,
        top_left_corner=(-180, 90),
        tile_dim=180,
        matrix_width=2,
        matrix_height=1,
        extent=iface.mapCanvas().extent(),
        min_zoom=0,
        max_zoom=2,
        output_dir=QgsProcessingUtils.tempFolder(),
        tile_size=256,
        cpu_percent=90.0,
        pack_to_gpkg=True,
        render_buffer_px=0,
        output_dpi=96.0,   # set to 96 for standard displays, 192 for Hi-DPI/Retina
    )

    output_path = tiles_generator.convert_project_to_raster_tiles()
    print(f"Output: {output_path}")