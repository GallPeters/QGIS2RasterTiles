"""Integration test of the "Merge XYZ tiles (GPKG)" tool.

    python-qgis-dev.bat tests/integration_merge.py [--out DIR]

Plugin run for zooms 12-16, a second run for zoom 17 only, merged with the tool,
compared with ONE built-in run for zooms 12-17: tile keys and bytes must match.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qgis_fixtures import ROOT, build_test_project, start_qgis  # noqa: E402

start_qgis()

from qgis.core import QgsApplication, QgsProcessingContext, QgsProcessingFeedback, QgsProject  # noqa: E402

from src.builtin_alg import as_argument, find_xyz_gpkg_algorithm, load_defaults  # noqa: E402
from src.provider import QGIS2RasterTilesProvider  # noqa: E402
from src.qgis_process import find_qgis_process  # noqa: E402

sys.path.append(os.path.join(QgsApplication.prefixPath(), "python", "plugins"))
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
EXTENT = "34.70,34.90,31.98,32.18 [EPSG:4326]"


def tiles(path):
    con = sqlite3.connect(path)
    rows = {(z, c, r): d for z, c, r, d in con.execute("SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles")}
    con.close()
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "tests", "_integration_merge"))
    a = ap.parse_args()
    shutil.rmtree(a.out, ignore_errors=True)
    project = build_test_project(os.path.join(a.out, "src"))
    reg = QgsApplication.processingRegistry()
    if reg.providerById("qgis2rastertiles") is None:
        reg.addProvider(QGIS2RasterTilesProvider())
    QgsProject.instance().read(project)
    ctx = QgsProcessingContext()
    ctx.setProject(QgsProject.instance())

    outs = {}
    for name, zmin, zmax in (("z12-16", 12, 16), ("z17", 17, 17)):
        res, ok = reg.algorithmById("qgis2rastertiles:xyztilesgpkg").run(
            {"EXTENT": EXTENT, "ZOOM_MIN": zmin, "ZOOM_MAX": zmax, "CPU_PERCENT": 100,
             "OUTPUT_DIR": os.path.join(a.out, name)}, ctx, QgsProcessingFeedback())
        assert ok, res
        outs[name] = res["OUTPUT_FILE"]
        print(f"plugin {name}: {res['TILE_COUNT']} tiles")

    merged = os.path.join(a.out, "merged.gpkg")
    res, ok = reg.algorithmById("qgis2rastertiles:mergetilesgpkg").run(
        {"INPUTS": [outs["z12-16"], outs["z17"]], "OUTPUT": merged}, ctx, QgsProcessingFeedback())
    assert ok, res
    print(f"merge tool: {res['TILE_COUNT']} tiles")

    xyz = find_xyz_gpkg_algorithm()
    ref = os.path.join(a.out, "builtin_z12-17.gpkg")
    args = [find_qgis_process(QgsApplication.prefixPath()), "run", xyz.id(), f"--PROJECT_PATH={project}",
            f"--EXTENT={EXTENT}", "--ZOOM_MIN=12", "--ZOOM_MAX=17", f"--OUTPUT_FILE={ref}"]
    args += [f"--{k}={as_argument(v)}" for k, v in load_defaults(xyz).items()]
    cp = subprocess.run(args, capture_output=True, text=True)
    assert cp.returncode == 0, cp.stdout[-2000:]

    A, B = tiles(ref), tiles(merged)
    same = A.keys() == B.keys() and all(A[k] == B[k] for k in A)
    con = sqlite3.connect(merged)
    zooms = [r[0] for r in con.execute("SELECT zoom_level FROM gpkg_tile_matrix ORDER BY 1")]
    con.close()
    print(f"built-in z12-17: {len(A)} tiles; merged: {len(B)} tiles; tile matrix zooms {zooms}")
    QgsProject.instance().clear()
    print("RESULT:", "identical to a single built-in run" if same else "DIFFERENT")
    return 0 if same and zooms == list(range(12, 18)) else 1


if __name__ == "__main__":
    sys.exit(main())
