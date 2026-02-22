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
    QgsCoordinateTransform,
    QgsProcessingParameterEnum,
    QgsRectangle,
    QgsApplication
)
from qgis.utils import iface
from .qgis2rastertiles import QGIS2RasterTiles


_PLUGIN_DIR = join(QgsApplication.qgisSettingsDirPath(), "python", "plugins", "QGIS2RasterTiles")
_ICON = QIcon(join(_PLUGIN_DIR, "icon.png"))

# Tile Matrix Set definitions keyed by EPSG code
_TILE_MATRIX_SETS = {
    4326: {
        "top_left_x":    -180.0,
        "top_left_y":     90.0,
        "tile_dim":       180.0,
        "matrix_width":   2,
        "matrix_height":  1,
        # Valid coordinate bounds for clamping
        "bounds": QgsRectangle(-180.0, -90.0, 180.0, 90.0),
    },
    3857: {
        "top_left_x":   -20037508.343,
        "top_left_y":    20037508.343,
        "tile_dim":      40075016.686,
        "matrix_width":  1,
        "matrix_height": 1,
        # Valid coordinate bounds for clamping
        "bounds": QgsRectangle(-20037508.343, -20037508.343, 20037508.343, 20037508.343),
    },
}

# Ordered list matching the enum options shown to the user
_TMS_OPTIONS = [
    (4326, "WGS84"),
    (3857, "WebMercator"),
]


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
    TILE_MATRIX_SET = "TILE_MATRIX_SET"
    CPU_PERCENT = "CPU_PERCENT"
    OUTPUT_DIR = "OUTPUT_DIR"
    OUTPUT_DPI = "OUTPUT_DPI"
    PACK_TO_GPKG = "PACK_TO_GPKG"

    def __init__(self):
        """Initialize the algorithm"""
        super().__init__()

    def tr(self, string):
        """Returns a translatable string with the self.tr() function."""
        return QCoreApplication.translate("Processing", string)

    def createInstance(self):
        """Returns a new instance of the algorithm. Required by QGIS Processing framework."""
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
        """Returns the name of the group this algorithm belongs to."""
        return None  # No inner group as requested

    def groupId(self):
        """Returns the unique ID of the group this algorithm belongs to."""
        return None  # No inner group as requested

    def icon(self):
        """Returns the algorithm icon."""
        return _ICON

    def shortHelpString(self):
        """Returns a localised short helper string for the algorithm."""
        return self.tr(
            "Generates an XYZ raster tile package from all visible project layers while preserving "
            "their original styling. The generated tiles are automatically loaded "
            "back into the project with identical appearance to the source layers."
            "\nMore information can be found at: https://github.com/GallPeters/QGIS2RasterTiles"
        )

    # ------------------------------------------------------------------
    # Helper utilities
    # ------------------------------------------------------------------

    def _resolve_tms_epsg(self, tms_index):
        """Return the EPSG integer for the chosen Tile Matrix Set index."""
        return _TMS_OPTIONS[tms_index][0]

    def _convert_extent_to_crs(self, extent, source_crs, target_crs):
        """
        Reproject *extent* from *source_crs* to *target_crs*.

        Parameters
        ----------
        extent : QgsRectangle
            Extent in source CRS coordinates.
        source_crs : QgsCoordinateReferenceSystem
            CRS of the incoming extent.
        target_crs : QgsCoordinateReferenceSystem
            Desired output CRS.

        Returns
        -------
        QgsRectangle
            Extent reprojected to *target_crs*.  Returns the original
            rectangle unchanged when both CRSes are equal.
        """
        if source_crs == target_crs:
            return extent

        transform = QgsCoordinateTransform(source_crs, target_crs, QgsProject.instance())
        return transform.transformBoundingBox(extent)

    def _clamp_extent_to_crs_bounds(self, extent, crs_epsg, feedback=None):
        """
        Clamp *extent* so that it does not exceed the valid coordinate range
        of the given CRS.

        If any edge of *extent* lies outside the CRS bounds it is trimmed to
        the boundary.  A warning is pushed to *feedback* when clamping occurs.

        Parameters
        ----------
        extent : QgsRectangle
            Extent to validate (already in the target CRS).
        crs_epsg : int
            EPSG code (4326 or 3857) used to look up the valid bounds.
        feedback : QgsProcessingFeedback, optional
            Used to report clamping warnings to the user.

        Returns
        -------
        QgsRectangle
            Clamped extent guaranteed to fit within the CRS bounds.
        """
        bounds = _TILE_MATRIX_SETS[crs_epsg]["bounds"]

        xmin = max(extent.xMinimum(), bounds.xMinimum())
        ymin = max(extent.yMinimum(), bounds.yMinimum())
        xmax = min(extent.xMaximum(), bounds.xMaximum())
        ymax = min(extent.yMaximum(), bounds.yMaximum())

        clamped = QgsRectangle(xmin, ymin, xmax, ymax)

        if clamped != extent and feedback is not None:
            feedback.pushWarning(
                f"The requested extent exceeded the valid bounds of EPSG:{crs_epsg} "
                f"and has been trimmed to fit: "
                f"[{xmin}, {ymin}, {xmax}, {ymax}]"
            )

        return clamped

    # ------------------------------------------------------------------
    # Algorithm definition
    # ------------------------------------------------------------------

    def initAlgorithm(self, config=None):  # pylint: disable=W0613
        """Define the inputs and outputs of the algorithm."""

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

        # Tile Matrix Set – replaces the former five individual matrix parameters
        # (Top Left X, Top Left Y, Tile Dimension, Matrix Width, Matrix Height).
        # Selecting WGS84 uses the EPSG:4326 well-known tile matrix set;
        # selecting WebMercator uses the EPSG:3857 (Google/OSM) tile matrix set.
        project_epsg = int(QgsProject.instance().crs().authid().split(":")[-1])
        tms_default = next(
            (i for i, (epsg, _) in enumerate(_TMS_OPTIONS) if epsg == project_epsg),
            0,  # fall back to WGS84 for any other CRS
        )
        self.addParameter(
            QgsProcessingParameterEnum(
                self.TILE_MATRIX_SET,
                self.tr("Tile Matrix Set (output tile CRS)"),
                options=[label for _, label in _TMS_OPTIONS],
                defaultValue=tms_default,
                optional=False,
            )
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

        # --- Zoom levels ---------------------------------------------------
        min_zoom = self.parameterAsInt(parameters, self.MIN_ZOOM, context)
        max_zoom = self.parameterAsInt(parameters, self.MAX_ZOOM, context)

        # --- Tile Matrix Set -----------------------------------------------
        tms_index = self.parameterAsInt(parameters, self.TILE_MATRIX_SET, context)
        crs_epsg = self._resolve_tms_epsg(tms_index)
        tms = _TILE_MATRIX_SETS[crs_epsg]

        top_left_x    = tms["top_left_x"]
        top_left_y    = tms["top_left_y"]
        tile_dim      = tms["tile_dim"]
        matrix_width  = tms["matrix_width"]
        matrix_height = tms["matrix_height"]

        target_crs = QgsCoordinateReferenceSystem(f"EPSG:{crs_epsg}")

        # --- Extent --------------------------------------------------------
        # parameterAsExtent returns the extent in the CRS that is explicitly
        # passed as the last argument.  We request it in the project CRS first
        # so we can properly transform it ourselves.
        project_crs = QgsProject.instance().crs()
        raw_extent = self.parameterAsExtent(parameters, self.EXTENT, context, project_crs)

        # 1. Reproject extent to the target tile CRS if necessary
        extent = self._convert_extent_to_crs(raw_extent, project_crs, target_crs)

        # 2. Clamp extent to the valid coordinate range of the target CRS
        extent = self._clamp_extent_to_crs_bounds(extent, crs_epsg, feedback)

        # --- Remaining parameters ------------------------------------------
        cpu_percent = self.parameterAsInt(parameters, self.CPU_PERCENT, context)
        output_dpi  = self.parameterAsInt(parameters, self.OUTPUT_DPI, context)
        pack_to_gpkg = bool(self.parameterAsInt(parameters, self.PACK_TO_GPKG, context))
        output_dir  = self.parameterAsString(parameters, self.OUTPUT_DIR, context)

        feedback.pushInfo(
            f"Using Tile Matrix Set: EPSG:{crs_epsg} | "
            f"Extent: [{extent.xMinimum():.6f}, {extent.yMinimum():.6f}, "
            f"{extent.xMaximum():.6f}, {extent.yMaximum():.6f}]"
        )

        try:
            tiles_generator = QGIS2RasterTiles(
                crs_epsg=crs_epsg,
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
                feedback=feedback,
            )
            tiles_generator.convert_project_to_raster_tiles()
            feedback.pushInfo("Raster tiles generation completed successfully")

        except (NameError, ValueError, AttributeError, TypeError) as e:
            feedback.reportError(f"Error during raster tiles generation: {str(e)}")
            return {}

        return {}