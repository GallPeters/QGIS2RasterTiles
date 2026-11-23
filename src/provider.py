"""Processing provider for the plugin."""

from __future__ import annotations

import os

from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon

from .algorithm import ParallelXyzTilesAlgorithm

ICON = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "icon.png")


class QGIS2RasterTilesProvider(QgsProcessingProvider):
    def id(self) -> str:
        return "qgis2rastertiles"

    def name(self) -> str:
        return "QGIS2RasterTiles"

    def longName(self) -> str:  # noqa: N802
        return self.name()

    def icon(self) -> QIcon:
        return QIcon(ICON)

    def loadAlgorithms(self) -> None:  # noqa: N802
        self.addAlgorithm(ParallelXyzTilesAlgorithm())
