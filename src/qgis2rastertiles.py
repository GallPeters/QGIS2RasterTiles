"""QGIS2RasterTiles — High-Performance Export to XYZ raster tiles / GeoPackage."""

import os
import glob
import sqlite3
import threading
import queue
import traceback
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
    QgsProcessingFeedback
)
from qgis.PyQt.QtCore import QSize, Qt, QEventLoop, QByteArray, QBuffer, QIODevice
from qgis.PyQt.QtGui import QImage, QPainter
from qgis.utils import iface


TileDescriptor = namedtuple("TileDescriptor", ["zoom", "col", "row", "extent"])


# ---------------------------------------------------------------------------
# Shared logging helper
# ---------------------------------------------------------------------------

def _log(message: str, feedback: "QgsProcessingFeedback | None" = None) -> None:
    if feedback is not None:
        feedback.pushInfo(message)
    else:
        print(message)


# ---------------------------------------------------------------------------
# Completion tracker
# ---------------------------------------------------------------------------

class _CompletionTracker:
    """Thread-safe counter that fires callbacks when all tasks have completed."""
    def __init__(self, total: int, *callbacks, feedback: "QgsProcessingFeedback | None" = None):
        self._remaining = total
        self._lock = threading.Lock()
        self._callbacks = list(callbacks)
        self._feedback = feedback

    def notify(self):
        with self._lock:
            self._remaining -= 1
            remaining = self._remaining
            _log(f"Render chunk completed, {remaining} chunks remaining.", self._feedback)
        if remaining <= 0:
            for cb in self._callbacks:
                cb()


# ---------------------------------------------------------------------------
# GeoPackage Setup & High-Speed Writer Thread
# ---------------------------------------------------------------------------

def setup_gpkg_schema(gpkg_path: str, crs_epsg: int, zoom_levels: set,
                      origin_x: float, origin_y: float, full_w: float, full_h: float,
                      exp_minx: float, exp_miny: float, exp_maxx: float, exp_maxy: float,
                      mw_z0: int, mh_z0: int, tile_size: int):
    """Initialize the OGC GeoPackage schema and metadata on the main thread before rendering."""
    conn = sqlite3.connect(gpkg_path)
    cur = conn.cursor()
    
    # Standard setup pragmas
    cur.execute("PRAGMA application_id = 1196444487")
    cur.execute("PRAGMA user_version = 10200")
    
    # Create Schema
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS gpkg_spatial_ref_sys (
            srs_name TEXT NOT NULL, srs_id INTEGER PRIMARY KEY, organization TEXT NOT NULL,
            organization_coordsys_id INTEGER NOT NULL, definition TEXT NOT NULL, description TEXT);
            
        CREATE TABLE IF NOT EXISTS gpkg_contents (
            table_name TEXT NOT NULL PRIMARY KEY, data_type TEXT NOT NULL, identifier TEXT UNIQUE,
            description TEXT DEFAULT '', last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            min_x REAL, min_y REAL, max_x REAL, max_y REAL, srs_id INTEGER REFERENCES gpkg_spatial_ref_sys(srs_id));
            
        CREATE TABLE IF NOT EXISTS gpkg_tile_matrix_set (
            table_name TEXT NOT NULL PRIMARY KEY REFERENCES gpkg_contents(table_name),
            srs_id INTEGER NOT NULL REFERENCES gpkg_spatial_ref_sys(srs_id),
            min_x REAL NOT NULL, min_y REAL NOT NULL, max_x REAL NOT NULL, max_y REAL NOT NULL);
            
        CREATE TABLE IF NOT EXISTS gpkg_tile_matrix (
            table_name TEXT NOT NULL REFERENCES gpkg_contents(table_name), zoom_level INTEGER NOT NULL,
            matrix_width INTEGER NOT NULL, matrix_height INTEGER NOT NULL, tile_width INTEGER NOT NULL, tile_height INTEGER NOT NULL,
            pixel_x_size REAL NOT NULL, pixel_y_size REAL NOT NULL, PRIMARY KEY (table_name, zoom_level));
            
        CREATE TABLE IF NOT EXISTS tiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT, zoom_level INTEGER NOT NULL,
            tile_column INTEGER NOT NULL, tile_row INTEGER NOT NULL, tile_data BLOB NOT NULL,
            UNIQUE (zoom_level, tile_column, tile_row));
    """)
    
    # Populate Metadata
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(crs_epsg)
    wkt = srs.ExportToWkt()
    
    cur.executemany(
        "INSERT OR IGNORE INTO gpkg_spatial_ref_sys (srs_name,srs_id,organization,organization_coordsys_id,definition) VALUES (?,?,?,?,?)",
        [("Undefined Cartesian", 0, "NONE", 0, "undefined"), ("Undefined Geographic", -1, "NONE", -1, "undefined")]
    )
    cur.execute(
        "INSERT OR REPLACE INTO gpkg_spatial_ref_sys (srs_name,srs_id,organization,organization_coordsys_id,definition) VALUES (?,?,?,?,?)",
        (srs.GetName(), crs_epsg, "EPSG", crs_epsg, wkt)
    )
    cur.execute(
        "INSERT OR REPLACE INTO gpkg_contents (table_name,data_type,identifier,description,min_x,min_y,max_x,max_y,srs_id) VALUES ('tiles','tiles','tiles','XYZ tile export',?,?,?,?,?)",
        (exp_minx, exp_miny, exp_maxx, exp_maxy, crs_epsg)
    )
    cur.execute(
        "INSERT OR REPLACE INTO gpkg_tile_matrix_set (table_name,srs_id,min_x,min_y,max_x,max_y) VALUES ('tiles',?,?,?,?,?)",
        (crs_epsg, origin_x, origin_y - full_h, origin_x + full_w, origin_y)
    )
    
    for z in sorted(zoom_levels):
        sc = 2 ** z
        mw, mh = mw_z0 * sc, mh_z0 * sc
        cur.execute(
            "INSERT OR REPLACE INTO gpkg_tile_matrix (table_name,zoom_level,matrix_width,matrix_height,tile_width,tile_height,pixel_x_size,pixel_y_size) VALUES ('tiles',?,?,?,?,?,?,?)",
            (z, mw, mh, tile_size, tile_size, full_w / (mw * tile_size), full_h / (mh * tile_size))
        )
        
    conn.commit()
    conn.close()


class GeoPackageQueueWriter(threading.Thread):
    """Dedicated background thread pulling PNG bytes from RAM and bulk-inserting to SQLite."""
    BATCH_SIZE = 2000

    def __init__(self, gpkg_path: str, tile_queue: queue.Queue, feedback: "QgsProcessingFeedback | None" = None):
        super().__init__(name="GPKG-Writer-Thread")
        self.gpkg_path = gpkg_path
        self.tile_queue = tile_queue
        self._feedback = feedback
        self._buffer = []
        self._conn = None

    def run(self):
        self._conn = sqlite3.connect(self.gpkg_path, timeout=60.0)
        self._setup_extreme_pragmas()
        
        inserted_count = 0
        while True:
            item = self.tile_queue.get()
            
            if item is None:  # Sentinel value to terminate thread
                self._flush()
                self._conn.close()
                self.tile_queue.task_done()
                _log(f"GeoPackage writer thread finished. Total DB inserts: {inserted_count}", self._feedback)
                break
                
            self._buffer.append(item)
            inserted_count += 1
            
            if len(self._buffer) >= self.BATCH_SIZE:
                self._flush()
                
            self.tile_queue.task_done()

    def _setup_extreme_pragmas(self):
        cur = self._conn.cursor()
        # Extreme pragmas for maximum bulk-insert speed. No crash recovery!
        pragmas = [
            "PRAGMA journal_mode = OFF",
            "PRAGMA synchronous = OFF",
            "PRAGMA locking_mode = EXCLUSIVE",
            "PRAGMA cache_size = -262144", # 256MB cache
            "PRAGMA temp_store = MEMORY"
        ]
        for p in pragmas:
            cur.execute(p)
        self._conn.commit()

    def _flush(self):
        if not self._buffer: return
        self._conn.executemany(
            "INSERT OR REPLACE INTO tiles (zoom_level,tile_column,tile_row,tile_data) VALUES (?,?,?,?)",
            self._buffer
        )
        self._conn.commit()
        self._buffer.clear()


# ---------------------------------------------------------------------------
# Tile render worker
# ---------------------------------------------------------------------------

class TileExportTask(QgsTask):
    """Renders tiles and pushes raw PNG bytes directly to the thread queue."""

    def __init__(self, tiles: list, output_dir: str, canvas_settings: QgsMapSettings,
                 tile_queue: queue.Queue = None, tile_size: int = 256, render_buffer_px: int = 128,
                 output_dpi: float = 96.0, feedback: "QgsProcessingFeedback | None" = None):
        super().__init__("Tile Export Worker", QgsTask.CanCancel)
        self.tiles = tiles
        self.output_dir = output_dir
        self.tile_queue = tile_queue  # If None, write to disk
        self.tile_size = tile_size
        self.render_buffer_px = render_buffer_px
        self.output_dpi = output_dpi
        self._feedback = feedback
        self._base_settings = self._build_base_settings(canvas_settings)
        self._render_size = tile_size + 2 * render_buffer_px

    def _build_base_settings(self, src: QgsMapSettings) -> QgsMapSettings:
        labeling_settings = QgsLabelingEngineSettings()
        labeling_settings.readSettingsFromProject(QgsProject.instance())

        s = QgsMapSettings()
        s.setLayers(src.layers())
        s.setDestinationCrs(src.destinationCrs())
        s.setOutputSize(QSize(self._render_size, self._render_size))
        s.setOutputDpi(self.output_dpi * src.devicePixelRatio())
        s.setBackgroundColor(Qt.transparent)
        s.setFlag(QgsMapSettings.DrawLabeling, src.testFlag(QgsMapSettings.DrawLabeling))
        s.setFlag(QgsMapSettings.UseAdvancedEffects, src.testFlag(QgsMapSettings.UseAdvancedEffects))
        s.setFlag(QgsMapSettings.ForceVectorOutput, True)
        s.setTransformContext(src.transformContext())
        s.setLabelingEngineSettings(labeling_settings)
        s.setScaleMethod(src.scaleMethod())
        return s

    def _render_tile(self, extent: QgsRectangle) -> QImage:
        mupx = extent.width() / self.tile_size
        buf_mu = self.render_buffer_px * mupx
        expanded = QgsRectangle(
            extent.xMinimum() - buf_mu, extent.yMinimum() - buf_mu,
            extent.xMaximum() + buf_mu, extent.yMaximum() + buf_mu,
        )

        settings = QgsMapSettings(self._base_settings)
        settings.setExtent(expanded)
        settings.setLabelBoundaryGeometry(QgsGeometry.fromWkt(extent.asWktPolygon()))

        image = QImage(self._render_size, self._render_size, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        
        painter = QPainter(image)
        job = QgsMapRendererCustomPainterJob(settings, painter)
        job.renderSynchronously()
        painter.end()

        return image.copy(
            self.render_buffer_px, self.render_buffer_px,
            self.tile_size, self.tile_size,
        )

    @staticmethod
    def _has_content(image: QImage) -> bool:
        """Absolute Zero-copy check for empty tiles directly on memory buffer."""
        ptr = image.constBits()
        ptr.setsize(image.byteCount())
        return any(memoryview(ptr)[3::4])

    def run(self) -> bool:
        rendered = skipped = errors = 0
        created_dirs = set()

        for td in self.tiles:
            if self.isCanceled(): break
            try:
                image = self._render_tile(td.extent)

                if not self._has_content(image):
                    skipped += 1
                    continue

                if self.tile_queue is not None:
                    # PRODUCER MODE: Encode entirely in RAM and push to DB thread
                    ba = QByteArray()
                    buf = QBuffer(ba)
                    buf.open(QIODevice.WriteOnly)
                    image.save(buf, "PNG")
                    
                    self.tile_queue.put((td.zoom, td.col, td.row, sqlite3.Binary(ba.data())))
                    rendered += 1
                    
                    # Force garbage collection in the tight loop
                    buf = None
                    ba = None
                    
                else:
                    # FALLBACK DISK MODE
                    out_img = image.convertToFormat(QImage.Format_ARGB32)
                    tile_dir = os.path.join(self.output_dir, str(td.zoom), str(td.col))
                    if tile_dir not in created_dirs:
                        os.makedirs(tile_dir, exist_ok=True)
                        created_dirs.add(tile_dir)

                    out_path = os.path.join(tile_dir, f"{td.row}.png")
                    if out_img.save(out_path, "PNG"):
                        rendered += 1
                    else:
                        errors += 1
                        
                image = None

            except Exception as exc:
                errors += 1
                _log(f"Error rendering tile z={td.zoom} x={td.col} y={td.row}: {exc}", self._feedback)
                traceback.print_exc()

        _log(f"Worker finished chunk — rendered: {rendered}, skipped (empty): {skipped}, errors: {errors}", self._feedback)
        return True


# ---------------------------------------------------------------------------
# XYZ tile exporter / orchestrator
# ---------------------------------------------------------------------------

class XYZTileExporter:
    """Orchestrate parallel tile rendering and coordinate the Producer-Consumer queue."""

    def __init__(self, output_dir: str, tile_index: list, num_workers: int = 4, tile_size: int = 256,
                 pack_to_gpkg: bool = False, render_buffer_px: int = 128, crs_epsg: int = 4326,
                 tile_matrix_origin: tuple = (-180.0, 90.0), full_width: float = 360.0, full_height: float = 180.0,
                 matrix_width_z0: int = 2, matrix_height_z0: int = 1, output_dpi: float = 96.0,
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
        self._render_tasks = []
        self._tracker = None

    def export(self) -> str:
        if not self.tile_index:
            return self.output_dir

        zoom_levels = set()
        exp_minx = exp_miny = float("inf")
        exp_maxx = exp_maxy = float("-inf")
        for td in self.tile_index:
            zoom_levels.add(td.zoom)
            e = td.extent
            if e.xMinimum() < exp_minx: exp_minx = e.xMinimum()
            if e.yMinimum() < exp_miny: exp_miny = e.yMinimum()
            if e.xMaximum() > exp_maxx: exp_maxx = e.xMaximum()
            if e.yMaximum() > exp_maxy: exp_maxy = e.yMaximum()

        total = len(self.tile_index)
        n = min(self.num_workers, total)

        chunk_size = (total + n - 1) // n
        slices = [self.tile_index[i * chunk_size:(i + 1) * chunk_size] for i in range(n) if self.tile_index[i * chunk_size:(i + 1) * chunk_size]]

        canvas_settings = QgsMapSettings(iface.mapCanvas().mapSettings())
        tiles_dir = os.path.join(self.output_dir, "tiles")
        os.makedirs(tiles_dir, exist_ok=True)

        self._wait_loop = QEventLoop()
        tile_queue = None

        if self.pack_to_gpkg:
            gpkg_path = os.path.join(self.output_dir, "tiles.gpkg")
            
            # Setup metadata on main thread first
            setup_gpkg_schema(gpkg_path, self.crs_epsg, zoom_levels, self.tile_matrix_origin[0], self.tile_matrix_origin[1],
                              self.full_width, self.full_height, exp_minx, exp_miny, exp_maxx, exp_maxy,
                              self.matrix_width_z0, self.matrix_height_z0, self.tile_size)
            
            # Create bounded queue (prevents RAM exhaustion if rendering outpaces DB writes)
            tile_queue = queue.Queue(maxsize=10000)
            
            # Start Consumer Thread
            writer_thread = GeoPackageQueueWriter(gpkg_path, tile_queue, self._feedback)
            writer_thread.start()

            def _on_renders_done():
                _log("Render workers finished. Waiting for database to finish writing...", self._feedback)
                tile_queue.put(None)  # Send kill signal
                writer_thread.join()  # Block until all tiles are written
                
                layer = QgsRasterLayer(gpkg_path, "tiles")
                if layer.isValid():
                    QgsProject.instance().addMapLayer(layer)
                    _log(f"GeoPackage loaded successfully: {gpkg_path}", self._feedback)
                else:
                    _log(f"WARNING: could not load GeoPackage layer: {gpkg_path}", self._feedback)

            self._tracker = _CompletionTracker(len(slices), _on_renders_done, self._wait_loop.quit, feedback=self._feedback)

        else:
            # Fallback to XML workflow
            xml_path = self._write_tilemapresource(zoom_levels, tiles_dir)
            def _on_renders_done():
                layer = QgsRasterLayer(xml_path, "tiles")
                if layer.isValid():
                    QgsProject.instance().addMapLayer(layer)
            self._tracker = _CompletionTracker(len(slices), _on_renders_done, self._wait_loop.quit, feedback=self._feedback)

        # Dispatch Producers
        self._render_tasks.clear()
        for i, tile_slice in enumerate(slices):
            task = TileExportTask(
                tiles=tile_slice, output_dir=tiles_dir, canvas_settings=canvas_settings,
                tile_queue=tile_queue, tile_size=self.tile_size, render_buffer_px=self.render_buffer_px,
                output_dpi=self.output_dpi, feedback=self._feedback,
            )
            task.taskCompleted.connect(self._tracker.notify)
            task.taskTerminated.connect(self._tracker.notify)
            self._render_tasks.append(task)
            QgsApplication.taskManager().addTask(task)

        _log("Waiting for rendering pipeline to complete...", self._feedback)
        self._wait_loop.exec_()
        return self.output_dir

    def _write_tilemapresource(self, zoom_levels: set, tiles_dir: str) -> str:
        # Keep XML generation untouched for the fallback non-GPKG path
        ox, oy = self.tile_matrix_origin
        url = "file:///" + tiles_dir.replace("\\", "/") + "/${z}/${x}/${y}.png"
        min_z, max_z = min(zoom_levels), max(zoom_levels)
        xml = (
            f"<GDAL_WMS>\n  <Service name=\"TMS\">\n    <ServerUrl>{url}</ServerUrl>\n  </Service>\n"
            f"  <DataWindow>\n    <UpperLeftX>{ox}</UpperLeftX>\n    <UpperLeftY>{oy}</UpperLeftY>\n"
            f"    <LowerRightX>{ox + self.full_width}</LowerRightX>\n    <LowerRightY>{oy - self.full_height}</LowerRightY>\n"
            f"    <TileLevel>{max_z}</TileLevel>\n    <MinTileLevel>{min_z}</MinTileLevel>\n"
            f"    <TileCountX>{self.matrix_width_z0}</TileCountX>\n    <TileCountY>{self.matrix_height_z0}</TileCountY>\n"
            f"    <YOrigin>top</YOrigin>\n  </DataWindow>\n  <Projection>EPSG:{self.crs_epsg}</Projection>\n"
            f"  <BlockSizeX>{self.tile_size}</BlockSizeX>\n  <BlockSizeY>{self.tile_size}</BlockSizeY>\n"
            f"  <BandsCount>4</BandsCount>\n  <ZeroBlockHttpCodes>204,404</ZeroBlockHttpCodes>\n</GDAL_WMS>"
        )
        xml_path = os.path.join(self.output_dir, "tiles.xml")
        with open(xml_path, "w", encoding="utf-8") as fh: fh.write(xml)
        return xml_path


# ---------------------------------------------------------------------------
# Tile index generator
# ---------------------------------------------------------------------------

class TileIndexGenerator:
    """Generates TileDescriptor objects for a given tile grid and map extent."""
    def __init__(self, crs_epsg: int, top_left_corner: tuple, tile_dim: float,
                 matrix_width: int, matrix_height: int, extent, min_zoom: int, max_zoom: int, output_dir: str,
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

    def _tile_bounds(self, col, row, zoom):
        sc = 2 ** zoom
        tw = th = self.tile_dim / sc
        minx = self.x0 + col * tw
        maxy = self.y0 - row * th
        return minx, maxy - th, minx + tw, maxy

    def generate_in_memory(self) -> list:
        tiles = []
        xmin, ymin = self.extent.xMinimum(), self.extent.yMinimum()
        xmax, ymax = self.extent.xMaximum(), self.extent.yMaximum()

        for zoom in range(self.min_zoom, self.max_zoom + 1):
            sc = 2 ** zoom
            tw = th = self.tile_dim / sc
            nx, ny = int(self.matrix_width * sc), int(self.matrix_height * sc)

            c0, c1 = max(0, int((xmin - self.x0) / tw)), min(nx - 1, int((xmax - self.x0) / tw))
            r0, r1 = max(0, int((self.y0 - ymax) / th)), min(ny - 1, int((self.y0 - ymin) / th))

            for row in range(r0, r1 + 1):
                for col in range(c0, c1 + 1):
                    minx, miny, maxx, maxy = self._tile_bounds(col, row, zoom)
                    tiles.append(TileDescriptor(zoom=zoom, col=col, row=row, extent=QgsRectangle(minx, miny, maxx, maxy)))

        _log(f"{len(tiles)} tiles generated for the selected extent.", self._feedback)
        return tiles

    def _make_subdir(self, base: str) -> str:
        if not base: base = QgsProcessingUtils.tempFolder()
        subdir = join(base, datetime.now().strftime("temp_%Y%m%d_%H%M%S"))
        makedirs(join(subdir, "tiles"), exist_ok=True)
        return subdir


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------

class QGIS2RasterTiles:
    """End-to-end pipeline: generate tile index, dispatch render workers, and pack to GeoPackage via Queue."""
    def __init__(self, crs_epsg: int, top_left_corner: tuple, tile_dim: float,
                 matrix_width: int, matrix_height: int, extent, min_zoom: int, max_zoom: int, output_dir: str,
                 tile_size: int, cpu_percent: float, pack_to_gpkg: bool, render_buffer_px: int,
                 output_dpi: float, feedback: "QgsProcessingFeedback | None" = None):
        if not (0 < cpu_percent <= 100):
            raise ValueError("cpu_percent must be in (0, 100]")
        self.feedback = feedback
        cores = multiprocessing.cpu_count()
        self.num_workers = max(1, round(cores * cpu_percent / 100))
        
        self._gen_kwargs = dict(
            crs_epsg=crs_epsg, top_left_corner=top_left_corner, tile_dim=tile_dim,
            matrix_width=matrix_width, matrix_height=matrix_height, extent=extent,
            min_zoom=min_zoom, max_zoom=max_zoom, output_dir=output_dir,
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

    def convert_project_to_raster_tiles(self) -> str:
        gen = TileIndexGenerator(**self._gen_kwargs, feedback=self.feedback)
        index = gen.generate_in_memory()

        self._exporter = XYZTileExporter(
            output_dir=gen.output_dir, tile_index=index, num_workers=self.num_workers,
            tile_size=self.tile_size, pack_to_gpkg=self.pack_to_gpkg, render_buffer_px=self.render_buffer_px,
            crs_epsg=self._crs_epsg, tile_matrix_origin=self._origin, full_width=self._full_w,
            full_height=self._full_h, matrix_width_z0=self._mw_z0, matrix_height_z0=self._mh_z0,
            output_dpi=self.output_dpi, feedback=self.feedback,
        )
        return self._exporter.export()


# ---------------------------------------------------------------------------
# Console entry point
# ---------------------------------------------------------------------------

if __name__ == "__console__":
    print("Starting High-Performance QGIS2RasterTiles…")

    tiles_generator = QGIS2RasterTiles(
        crs_epsg=4326,
        top_left_corner=(-180, 90),
        tile_dim=180,
        matrix_width=2,
        matrix_height=1,
        extent=iface.mapCanvas().extent(),
        min_zoom=0,
        max_zoom=17,
        output_dir=QgsProcessingUtils.tempFolder(),
        tile_size=256,
        cpu_percent=100.0,
        pack_to_gpkg=True,
        render_buffer_px=0,
        output_dpi=96.0,
    )

    output_path = tiles_generator.convert_project_to_raster_tiles()
    print(f"Output: {output_path}")