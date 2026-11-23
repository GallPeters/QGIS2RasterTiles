"""Plugin entry: registers the Processing provider."""

from __future__ import annotations

from qgis.core import QgsApplication

from .provider import QGIS2RasterTilesProvider


class QGIS2RasterTilesPlugin:
    def __init__(self, iface) -> None:
        self.iface = iface
        self.provider = None

    def initProcessing(self) -> None:  # noqa: N802
        self.provider = QGIS2RasterTilesProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def initGui(self) -> None:  # noqa: N802
        self.initProcessing()

    def unload(self) -> None:
        if self.provider is not None:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None
