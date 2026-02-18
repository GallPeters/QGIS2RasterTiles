"""QGIS2RasterTiles plugin for QGIS"""

from qgis.core import QgsApplication
from .src.provider import QGIS2RasterTilesPorvider


class QGIS2RasterTiles:
    """QGIS2RasterTiles main class"""

    def __init__(self, iface):
        """Constructor"""
        self.provider = None
        self.iface = iface

    def initProcessing(self):
        """Initialize processing provider"""
        self.provider = QGIS2RasterTilesPorvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def initGui(self):
        """Initialize GUI"""
        self.initProcessing()

    def unload(self):
        """Unload plugin"""
        QgsApplication.processingRegistry().removeProvider(self.provider)


def classFactory(iface):
    """invoke plugin"""
    return QGIS2RasterTiles(iface)
