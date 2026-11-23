"""Build the plugin zip for the QGIS plugin repository.

    python tools/build_release.py            # version from metadata.txt
    python tools/build_release.py --icon     # also re-render icon.png from icon.svg (needs QGIS Python)

Writes releases/QGIS2RasterTiles-<version>.zip containing a single
``QGIS2RasterTiles/`` folder with the runtime files only (no tests, tools,
releases, caches).
"""

from __future__ import annotations

import argparse
import configparser
import os
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = "QGIS2RasterTiles"
FILES = ["__init__.py", "metadata.txt", "LICENSE", "README.md", "defaults.json", "icon.png", "icon.svg"]


def render_icon(size: int = 64) -> None:
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QGuiApplication, QImage, QPainter
    from qgis.PyQt.QtSvg import QSvgRenderer

    _app = QGuiApplication.instance() or QGuiApplication([])  # noqa: F841
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    painter = QPainter(img)
    QSvgRenderer(os.path.join(ROOT, "icon.svg")).render(painter)
    painter.end()
    img.save(os.path.join(ROOT, "icon.png"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--icon", action="store_true")
    a = ap.parse_args()
    if a.icon:
        render_icon()
    meta = configparser.ConfigParser()
    meta.read(os.path.join(ROOT, "metadata.txt"), encoding="utf-8")
    version = meta["general"]["version"]
    os.makedirs(os.path.join(ROOT, "releases"), exist_ok=True)
    out = os.path.join(ROOT, "releases", f"{PLUGIN}-{version}.zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in FILES:
            z.write(os.path.join(ROOT, f), f"{PLUGIN}/{f}")
        for f in sorted(os.listdir(os.path.join(ROOT, "src"))):
            if f.endswith(".py"):
                z.write(os.path.join(ROOT, "src", f), f"{PLUGIN}/src/{f}")
    print(out)


if __name__ == "__main__":
    main()
