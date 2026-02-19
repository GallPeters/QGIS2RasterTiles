"""QGIS2RasterTiles provider implementation."""

from qgis.core import QgsProcessingProvider
from .algorithms import QGIS2RasterTilesAlgorithm, _ICON


# Create a proper temporary provider class
class QGIS2RasterTilesPorvider(QgsProcessingProvider):
    """Provider for QGIS2RasterTiles plugin, integrating the QGIS2RasterTilesAlgorithm
    into the QGIS Processing framework."""

    def __init__(self):
        super().__init__()

    def id(self):
        """Returns the unique ID of the provider."""
        return "QGIS2RasterTiles"

    def name(self):
        """Returns the display name of the provider."""
        return "QGIS2RasterTiles"

    def icon(self):
        """Returns the provider icon."""
        return _ICON

    def loadAlgorithms(self):
        """Loads the algorithms provided by this provider."""
        self.addAlgorithm(QGIS2RasterTilesAlgorithm())
