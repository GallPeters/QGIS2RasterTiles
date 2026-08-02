"""
xyz_gpkg_processing_provider.py

QGIS Processing provider and algorithm exposing `XyzGpkgExporter` in the
Processing toolbox.

Target platform: QGIS 4.0+ / Qt6 / PyQt6.


THREAD DISCIPLINE
=================

Processing gives an algorithm three lifecycle hooks with different thread
affinities, and using them correctly is the whole point of this file:

  * `prepareAlgorithm()`  - MAIN THREAD. Reads parameters and captures the
    project snapshot (active map theme, visible layers, style overrides,
    labeling engine settings, expression context, canvas extent). All of
    that touches live project and canvas state.

  * `processAlgorithm()`  - WORKER THREAD. Pure computation against the
    snapshot: occupancy analysis, rendering, GeoPackage writing. Touches
    no GUI object and mutates no project state.

  * `postProcessAlgorithm()` - MAIN THREAD. Adds the produced GeoPackage to
    the project.

The previous version did everything inside `processAlgorithm()`, which
meant `iface.mapCanvas()`, `QgsProject.writeEntry()`,
`QgsProject.setLabelingEngineSettings()` and finally
`QgsProject.addMapLayer()` were all being called from a background thread.
Setting the labeling engine settings in particular emits
`labelingEngineSettingsChanged`, which starts a canvas repaint on the main
thread at the exact moment worker threads are cloning those same layers.


WHY THE UI USED TO STOP RESPONDING
==================================

Rendering was already backgrounded, so the freeze was not a blocked event
loop - it was a flooded one:

  * `progress_callback` was wired straight to `feedback.setProgress()` and
    called once per tile. That emits a queued cross-thread signal which
    fans out to the dialog's progress bar and the task manager. Millions of
    tiles means millions of queued events arriving faster than the event
    loop drains them.

  * A `logging` handler mirrored *every* record into `feedback.pushInfo()`,
    on top of the exporter's own `QgsMessageLog` handler. That included one
    message per database batch flush and one per failed tile, each one
    appending to a `QTextEdit` whose repaint cost grows with its contents.

Progress is now throttled in the exporter and again here; log bridging is
rate limited, deduplicated and capped.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtGui import QIcon
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingOutputFile,
    QgsProcessingParameterEnum,
    QgsProcessingParameterExtent,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterNumber,
    QgsProcessingProvider,
    QgsRasterLayer,
)

try:
    from .xyz_gpkg_exporter import (
        LOG_TAG,
        ProjectRenderSnapshot,
        XyzGpkgExporter,
        compute_worker_count,
    )
except ImportError:  # running as a loose script / from the console
    from xyz_gpkg_exporter import (  # type: ignore[no-redef]
        LOG_TAG,
        ProjectRenderSnapshot,
        XyzGpkgExporter,
        compute_worker_count,
    )


TILE_FORMATS = ("PNG", "JPEG", "WEBP", "JPEG2000")

# Feedback bridge limits. These exist purely to keep the dialog's log widget
# from being flooded with queued cross-thread appends.
FEEDBACK_MIN_INTERVAL_S = 0.25
FEEDBACK_MAX_ERRORS = 25
FEEDBACK_MAX_WARNINGS = 25


# --------------------------------------------------------------------------
# QGIS 3.x -> 4.x enum compatibility
# --------------------------------------------------------------------------

def _resolve(*candidates):
    for factory in candidates:
        try:
            value = factory()
        except (AttributeError, TypeError):
            continue
        if value is not None:
            return value
    return None


_INT_PARAM = _resolve(
    lambda: Qgis.ProcessingNumberParameterType.Integer,
    lambda: QgsProcessingParameterNumber.Type.Integer,
    lambda: QgsProcessingParameterNumber.Integer,
)
_FLAG_SUPPORTS_BATCH = _resolve(
    lambda: Qgis.ProcessingAlgorithmFlag.SupportsBatch,
    lambda: QgsProcessingAlgorithm.Flag.FlagSupportsBatch,
)
_FLAG_REQUIRES_PROJECT = _resolve(
    lambda: Qgis.ProcessingAlgorithmFlag.RequiresProject,
    lambda: QgsProcessingAlgorithm.Flag.FlagRequiresProject,
)


# --------------------------------------------------------------------------
# Rate-limited logging bridge
# --------------------------------------------------------------------------

class _FeedbackLogHandler(logging.Handler):
    """Mirrors the exporter's `logging` records into the Processing dialog.

    Deliberately lossy. `QgsProcessingFeedback.push*()` emits a queued signal
    that appends to a GUI text widget on the main thread; an unbounded feed
    from a multi-million-tile run is what made the dialog stop responding.

    Three limits apply:
      * INFO records are dropped if one was pushed less than
        `FEEDBACK_MIN_INTERVAL_S` ago (the exporter no longer logs per tile,
        so this is only a safety net).
      * Identical consecutive messages collapse into one.
      * Errors and warnings are capped, with a single summary line when the
        cap is reached. Nothing is lost overall - the full record still goes
        to the Log Messages panel via the exporter's own handler.
    """

    def __init__(self, feedback):
        super().__init__()
        self._feedback = feedback
        self._last_info = 0.0
        self._last_message: Optional[str] = None
        self._errors = 0
        self._warnings = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            return
        if message == self._last_message:
            return

        try:
            if record.levelno >= logging.ERROR:
                self._errors += 1
                if self._errors < FEEDBACK_MAX_ERRORS:
                    self._feedback.reportError(message)
                elif self._errors == FEEDBACK_MAX_ERRORS:
                    self._feedback.reportError(
                        "Further errors suppressed here - see the Log Messages panel "
                        f"('{LOG_TAG}' tab) for the complete record."
                    )
            elif record.levelno >= logging.WARNING:
                self._warnings += 1
                if self._warnings < FEEDBACK_MAX_WARNINGS:
                    self._feedback.pushWarning(message)
                elif self._warnings == FEEDBACK_MAX_WARNINGS:
                    self._feedback.pushWarning(
                        "Further warnings suppressed here - see the Log Messages panel "
                        f"('{LOG_TAG}' tab) for the complete record."
                    )
            else:
                now = time.monotonic()
                if now - self._last_info < FEEDBACK_MIN_INTERVAL_S:
                    return
                self._last_info = now
                self._feedback.pushInfo(message)
            self._last_message = message
        except Exception:
            # The dialog may already be torn down after a cancel; never let
            # a logging call take the algorithm down with it.
            pass


class _FeedbackBridge:
    """Attaches/detaches the log handler and owns the throttled callbacks.

    Used as a context manager so the handler is always removed, including on
    cancellation or an exception - a leaked handler would keep pushing into
    a destroyed dialog on the next run.
    """

    def __init__(self, feedback):
        self._feedback = feedback
        self._logger = logging.getLogger(LOG_TAG)
        self._handler = _FeedbackLogHandler(feedback)
        self._handler.setFormatter(logging.Formatter("%(message)s"))
        self._last_progress = 0.0
        self._last_status: Optional[str] = None

    def __enter__(self) -> "_FeedbackBridge":
        self._logger.addHandler(self._handler)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._logger.removeHandler(self._handler)
        return False

    def on_progress(self, done: int, total: int) -> None:
        """Second throttle stage.

        The exporter already ticks on an interval, but batch runs and future
        callers may not, so the guarantee is enforced here too. `setProgress`
        is a queued cross-thread emission and must never be driven per tile.
        """
        if total <= 0:
            return
        now = time.monotonic()
        if done < total and (now - self._last_progress) < FEEDBACK_MIN_INTERVAL_S:
            return
        self._last_progress = now
        self._feedback.setProgress(min(100.0, 100.0 * done / total))

    def on_status(self, message: str) -> None:
        if message == self._last_status:
            return
        self._last_status = message
        try:
            self._feedback.setProgressText(message)
        except AttributeError:
            self._feedback.pushInfo(message)


# --------------------------------------------------------------------------
# Algorithm
# --------------------------------------------------------------------------

class XyzGpkgExporterAlgorithm(QgsProcessingAlgorithm):
    """Processing algorithm wrapping `XyzGpkgExporter`."""

    EXTENT = "EXTENT"
    MIN_ZOOM = "MIN_ZOOM"
    MAX_ZOOM = "MAX_ZOOM"
    DPI = "DPI"
    CPU_PERCENT = "CPU_PERCENT"
    TILE_FORMAT = "TILE_FORMAT"
    QUALITY = "QUALITY"
    OCCUPANCY_TILE_BUFFER = "OCCUPANCY_TILE_BUFFER"
    OUTPUT_DIR = "OUTPUT_DIR"
    OUTPUT_FILE = "OUTPUT_FILE"

    def __init__(self):
        super().__init__()
        self._exporter: Optional[XyzGpkgExporter] = None
        self._output_path: Optional[str] = None

    # -- identity ---------------------------------------------------------

    def tr(self, string: str) -> str:
        return QCoreApplication.translate("XyzGpkgExporterAlgorithm", string)

    def createInstance(self) -> "XyzGpkgExporterAlgorithm":
        return XyzGpkgExporterAlgorithm()

    def name(self) -> str:
        return "xyzgpkgexport"

    def displayName(self) -> str:
        return self.tr("Export XYZ Tiles to GeoPackage")

    def group(self) -> str:
        return self.tr("Tile Export")

    def groupId(self) -> str:
        return "tileexport"

    def icon(self) -> QIcon:
        return QIcon()

    def shortHelpString(self) -> str:
        return self.tr(
            "Exports the currently loaded QGIS project as raster XYZ tiles into a single "
            "GeoPackage (EPSG:4326). Rule-based symbology, labeling and scale-dependent "
            "visibility are preserved using a per-tile equator-based scale calculation that "
            "matches the live map canvas at every zoom level.\n\n"
            "Only tiles that actually contain feature content are rendered. Occupancy is "
            "derived from each layer's real feature geometry rather than its overall bounding "
            "box, so a sparse dataset spread over a large extent skips empty tiles instead of "
            "spending time rendering them. See the 'Occupancy padding' parameter if tiles near "
            "real content come out unexpectedly empty.\n\n"
            "The export runs on background threads and reports progress on a fixed interval, so "
            "QGIS stays responsive and Cancel takes effect promptly even on multi-million-tile "
            "jobs."
        )

    def flags(self):
        # FlagNoThreading is deliberately NOT set: leaving it unset is what
        # keeps processAlgorithm() on a background thread via the standard
        # QgsProcessingAlgRunnerTask dispatch.
        result = super().flags()
        if _FLAG_SUPPORTS_BATCH is not None:
            result |= _FLAG_SUPPORTS_BATCH
        if _FLAG_REQUIRES_PROJECT is not None:
            result |= _FLAG_REQUIRES_PROJECT
        return result

    # -- parameters -------------------------------------------------------

    def initAlgorithm(self, config=None) -> None:
        add = self.addParameter

        add(QgsProcessingParameterExtent(
            self.EXTENT, self.tr("Export extent (EPSG:4326)"), optional=True
        ))
        add(self._int_param(
            self.MIN_ZOOM, "Minimum zoom level", default=0, minimum=0, maximum=23
        ))
        add(self._int_param(
            self.MAX_ZOOM, "Maximum zoom level", default=10, minimum=0, maximum=23
        ))
        add(self._int_param(
            self.DPI, "Output DPI", default=96, minimum=48, maximum=600
        ))
        add(self._int_param(
            self.CPU_PERCENT, "Max CPU usage (%)", default=75, minimum=1, maximum=100,
            help_text=(
                "Render worker threads are allocated as this percentage of the available CPU "
                "cores. Rendering is CPU bound, so values above 100% of the core count would "
                "only add contention and are not offered."
            ),
        ))
        add(QgsProcessingParameterEnum(
            self.TILE_FORMAT, self.tr("Tile image format"),
            options=list(TILE_FORMATS), defaultValue=0,
        ))
        add(self._int_param(
            self.QUALITY, "Image quality / compression", default=85, minimum=0, maximum=100
        ))
        add(self._int_param(
            self.OCCUPANCY_TILE_BUFFER, "Occupancy padding (tiles)",
            default=1, minimum=0, maximum=10,
            help_text=(
                "Tiles are rendered only where a layer's actual features fall, not across the "
                "layer's whole bounding box. This many extra tiles are included around every "
                "feature to allow for rendered content - symbol size, line width, label "
                "placement - extending beyond the bare geometry. The default of 1 covers typical "
                "symbology; raise it if the project uses unusually large point markers or label "
                "offsets and tiles near real content come out unexpectedly empty."
            ),
        ))
        add(QgsProcessingParameterFolderDestination(
            self.OUTPUT_DIR, self.tr("Output directory")
        ))

        # The folder parameter names a directory; the actual GeoPackage is
        # timestamped inside it, so it needs its own output definition rather
        # than being returned under the folder's key.
        self.addOutput(QgsProcessingOutputFile(
            self.OUTPUT_FILE, self.tr("Output GeoPackage")
        ))

    def _int_param(
        self, name: str, label: str, default: int, minimum: int, maximum: int,
        help_text: Optional[str] = None,
    ) -> QgsProcessingParameterNumber:
        param = QgsProcessingParameterNumber(
            name, self.tr(label), _INT_PARAM,
            defaultValue=default, minValue=minimum, maxValue=maximum,
        )
        if help_text and hasattr(param, "setHelp"):
            param.setHelp(self.tr(help_text))
        return param

    # -- lifecycle: main thread -------------------------------------------

    def prepareAlgorithm(self, parameters, context, feedback) -> bool:
        """Runs on the MAIN thread. Everything that touches live project or
        canvas state belongs here and nowhere else."""
        min_zoom = self.parameterAsInt(parameters, self.MIN_ZOOM, context)
        max_zoom = self.parameterAsInt(parameters, self.MAX_ZOOM, context)
        if max_zoom < min_zoom:
            raise QgsProcessingException(
                self.tr("Maximum zoom must be greater than or equal to minimum zoom.")
            )

        dpi = self.parameterAsInt(parameters, self.DPI, context)
        cpu_percent = self.parameterAsInt(parameters, self.CPU_PERCENT, context)
        quality = self.parameterAsInt(parameters, self.QUALITY, context)
        tile_buffer = self.parameterAsInt(parameters, self.OCCUPANCY_TILE_BUFFER, context)
        tile_format = TILE_FORMATS[self.parameterAsEnum(parameters, self.TILE_FORMAT, context)]

        output_dir = self.parameterAsString(parameters, self.OUTPUT_DIR, context)
        if not output_dir:
            raise QgsProcessingException(self.tr("An output directory is required."))

        target_crs = QgsCoordinateReferenceSystem("EPSG:4326")
        extent = self.parameterAsExtent(parameters, self.EXTENT, context, target_crs)
        if extent is None or extent.isNull() or extent.isEmpty():
            extent = None

        project = context.project()

        try:
            snapshot = ProjectRenderSnapshot.capture(
                extent=extent, project=project, dest_crs=target_crs
            )
        except Exception as exc:
            raise QgsProcessingException(
                self.tr(f"Could not read the project's render configuration: {exc}")
            ) from exc

        if not snapshot.layers:
            raise QgsProcessingException(
                self.tr("No visible layers were found to export. Check the layer tree or the "
                        "active map theme.")
            )

        try:
            self._exporter = XyzGpkgExporter(
                min_zoom=min_zoom,
                max_zoom=max_zoom,
                dpi=dpi,
                max_cpu_percent=cpu_percent,
                tile_format=tile_format,
                quality=quality,
                output_dir=output_dir,
                occupancy_tile_buffer=tile_buffer,
                snapshot=snapshot,
                should_cancel=feedback.isCanceled,
            )
        except Exception as exc:
            raise QgsProcessingException(
                self.tr(f"Could not configure the export: {exc}")
            ) from exc

        feedback.pushInfo(self.tr(
            f"Export configured: zoom {min_zoom}-{max_zoom}, {tile_format} @ {dpi} dpi, "
            f"{len(snapshot.layers)} layer(s), "
            f"{compute_worker_count(cpu_percent)} render thread(s)."
        ))
        return True

    # -- lifecycle: worker thread -----------------------------------------

    def processAlgorithm(self, parameters, context, feedback) -> dict:
        """Runs on a BACKGROUND thread. No GUI or project mutation here."""
        exporter = self._exporter
        if exporter is None:  # pragma: no cover - prepareAlgorithm guarantees this
            raise QgsProcessingException(self.tr("The algorithm was not prepared correctly."))

        with _FeedbackBridge(feedback) as bridge:
            exporter.progress_callback = bridge.on_progress
            exporter.status_callback = bridge.on_status
            exporter.should_cancel = feedback.isCanceled
            try:
                self._output_path = exporter.export()
            except Exception as exc:
                raise QgsProcessingException(str(exc)) from exc

        if exporter.cancelled or feedback.isCanceled():
            feedback.pushInfo(self.tr("Export cancelled by user."))
        else:
            feedback.pushInfo(self.tr(
                f"Export finished: {exporter.tiles_written:,} tile(s) written to "
                f"{self._output_path}"
            ))
        if exporter.tiles_failed:
            feedback.pushWarning(self.tr(
                f"{exporter.tiles_failed:,} tile(s) failed to render. See the Log Messages "
                f"panel ('{LOG_TAG}' tab) for details."
            ))

        return {
            self.OUTPUT_DIR: exporter.output_dir,
            self.OUTPUT_FILE: self._output_path or "",
        }

    # -- lifecycle: main thread -------------------------------------------

    def postProcessAlgorithm(self, context, feedback) -> dict:
        """Runs on the MAIN thread. Registry mutation belongs here.

        The previous version called `QgsProject.addMapLayer()` from inside
        `processAlgorithm()`, i.e. from the worker thread.
        """
        exporter = self._exporter
        path = self._output_path

        if path and os.path.exists(path) and not feedback.isCanceled():
            name = os.path.splitext(os.path.basename(path))[0]
            layer = QgsRasterLayer(path, name, "gdal")
            if layer.isValid():
                project = context.project()
                if project is not None:
                    project.addMapLayer(layer)
            else:
                feedback.pushWarning(self.tr(
                    f"The GeoPackage was written to {path} but could not be loaded as a raster "
                    "layer. Try adding it manually."
                ))

        return {
            self.OUTPUT_DIR: exporter.output_dir if exporter else "",
            self.OUTPUT_FILE: path or "",
        }


# --------------------------------------------------------------------------
# Provider
# --------------------------------------------------------------------------

class XyzGpkgExporterProvider(QgsProcessingProvider):
    """Processing provider hosting the XYZ GeoPackage export algorithm."""

    def id(self) -> str:
        return "xyzgpkgexporter"

    def name(self) -> str:
        return "XYZ GeoPackage Exporter"

    def longName(self) -> str:
        return "XYZ GeoPackage Exporter"

    def icon(self) -> QIcon:
        return QIcon()

    def loadAlgorithms(self) -> None:
        self.addAlgorithm(XyzGpkgExporterAlgorithm())


def register_provider() -> XyzGpkgExporterProvider:
    """Call from a plugin's `initProcessing()`/`initGui()`."""
    provider = XyzGpkgExporterProvider()
    QgsApplication.processingRegistry().addProvider(provider)
    return provider


def unregister_provider(provider: XyzGpkgExporterProvider) -> None:
    """Call from a plugin's `unload()`."""
    QgsApplication.processingRegistry().removeProvider(provider)
