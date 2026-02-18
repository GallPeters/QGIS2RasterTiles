"""QGIS Processing Algorithms for QGIS2VectorTiles plugin."""

from os.path import join
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterNumber,
    QgsProcessingParameterExtent,
    QgsProcessingParameterFolderDestination,
    QgsCoordinateReferenceSystem,
    QgsProcessingParameterEnum,
)
from qgis.utils import iface
from .QGIS2RasterTiles import TileExportPipeline, _PLUGIN_DIR

_ICON = QIcon(join(_PLUGIN_DIR, "icon.png"))


class QGIS2RasterTilesAlgorithm(QgsProcessingAlgorithm):
    """
    QGIS Processing Algorithm for generating raster tiles from project layers.
    This wrapper provides a user interface for the raster tiles generation process
    through the QGIS Processing Toolbox.
    """

    # Parameter names (constants for consistency)
    MIN_ZOOM = "MIN_ZOOM"
    MAX_ZOOM = "MAX_ZOOM"
    EXTENT = "EXTENT"
    CPU_PERCENT = "CPU_PERCENT"
    OUTPUT_DIR = "OUTPUT_DIR"
    OUTPUT_DPI = "OUTPUT_DPI"
    PACK_TO_GPKG = "PACK_TO_GPKG"

    def __init__(self):
        """Initialize the algorithm"""
        super().__init__()

    def tr(self, string):
        """
        Returns a translatable string with the self.tr() function.
        """
        return QCoreApplication.translate("Processing", string)

    def createInstance(self):
        """
        Returns a new instance of the algorithm. Required by QGIS Processing framework.
        """
        return QGIS2RasterTilesAlgorithm()

    def name(self):
        """
        Returns the algorithm name, used for identifying the algorithm.
        This string should be fixed for the algorithm, and must not be localized.
        """
        return "QGIS2RasterTiles_action"

    def displayName(self):
        """
        Returns the translated algorithm name, which should be used for any
        user-visible display of the algorithm name.
        """
        return self.tr("QGIS2RasterTiles")

    def group(self):
        """
        Returns the name of the group this algorithm belongs to.
        """
        return None  # No inner group as requested

    def groupId(self):
        """
        Returns the unique ID of the group this algorithm belongs to.
        """
        return None  # No inner group as requested

    def icon(self):
        """
        Returns the algorithm icon.
        """
        return _ICON

    def shortHelpString(self):
        """
        Returns a localised short helper string for the algorithm.
        """
        return self.tr(
            "Generates an XYZ raster tile package from all visible project layers while preserving "
            "their original styling. The generated tiles are automatically loaded "
            "back into the project with identical appearance to the source layers."
            "\nMore information can be found at: https://github.com/GallPeters/QGIS2RasterTiles"
        )

    def initAlgorithm(self, config=None):  # pylint: disable=W0613
        """
        Define the inputs and outputs of the algorithm.
        """

        # Minimum zoom level parameter
        self.addParameter(
            QgsProcessingParameterNumber(
                self.MIN_ZOOM,
                self.tr("Minimum Zoom"),
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=0,
                minValue=0,
                maxValue=22,
            )
        )

        # Maximum zoom level parameter
        self.addParameter(
            QgsProcessingParameterNumber(
                self.MAX_ZOOM,
                self.tr("Maximum Zoom"),
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=10,
                minValue=0,
                maxValue=22,
            )
        )

        # Extent parameter - defaults to current map canvas extent
        extent_param = QgsProcessingParameterExtent(
            self.EXTENT, self.tr("Tiles Extent"), optional=False
        )
        # Set default to current map canvas extent if available
        if iface and iface.mapCanvas():
            extent_param.setDefaultValue(iface.mapCanvas().extent())
        self.addParameter(extent_param)

        # CPU Percent parameter
        self.addParameter(
            QgsProcessingParameterNumber(
                self.CPU_PERCENT,
                self.tr("CPU Percent"),
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=100,
                minValue=1,
                maxValue=100,
            )
        )

        # DPI parameter — controls label/symbol pixel size in rendered tiles.
        # Use 96 for standard displays, 192 for Hi-DPI/Retina monitors so that
        # labels match the appearance of the original QGIS project.
        self.addParameter(
            QgsProcessingParameterNumber(
                self.OUTPUT_DPI,
                self.tr("Output DPI (96 = standard, 192 = Hi-DPI/Retina)"),
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=96,
                minValue=72,
                maxValue=600,
            )
        )

        # Pack into GeoPackage parameter
        self.addParameter(
            QgsProcessingParameterEnum(
                self.PACK_TO_GPKG,
                self.tr("Output Format"),
                options=["Directory (XYZ PNGs)", "GeoPackage (.gpkg)"],
                defaultValue=0,
                optional=False,
            )
        )

        # Output directory parameter
        self.addParameter(
            QgsProcessingParameterFolderDestination(
                self.OUTPUT_DIR, self.tr("Output Directory"), optional=False
            )
        )

    def checkParameterValues(self, parameters, context):
        """
        Validate parameter values before processing.
        Returns tuple (is_valid, error_message)
        """
        min_zoom = self.parameterAsInt(parameters, self.MIN_ZOOM, context)
        max_zoom = self.parameterAsInt(parameters, self.MAX_ZOOM, context)

        # Check that min_zoom <= max_zoom
        if min_zoom > max_zoom:
            return False, self.tr(
                "Minimum zoom level must be less than or equal to maximum zoom level"
            )

        return super().checkParameterValues(parameters, context)

    def processAlgorithm(self, parameters, context, feedback):
        """
        Main processing method. Calls TileExportPipeline from QGIS2RasterTiles.

        Args:
            parameters: Dictionary containing parameter values
            context: QgsProcessingContext object
            feedback: QgsProcessingFeedback object for progress reporting

        Returns:
            Dictionary with results (can be empty for this use case)
        """

        # Extract parameter values
        min_zoom = self.parameterAsInt(parameters, self.MIN_ZOOM, context)
        max_zoom = self.parameterAsInt(parameters, self.MAX_ZOOM, context)
        extent = self.parameterAsExtent(
            parameters, self.EXTENT, context, QgsCoordinateReferenceSystem("EPSG:4326")
        )
        cpu_percent = self.parameterAsInt(parameters, self.CPU_PERCENT, context)
        output_dpi = self.parameterAsInt(parameters, self.OUTPUT_DPI, context)
        pack_to_gpkg = bool(self.parameterAsInt(parameters, self.PACK_TO_GPKG, context))
        output_dir = self.parameterAsString(parameters, self.OUTPUT_DIR, context)

        try:
            pipeline = TileExportPipeline(
                crs_epsg=4326,
                top_left_corner=(-180, 90),
                tile_width=180,
                tile_height=180,
                matrix_width=2,
                matrix_height=1,
                extent=extent,
                min_zoom=min_zoom,
                max_zoom=max_zoom,
                output_dir=output_dir,
                tile_size=256,
                cpu_percent=float(cpu_percent),
                pack_to_gpkg=pack_to_gpkg,
                render_buffer_px=0,
                output_dpi=float(output_dpi),
            )
            pipeline.run()
            feedback.pushInfo("Raster tiles generation completed successfully")

        except (NameError, ValueError, AttributeError, TypeError) as e:
            feedback.reportError(f"Error during raster tiles generation: {str(e)}")
            return {}

        return {}
