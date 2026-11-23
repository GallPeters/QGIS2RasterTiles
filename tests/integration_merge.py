"""Integration test of the "Merge XYZ tiles (GPKG)" tool.

    python-qgis-dev.bat tests/integration_merge.py [--out DIR]

Plugin run for zooms 12-16, a second plugin run for zoom 17 only, merged with
the tool (through the Processing registry), compared with ONE built-in run
for zooms 12-17: tile keys and bytes must be identical.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shutil
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qgis_fixtures import EXTENT_4326, ROOT, build_test_project, start_qgis  # noqa: E402

start_qgis()

from qgis.core import Qgis, QgsApplication, QgsProcessingContext, QgsProcessingFeedback, QgsProject  # noqa: E402

import src.algorithm as algmod  # noqa: E402
from src.builtin_alg import find_xyz_gpkg_algorithm, map_parameters  # noqa: E402
from src.config import load_config  # noqa: E402
from src.job_runner import find_qgis_process  # noqa: E402
from src.provider import QGIS2RasterTilesProvider  # noqa: E402
from src.tile_grid import Rect  # noqa: E402

EXT = (34.70, 31.98, 34.90, 32.18)


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
    cfg = dataclasses.replace(load_config(), metatile_size=4, job_startup_s=1.0, cost_per_metatile_s=0.01)
    algmod.load_config = lambda path=None: cfg
    reg = QgsApplication.processingRegistry()
    if reg.providerById("qgis2rastertiles") is None:
        reg.addProvider(QGIS2RasterTilesProvider())
    QgsProject.instance().read(project)
    ctx = QgsProcessingContext()
    ctx.setProject(QgsProject.instance())
    extent = f"{EXT[0]},{EXT[2]},{EXT[1]},{EXT[3]} [EPSG:4326]"

    outs = {}
    for name, zmin, zmax in (("z12-16", 12, 16), ("z17", 17, 17)):
        res, ok = reg.algorithmById("qgis2rastertiles:parallelxyztilesgpkg").run(
            {"EXTENT": extent, "ZOOM_MIN": zmin, "ZOOM_MAX": zmax, "CPU_PERCENT": 100,
             "OUTPUT_DIR": os.path.join(a.out, name)}, ctx, QgsProcessingFeedback())
        assert ok, res
        outs[name] = res["OUTPUT_FILE"]
        print(f"plugin {name}: {res['TILE_COUNT']} tiles")

    merged = os.path.join(a.out, "merged.gpkg")
    res, ok = reg.algorithmById("qgis2rastertiles:mergetilesgpkg").run(
        {"INPUTS": [outs["z12-16"], outs["z17"]], "OUTPUT": merged}, ctx, QgsProcessingFeedback())
    assert ok, res
    print(f"merge tool: {res['TILE_COUNT']} tiles -> {res['OUTPUT']}")

    mapping = map_parameters(find_xyz_gpkg_algorithm())
    ref = os.path.join(a.out, "builtin_z12-17.gpkg")
    inputs = mapping.job_inputs(cfg, Rect(*EXT), "EPSG:4326", 12, 17, ref)
    qp = find_qgis_process(QgsApplication.prefixPath(), Qgis.version())
    cp = subprocess.run([str(qp), "--skip-loading-plugins", "run", mapping.algorithm_id, "-"],
                        input=json.dumps({"inputs": inputs, "project_path": project}), capture_output=True, text=True)
    assert cp.returncode == 0, cp.stderr[-2000:]

    A, B = tiles(ref), tiles(merged)
    same = A.keys() == B.keys() and all(A[k] == B[k] for k in A)
    per_zoom = {z: sum(1 for k in B if k[0] == z) for z in range(12, 18)}
    print(f"built-in single run z12-17: {len(A)} tiles; merged: {len(B)} tiles; per zoom {per_zoom}")
    con = sqlite3.connect(merged)
    zooms = [r[0] for r in con.execute("SELECT zoom_level FROM gpkg_tile_matrix ORDER BY 1")]
    con.close()
    print(f"gpkg_tile_matrix zooms in merged file: {zooms}")
    QgsProject.instance().clear()
    print("RESULT:", "identical to a single built-in run" if same else "DIFFERENT")
    return 0 if same and zooms == list(range(12, 18)) else 1


if __name__ == "__main__":
    sys.exit(main())
