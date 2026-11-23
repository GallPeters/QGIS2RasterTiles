"""Integration test: built-in single run vs. the plugin, same inputs.

Usage (QGIS Python):
    python-qgis-dev.bat tests/integration_compare.py [--out DIR] [--zmin 10] [--zmax 16]
        [--metatile 2] [--split 13] [--jobs 4] [--points 400] [--project path.qgz]

* builds the synthetic test project (unless --project is given),
* runs the built-in "Generate XYZ tiles (GeoPackage)" once over the extent
  with qgis_process (timed),
* runs the plugin algorithm in-process with the same settings (timed),
* compares per zoom: tile keys (must be identical) and decoded pixels
  (differences are reported; only partition-edge label effects are expected).

Exit code 0 when the tile key sets are identical.
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
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qgis_fixtures import EXTENT_4326, ROOT, build_test_project, start_qgis  # noqa: E402

start_qgis()

from qgis.core import (  # noqa: E402
    Qgis,
    QgsApplication,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsProject,
    QgsRectangle,
)
from qgis.PyQt.QtGui import QImage  # noqa: E402

import src.algorithm as algmod  # noqa: E402
from src.algorithm import ParallelXyzTilesAlgorithm  # noqa: E402
from src.builtin_alg import find_xyz_gpkg_algorithm, map_parameters  # noqa: E402
from src.config import load_config  # noqa: E402
from src.job_runner import find_qgis_process  # noqa: E402
from src.tile_grid import Rect  # noqa: E402


class PrintFeedback(QgsProcessingFeedback):
    def pushInfo(self, info):  # noqa: N802
        print("   ", info, flush=True)

    def pushWarning(self, w):  # noqa: N802
        print("    WARNING:", w, flush=True)

    def reportError(self, e, fatal=False):  # noqa: N802
        print("    ERROR:", e, flush=True)


def read_tiles(path):
    con = sqlite3.connect(path)
    t = con.execute("SELECT table_name FROM gpkg_contents WHERE data_type='tiles'").fetchone()[0]
    rows = {(z, c, r): d for z, c, r, d in con.execute(f'SELECT zoom_level, tile_column, tile_row, tile_data FROM "{t}"')}
    con.close()
    return rows


def pixel_diff(a: bytes, b: bytes):
    """(differing pixels, max channel difference)."""
    ia, ib = QImage.fromData(a), QImage.fromData(b)
    ia = ia.convertToFormat(QImage.Format.Format_ARGB32)
    ib = ib.convertToFormat(QImage.Format.Format_ARGB32)
    if ia.size() != ib.size():
        return -1, 255
    n, mx = 0, 0
    for y in range(ia.height()):
        for x in range(ia.width()):
            pa, pb = ia.pixel(x, y), ib.pixel(x, y)
            if pa != pb:
                n += 1
                d = max(abs(((pa >> s) & 255) - ((pb >> s) & 255)) for s in (0, 8, 16, 24))
                mx = max(mx, d)
    return n, mx


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "tests", "_integration_out"))
    ap.add_argument("--zmin", type=int, default=10)
    ap.add_argument("--zmax", type=int, default=16)
    ap.add_argument("--metatile", type=int, default=2)
    ap.add_argument("--split", default="13")
    ap.add_argument("--jobs", default="4", help="number of parallel processes, or 'auto' (adaptive)")
    ap.add_argument("--ram-per-job", type=float, default=None, help="override ram_per_job_gb")
    ap.add_argument("--feature-cost", type=float, default=None, help="override cost_per_feature_s (planning only)")
    ap.add_argument("--points", type=int, default=400)
    ap.add_argument("--startup", default="0.5", help="job_startup_s ('auto' to measure)")
    ap.add_argument("--project", default=None)
    ap.add_argument("--extent", default=None, help="xmin,ymin,xmax,ymax in EPSG:4326")
    a = ap.parse_args()

    if os.path.isdir(a.out):
        shutil.rmtree(a.out, ignore_errors=True)
    os.makedirs(a.out, exist_ok=True)
    project_path = a.project or build_test_project(os.path.join(a.out, "src"), n_points=a.points)
    ext = tuple(float(v) for v in a.extent.split(",")) if a.extent else EXTENT_4326

    base_cfg = load_config()
    cfg = dataclasses.replace(
        base_cfg, metatile_size=a.metatile, max_parallel_jobs=a.jobs if a.jobs == "auto" else int(a.jobs),
        ram_per_job_gb=a.ram_per_job or base_cfg.ram_per_job_gb,
        cost_per_feature_s=a.feature_cost if a.feature_cost is not None else base_cfg.cost_per_feature_s,
        split_zoom=a.split if a.split == "auto" else int(a.split),
        job_startup_s=a.startup if a.startup == "auto" else float(a.startup), keep_temp_files=True)
    print("Config:", json.dumps(cfg.to_dict()))

    # -------- built-in, single run -------------------------------------------
    mapping = map_parameters(find_xyz_gpkg_algorithm())
    qp = find_qgis_process(QgsApplication.prefixPath(), Qgis.version())
    builtin_out = os.path.join(a.out, "builtin.gpkg")
    inputs = mapping.job_inputs(cfg, Rect(*ext), "EPSG:4326", a.zmin, a.zmax, builtin_out)
    payload = json.dumps({"inputs": inputs, "project_path": project_path})
    print(f"\n[built-in] {mapping.algorithm_id} zooms {a.zmin}-{a.zmax} ...", flush=True)
    t0 = time.monotonic()
    cp = subprocess.run([str(qp), "--skip-loading-plugins", "run", mapping.algorithm_id, "-"], input=payload,
                        capture_output=True, text=True)
    t_builtin = time.monotonic() - t0
    if cp.returncode != 0:
        print(cp.stdout[-3000:], cp.stderr[-3000:])
        return 2
    print(f"[built-in] done in {t_builtin:.1f}s")

    # -------- plugin -------------------------------------------------------------
    algmod.load_config = lambda path=None: cfg  # use the test configuration
    project = QgsProject.instance()
    assert project.read(project_path)
    alg = ParallelXyzTilesAlgorithm().create()
    ctx = QgsProcessingContext()
    ctx.setProject(project)
    fb = PrintFeedback()
    plugin_dir = os.path.join(a.out, "plugin")
    params = {
        "EXTENT": f"{ext[0]},{ext[2]},{ext[1]},{ext[3]} [EPSG:4326]",
        "ZOOM_MIN": a.zmin, "ZOOM_MAX": a.zmax, "CPU_PERCENT": 100, "OUTPUT_DIR": plugin_dir,
    }
    print(f"\n[plugin] zooms {a.zmin}-{a.zmax} ...", flush=True)
    t0 = time.monotonic()
    res, ok = alg.run(params, ctx, fb)
    t_plugin = time.monotonic() - t0
    if not ok:
        print("[plugin] FAILED")
        return 3
    print(f"[plugin] done in {t_plugin:.1f}s -> {res['OUTPUT_FILE']}")

    # -------- compare ---------------------------------------------------------------
    A, B = read_tiles(builtin_out), read_tiles(res["OUTPUT_FILE"])
    keys_ok = True
    print("\nzoom | built-in | plugin | only built-in | only plugin | identical bytes | pixel-different (max diff)")
    for z in range(a.zmin, a.zmax + 1):
        ka = {k for k in A if k[0] == z}
        kb = {k for k in B if k[0] == z}
        same = ka & kb
        ident = sum(1 for k in same if A[k] == B[k])
        diffs = []
        for k in same:
            if A[k] != B[k]:
                n, mx = pixel_diff(A[k], B[k])
                if n:
                    diffs.append((k, n, mx))
        keys_ok &= ka == kb
        print(f"{z:4d} | {len(ka):8d} | {len(kb):6d} | {len(ka - kb):13d} | {len(kb - ka):11d} | "
              f"{ident:15d} | {len(diffs)} ({max((d[2] for d in diffs), default=0)})")
        for k, n, mx in sorted(diffs, key=lambda d: -d[1])[:3]:
            print(f"       tile {k}: {n} px differ, max channel diff {mx}")
    print(f"\nTiming: built-in {t_builtin:.1f}s, plugin {t_plugin:.1f}s "
          f"(speed-up x{t_builtin / max(t_plugin, 1e-9):.2f})")
    print("RESULT:", "tile keys identical" if keys_ok else "TILE KEYS DIFFER")
    project.clear()
    return 0 if keys_ok else 1


if __name__ == "__main__":
    sys.exit(main())
