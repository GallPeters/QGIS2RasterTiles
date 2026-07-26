"""
xyz_gpkg_exporter_provider.py

QGIS Processing GUI wrapper exposing XyzGpkgExporter (from
xyz_gpkg_exporter.py) as a standard Processing algorithm/provider. Running
via the Processing toolbox (instead of instantiating/calling the exporter
directly from the Python Console) keeps the export off the main/GUI
thread, since QgsProcessingAlgRunnerTask dispatches processAlgorithm() to
a background thread by default - this is what avoids the UI-freezing
behavior observed with direct Python Console execution.
"""

from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtGui import QIcon
from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterEnum,
    QgsProcessingParameterExtent,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterNumber,
    QgsProcessingProvider,
)

try:
    from .xyz_gpkg_exporter import XyzGpkgExporter
except ImportError:
    from xyz_gpkg_exporter import XyzGpkgExporter

TILE_FORMATS = ["PNG", "JPEG", "WEBP", "JPEG2000"]


class XyzGpkgExporterAlgorithm(QgsProcessingAlgorithm):
    """Processing algorithm wrapping XyzGpkgExporter."""

    EXTENT = "EXTENT"
    MIN_ZOOM = "MIN_ZOOM"
    MAX_ZOOM = "MAX_ZOOM"
    DPI = "DPI"
    CPU_PERCENT = "CPU_PERCENT"
    TILE_FORMAT = "TILE_FORMAT"
    QUALITY = "QUALITY"
    OUTPUT_DIR = "OUTPUT_DIR"

    def tr(self, string: str) -> str:
        return QCoreApplication.translate("XyzGpkgExporterAlgorithm", string)

    def createInstance(self):
        return XyzGpkgExporterAlgorithm()

    def name(self) -> str:
        return "xyzgpkgexport"

    def displayName(self) -> str:
        return self.tr("Export XYZ Tiles to GeoPackage")

    def group(self) -> str:
        return self.tr("Tile Export")

    def groupId(self) -> str:
        return "tileexport"

    def shortHelpString(self) -> str:
        return self.tr(
            "Exports the currently loaded QGIS project as raster XYZ tiles into a single "
            "GeoPackage. Preserves rule-based symbology, labeling, and scale-dependent "
            "visibility using a per-tile equator-based scale calculation that matches the "
            "live map canvas at every zoom level."
        )

    def icon(self):
        return QIcon()

    def flags(self):
        # Deliberately does NOT set Flag.FlagNoThreading, which would force
        # main-thread execution; leaving it unset keeps this backgroundable
        # via the standard Processing task dispatch mechanism.
        return super().flags() | QgsProcessingAlgorithm.Flag.FlagSupportsBatch

    def initAlgorithm(self, config=None) -> None:
        self.addParameter(
            QgsProcessingParameterExtent(self.EXTENT, self.tr("Export extent (EPSG:4326)"), optional=True)
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.MIN_ZOOM, self.tr("Minimum zoom level"),
                QgsProcessingParameterNumber.Type.Integer, defaultValue=0, minValue=0, maxValue=23,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.MAX_ZOOM, self.tr("Maximum zoom level"),
                QgsProcessingParameterNumber.Type.Integer, defaultValue=10, minValue=0, maxValue=23,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.DPI, self.tr("Output DPI"),
                QgsProcessingParameterNumber.Type.Integer, defaultValue=96, minValue=48, maxValue=600,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.CPU_PERCENT, self.tr("Max CPU usage (%)"),
                QgsProcessingParameterNumber.Type.Integer, defaultValue=75, minValue=1, maxValue=100,
            )
        )
        self.addParameter(
            QgsProcessingParameterEnum(
                self.TILE_FORMAT, self.tr("Tile image format"), options=TILE_FORMATS, defaultValue=0
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.QUALITY, self.tr("Image quality / compression"),
                QgsProcessingParameterNumber.Type.Integer, defaultValue=85, minValue=0, maxValue=100,
            )
        )
        self.addParameter(
            QgsProcessingParameterFolderDestination(self.OUTPUT_DIR, self.tr("Output directory"))
        )

    def processAlgorithm(self, parameters, context, feedback):
        target_crs = QgsCoordinateReferenceSystem("EPSG:4326")
        extent = self.parameterAsExtent(parameters, self.EXTENT, context, target_crs)
        if extent is None or extent.isNull() or extent.isEmpty():
            extent = None

        min_zoom = self.parameterAsInt(parameters, self.MIN_ZOOM, context)
        max_zoom = self.parameterAsInt(parameters, self.MAX_ZOOM, context)
        dpi = self.parameterAsInt(parameters, self.DPI, context)
        cpu_percent = self.parameterAsInt(parameters, self.CPU_PERCENT, context)
        format_index = self.parameterAsEnum(parameters, self.TILE_FORMAT, context)
        tile_format = TILE_FORMATS[format_index]
        quality = self.parameterAsInt(parameters, self.QUALITY, context)
        output_dir = self.parameterAsString(parameters, self.OUTPUT_DIR, context)

        if max_zoom < min_zoom:
            raise QgsProcessingException(self.tr("Maximum zoom must be greater than or equal to minimum zoom."))

        feedback.pushInfo(
            self.tr(f"Starting export: zoom {min_zoom}-{max_zoom}, format {tile_format}, dpi {dpi}")
        )

        def progress_callback(done: int, total: int) -> None:
            feedback.setProgress(min(100.0, (float(done) / float(total)) * 100.0))
            # if done % 50 == 0 or done == total:
            #     feedback.pushInfo(self.tr(f"Processed {done}/{total} tiles"))

        try:
            exporter = XyzGpkgExporter(
                extent=extent,
                min_zoom=min_zoom,
                max_zoom=max_zoom,
                dpi=dpi,
                max_cpu_percent=cpu_percent,
                tile_format=tile_format,
                quality=quality,
                output_dir=output_dir,
                progress_callback=progress_callback,
                should_cancel=feedback.isCanceled,
            )
            output_path = exporter.export()
            exporter.add_result_layer()
        except Exception as exc:
            raise QgsProcessingException(str(exc))

        if feedback.isCanceled():
            feedback.pushInfo(self.tr("Export cancelled by user."))
        else:
            feedback.pushInfo(self.tr(f"Export finished: {output_path}"))

        return {self.OUTPUT_DIR: output_path}


class XyzGpkgExporterProvider(QgsProcessingProvider):
    """Processing provider hosting the XYZ GeoPackage export algorithm."""

    def id(self) -> str:
        return "xyzgpkgexporter"

    def name(self) -> str:
        return "XYZ GeoPackage Exporter"

    def icon(self):
        return QIcon()

    def loadAlgorithms(self) -> None:
        self.addAlgorithm(XyzGpkgExporterAlgorithm())


def register_provider() -> XyzGpkgExporterProvider:
    """Call from a plugin's initProcessing()/initGui() to register the
    provider with the global Processing registry."""
    provider = XyzGpkgExporterProvider()
    QgsApplication.processingRegistry().addProvider(provider)
    return provider


def unregister_provider(provider: XyzGpkgExporterProvider) -> None:
    """Call from a plugin's unload() to cleanly remove the provider."""
    QgsApplication.processingRegistry().removeProvider(provider)