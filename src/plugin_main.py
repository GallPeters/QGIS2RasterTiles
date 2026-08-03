from qgis.core import QgsApplication
from .xyz_gpkg_processing_provider import XyzGpkgExporterProvider

class XyzGpkgPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.provider = XyzGpkgExporterProvider()

    def initGui(self):
        QgsApplication.processingRegistry().addProvider(self.provider)

    def unload(self):
        QgsApplication.processingRegistry().removeProvider(self.provider)