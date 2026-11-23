"""Fill a :class:`partition.Occupancy` from the exported layers (QGIS side).

Features are read from the lean FlatGeobuf files, which are already in the
target CRS. Small features contribute their envelope; large ones get an exact
GEOS test (prepared geometry) for every candidate metatile rectangle.
Layers whose rendering is not bounded by their features (inverted polygons)
mark everything; raster/other layers mark their extent.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCoordinateTransformContext,
    QgsCsException,
    QgsFeatureRequest,
    QgsGeometry,
    QgsMapLayer,
    QgsRectangle,
    QgsVectorLayer,
)

from .exporter import dispose_layer
from .partition import Occupancy
from .tile_grid import Rect

log = logging.getLogger("qgis2rastertiles." + __name__.rsplit(".", 1)[-1])

#: Renderers that draw outside of their features' geometry.
UNBOUNDED_RENDERERS = {"invertedPolygonRenderer"}


def _rect(r: QgsRectangle) -> Rect:
    return Rect(r.xMinimum(), r.yMinimum(), r.xMaximum(), r.yMaximum())


class _GeometryTester:
    """Exact rectangle tests against one feature geometry (prepared lazily).

    Every QgsGeometry whose ``constGet()`` pointer is handed to GEOS is kept
    in a variable: ``QgsGeometry.fromRect(...).constGet()`` on a temporary
    would leave GEOS reading a deleted geometry (wrong answers or a crash).
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
        rect_geom = QgsGeometry.fromRect(QgsRectangle(r.xmin, r.ymin, r.xmax, r.ymax))
        return self._engine().intersects(rect_geom.constGet())

    def contains(self, r: Rect) -> bool:
        rect_geom = QgsGeometry.fromRect(QgsRectangle(r.xmin, r.ymin, r.xmax, r.ymax))
        return self._engine().contains(rect_geom.constGet())


def scan_vector_file(path: str, occ: Occupancy, is_canceled: Callable[[], bool],
                     progress: Optional[Callable[[float], None]] = None) -> int:
    """Add every feature of an exported file; returns the feature count."""
    lyr = QgsVectorLayer(path, "scan", "ogr")
    try:
        total = max(1, lyr.featureCount())
        req = QgsFeatureRequest().setNoAttributes()
        n = 0
        for f in lyr.getFeatures(req):
            g = f.geometry()
            if g.isNull() or g.isEmpty():
                continue
            bb = _rect(g.boundingBox())
            tester = _GeometryTester(g)
            occ.add_feature(bb, tester.intersects, tester.contains)
            n += 1
            if n % 5000 == 0:
                if is_canceled():
                    break
                if progress:
                    progress(n / total)
        return n
    finally:
        dispose_layer(lyr)


def scan_other_layer(layer: QgsMapLayer, occ: Occupancy, target_crs: QgsCoordinateReferenceSystem,
                     transform_context: QgsCoordinateTransformContext) -> None:
    """Mark the extent of a non-vector layer (raster, mesh, ...)."""
    ext = layer.extent()
    if layer.crs() != target_crs:
        ct = QgsCoordinateTransform(layer.crs(), target_crs, transform_context)
        ct.setBallparkTransformsAreAppropriate(True)
        try:
            ext = ct.transformBoundingBox(ext)
        except QgsCsException:
            log.warning("Cannot transform the extent of '%s': treating the whole output as occupied", layer.name())
            occ.mark_all()
            return
    if ext.isNull() or not ext.isFinite():
        occ.mark_all()
        return
    occ.add_rect(_rect(ext))
