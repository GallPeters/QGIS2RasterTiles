"""The "Merge XYZ tiles (GPKG)" Processing algorithm.

Merges several tile GeoPackages that share one tile matrix set into one,
e.g. the result of a zooms 0-18 run and of a zoom 19 run. Uses only SQLite,
so it runs in the Processing worker thread without touching any QGIS layer.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from qgis.core import (
    Qgis,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingOutputNumber,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterMultipleLayers,
)
from qgis.PyQt.QtCore import QCoreApplication

from .merger import MergeError, merge_tile_geopackages


def gpkg_path(source: str) -> str:
    """File path of a GeoPackage given as a path, 'path|layername=..' or 'GPKG:path:table'."""
    p = source.split("|")[0]
    if p.upper().startswith("GPKG:"):
        p = p[5:]
        if not os.path.isfile(p) and ":" in p:
            head = p.rsplit(":", 1)[0]
            if os.path.isfile(head):
                p = head
    return p


class MergeTileGeoPackagesAlgorithm(QgsProcessingAlgorithm):
    INPUTS = "INPUTS"
    OUTPUT = "OUTPUT"
    TILE_COUNT = "TILE_COUNT"

    def tr(self, s: str) -> str:
        return QCoreApplication.translate("QGIS2RasterTiles", s)

    def createInstance(self):  # noqa: N802
        return MergeTileGeoPackagesAlgorithm()

    def name(self) -> str:
        return "mergetilesgpkg"

    def displayName(self) -> str:  # noqa: N802
        return self.tr("Merge XYZ tiles (GPKG)")

    def shortHelpString(self) -> str:  # noqa: N802
        return self.tr(
            "Merges XYZ tile GeoPackages into one, e.g. a zooms 0-18 result and a zoom 19 result.\n\n"
            "The inputs must use the same tile matrix (same CRS, zoom-0 extent and tile size). "
            "A tile that exists in several inputs is taken from the first input in the list. "
            "The inputs are not modified.")

    def initAlgorithm(self, config: Optional[Dict[str, Any]] = None) -> None:  # noqa: N802
        self.addParameter(QgsProcessingParameterMultipleLayers(
            self.INPUTS, self.tr("Tile GeoPackages"), Qgis.ProcessingSourceType.File))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUTPUT, self.tr("Merged GeoPackage"), self.tr("GeoPackage (*.gpkg)")))
        self.addOutput(QgsProcessingOutputNumber(self.TILE_COUNT, self.tr("Number of tiles")))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        inputs = self.parameterAsFileList(parameters, self.INPUTS, context)
        output = self.parameterAsFileOutput(parameters, self.OUTPUT, context)
        # loaded GeoPackage tile layers may come as "GPKG:<path>:<table>" or "<path>|..."
        paths = [gpkg_path(p) for p in inputs]
        if not paths:
            raise QgsProcessingException(self.tr("Select at least one tile GeoPackage"))
        feedback.pushInfo(f"Merging {len(paths)} GeoPackage(s) ...")
        try:
            rep = merge_tile_geopackages(paths, output, progress=lambda f: feedback.setProgress(100 * f),
                                         is_canceled=feedback.isCanceled)
        except MergeError as e:
            raise QgsProcessingException(str(e)) from e
        zooms = sorted(rep.tiles_per_zoom)
        feedback.pushInfo(f"Done: {rep.total_tiles} tiles, zooms {zooms[0]}-{zooms[-1]} -> {output}"
                          if zooms else f"Done: no tiles -> {output}")
        if rep.ignored_duplicates:
            feedback.pushInfo(f"{rep.ignored_duplicates} tile(s) existed in more than one input; "
                              f"kept the first input's copy")
        return {self.OUTPUT: output, self.TILE_COUNT: rep.total_tiles}
