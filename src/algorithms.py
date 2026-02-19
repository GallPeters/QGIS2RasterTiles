"""QGIS Processing Algorithms for QGIS2RasterTiles plugin."""

from os.path import join
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterNumber,
    QgsProject,
    QgsProcessingParameterExtent,
    QgsProcessingParameterFolderDestination,
    QgsCoordinateReferenceSystem,
    QgsProcessingParameterEnum,
    QgsApplication
)
from qgis.utils import iface
from .qgis2rastertiles import QGIS2RasterTiles


_PLUGIN_DIR = join(QgsApplication.qgisSettingsDirPath(), "python", "plugins", "QGIS2RasterTiles")
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
    TOP_LEFT_X = "TOP_LEFT_X"
    TOP_LEFT_Y = "TOP_LEFT_Y"
    TILE_DIM = "TILE_DIM"
    MATRIX_WIDTH = "MATRIX_WITH"
    MATRIX_HEIGHT = "MATRIX_HEIGHT"
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

        # Extent
        extent_param = QgsProcessingParameterExtent(
            self.EXTENT, self.tr("Tiles Extent"), optional=False
        )
        # Set default to current map canvas extent if available
        if iface and iface.mapCanvas():
            extent_param.setDefaultValue(iface.mapCanvas().extent())
        self.addParameter(extent_param)

        project_crs = int(QgsProject.instance().crs().authid().split(':')[-1])
        # Top left X
        tl_x_param = QgsProcessingParameterNumber(
                self.TOP_LEFT_X, self.tr("Top Left X"), optional=False
            )
        tl_x_param.setDefaultValue({4326:-180, 3857:-20037508.343}.get(project_crs, 0))
        self.addParameter(tl_x_param

        )

        # Top left Y
        tl_y_params = QgsProcessingParameterNumber(
                self.TOP_LEFT_Y, self.tr("Top Left Y"), optional=False
            )
        tl_y_params.setDefaultValue({4326:90, 3857:20037508.343}.get(project_crs, 0))
        self.addParameter(
            tl_y_params
        )
        
        # Tile dimention
        td_param = QgsProcessingParameterNumber(
                self.TILE_DIM, self.tr("Top Tile Dimention"), optional=False
            )
        td_param.setDefaultValue({4326:180.0, 3857:40075016.686}.get(project_crs, 0))
        self.addParameter(td_param

        )

        # Matrix width
        mw_param = QgsProcessingParameterNumber(
                self.MATRIX_WIDTH, self.tr("Matrix Width"), optional=False
            )
        mw_param.setDefaultValue({4326:2, 3857:1}.get(project_crs, 1))
        self.addParameter(mw_param

        )

        # Matrix height
        mh_param = QgsProcessingParameterNumber(
                self.MATRIX_HEIGHT, self.tr("Matrix Height"), optional=False
            )
        mh_param.setDefaultValue({4326:1, 3857:1}.get(project_crs, 1))
        self.addParameter(mh_param

        )

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
                self.tr("Output DPI"),
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=96,
                minValue=0,
                maxValue=600,
            )
        )

        # Pack into GeoPackage parameter
        self.addParameter(
            QgsProcessingParameterEnum(
                self.PACK_TO_GPKG,
                self.tr("Output Format"),
                options=["XYZ Directory", "GeoPackage"],
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
        Main processing method. Calls QGIS2RasterTiles from QGIS2RasterTiles.

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
        top_left_x = self.parameterAsDouble(parameters, self.TOP_LEFT_X, context)
        top_left_y = self.parameterAsDouble(parameters, self.TOP_LEFT_Y, context)
        tile_dim = self.parameterAsDouble(parameters, self.TILE_DIM, context)
        matrix_width = self.parameterAsDouble(parameters, self.MATRIX_WIDTH, context)
        matrix_height = self.parameterAsDouble(parameters, self.MATRIX_HEIGHT, context)
        cpu_percent = self.parameterAsInt(parameters, self.CPU_PERCENT, context)
        cpu_percent = self.parameterAsInt(parameters, self.CPU_PERCENT, context)
        output_dpi = self.parameterAsInt(parameters, self.OUTPUT_DPI, context)
        pack_to_gpkg = bool(self.parameterAsInt(parameters, self.PACK_TO_GPKG, context))
        output_dir = self.parameterAsString(parameters, self.OUTPUT_DIR, context)

        try:
            tiles_generator = QGIS2RasterTiles(
                crs_epsg=4326,
                top_left_corner=(top_left_x, top_left_y),
                tile_dim=tile_dim,
                matrix_width=matrix_width,
                matrix_height=matrix_height,
                extent=extent,
                min_zoom=min_zoom,
                max_zoom=max_zoom,
                output_dir=output_dir,
                tile_size=256,
                cpu_percent=float(cpu_percent),
                pack_to_gpkg=pack_to_gpkg,
                render_buffer_px=0,
                output_dpi=float(output_dpi),
                feedback=feedback
            )
            tiles_generator.convert_project_to_raster_tiles()
            feedback.pushInfo("Raster tiles generation completed successfully")

        except (NameError, ValueError, AttributeError, TypeError) as e:
            feedback.reportError(f"Error during raster tiles generation: {str(e)}")
            return {}

        return {}