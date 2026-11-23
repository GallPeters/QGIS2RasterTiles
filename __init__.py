"""QGIS2RasterTiles plugin for QGIS."""


def classFactory(iface):  # noqa: N802 - QGIS plugin entry point
    """Invoke the plugin (QGIS imports are kept lazy so ``src`` stays testable without QGIS)."""
    from .src.plugin import QGIS2RasterTilesPlugin

    return QGIS2RasterTilesPlugin(iface)
