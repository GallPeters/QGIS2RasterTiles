"""Fill a :class:`partition.Occupancy` from the packaged layers (QGIS side).

Small features contribute their envelope; large ones get an exact GEOS test
(prepared geometry) against each candidate metatile rectangle.
"""

from __future__ import annotations

from typing import Callable, Tuple

from qgis.core import QgsFeatureRequest, QgsGeometry, QgsRectangle, QgsVectorLayer
from qgis.PyQt import sip

from .partition import Occupancy
from .tile_grid import Rect


class _GeometryTester:
    """Exact rectangle tests against one geometry (prepared lazily).

    Every QgsGeometry whose ``constGet()`` is handed to GEOS is kept in a
    variable: ``QgsGeometry.fromRect(...).constGet()`` on a temporary would let
    GEOS read a deleted geometry.
    """

    def __init__(self, geometry: QgsGeometry) -> None:
        self.geometry = geometry
        self.engine = None

    def _engine(self):
        if self.engine is None:
            self.engine = QgsGeometry.createGeometryEngine(self.geometry.constGet())
            self.engine.prepareGeometry()
        return self.engine

    def intersects(self, r: Rect) -> bool:
        g = QgsGeometry.fromRect(QgsRectangle(r.xmin, r.ymin, r.xmax, r.ymax))
        return self._engine().intersects(g.constGet())

    def contains(self, r: Rect) -> bool:
        g = QgsGeometry.fromRect(QgsRectangle(r.xmin, r.ymin, r.xmax, r.ymax))
        return self._engine().contains(g.constGet())


def scan_layer(uri: str, occ: Occupancy, is_canceled: Callable[[], bool]) -> Tuple[int, int]:
    """Add every feature of a packaged table; returns (features read, features with geometry)."""
    lyr = QgsVectorLayer(uri, "scan", "ogr")
    total = with_geom = 0
    try:
        for f in lyr.getFeatures(QgsFeatureRequest().setNoAttributes()):
            total += 1
            g = f.geometry()
            if g.isNull() or g.isEmpty():
                continue
            b = g.boundingBox()
            t = _GeometryTester(g)
            occ.add_feature(Rect(b.xMinimum(), b.yMinimum(), b.xMaximum(), b.yMaximum()), t.intersects, t.contains)
            with_geom += 1
            if total % 5000 == 0 and is_canceled():
                break
    finally:
        sip.delete(lyr)
    return total, with_geom
