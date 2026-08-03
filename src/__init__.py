"""Init"""
def classFactory(iface):
    from .plugin_main import XyzGpkgPlugin
    return XyzGpkgPlugin(iface)