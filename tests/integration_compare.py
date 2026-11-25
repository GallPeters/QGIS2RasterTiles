"""Integration test: the plugin vs the built-in tool on the original project.

    python-qgis-dev.bat tests/integration_compare.py [--out DIR] [--zmin 12] [--zmax 17]

Runs "Generate XYZ tiles (GeoPackage)" once on the original test project and the
plugin once with the same settings (defaults.json), then compares tile keys and
bytes per zoom and reports the timing and the priority of the rendering process.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time

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


class Fb(QgsProcessingFeedback):
    def pushInfo(self, info):  # noqa: N802
        print("   ", info, flush=True)

    def pushWarning(self, w):  # noqa: N802
        print("    WARNING:", w, flush=True)


def tiles(path):
    con = sqlite3.connect(path)
    rows = {(z, c, r): d for z, c, r, d in con.execute("SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles")}
    con.close()
    return rows


def watch_priority(stop, seen):
    """Record the Windows priority class of running qgis_process.exe (via PowerShell)."""
    while not stop.is_set():
        ps = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0",
                          "powershell.exe")
        cp = subprocess.run([ps, "-NoProfile", "-Command",
                             "(Get-Process qgis_process -ErrorAction SilentlyContinue).PriorityClass"],
                            capture_output=True, text=True)
        for line in cp.stdout.split():
            seen[line] = seen.get(line, 0) + 1
        time.sleep(1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "tests", "_integration_out"))
    ap.add_argument("--zmin", type=int, default=12)
    ap.add_argument("--zmax", type=int, default=17)
    ap.add_argument("--extent", default="34.70,34.90,31.98,32.18", help="xmin,xmax,ymin,ymax in EPSG:4326")
    a = ap.parse_args()
    shutil.rmtree(a.out, ignore_errors=True)
    project_path = build_test_project(os.path.join(a.out, "src"))
    extent = f"{a.extent} [EPSG:4326]"

    xyz = find_xyz_gpkg_algorithm()
    defaults = load_defaults(xyz)
    ref = os.path.join(a.out, "builtin.gpkg")
    args = [find_qgis_process(QgsApplication.prefixPath()), "run", xyz.id(), f"--PROJECT_PATH={project_path}",
            f"--EXTENT={extent}", f"--ZOOM_MIN={a.zmin}", f"--ZOOM_MAX={a.zmax}", f"--OUTPUT_FILE={ref}"]
    args += [f"--{k}={as_argument(v)}" for k, v in defaults.items()]
    print(f"[built-in] {xyz.id()} zooms {a.zmin}-{a.zmax} on the original project ...", flush=True)
    t0 = time.monotonic()
    cp = subprocess.run(args, capture_output=True, text=True)
    t_builtin = time.monotonic() - t0
    if cp.returncode != 0:
        print(cp.stdout[-2000:], cp.stderr[-2000:])
        return 2
    print(f"[built-in] {t_builtin:.1f}s")

    reg = QgsApplication.processingRegistry()
    if reg.providerById("qgis2rastertiles") is None:
        reg.addProvider(QGIS2RasterTilesProvider())
    project = QgsProject.instance()
    project.read(project_path)
    ctx = QgsProcessingContext()
    ctx.setProject(project)
    stop, seen = threading.Event(), {}
    watcher = threading.Thread(target=watch_priority, args=(stop, seen), daemon=True)
    watcher.start()
    print(f"[plugin] zooms {a.zmin}-{a.zmax} ...", flush=True)
    t0 = time.monotonic()
    res, ok = reg.algorithmById("qgis2rastertiles:xyztilesgpkg").run(
        {"EXTENT": extent, "ZOOM_MIN": a.zmin, "ZOOM_MAX": a.zmax, "CPU_PERCENT": 100,
         "OUTPUT_DIR": os.path.join(a.out, "plugin")}, ctx, Fb())
    t_plugin = time.monotonic() - t0
    stop.set()
    if not ok:
        print("[plugin] FAILED", res)
        return 3
    print(f"[plugin] {t_plugin:.1f}s")

    A, B = tiles(ref), tiles(res["OUTPUT_FILE"])
    keys_ok = True
    print("\nzoom | built-in | plugin | identical bytes")
    for z in range(a.zmin, a.zmax + 1):
        ka = {k for k in A if k[0] == z}
        kb = {k for k in B if k[0] == z}
        keys_ok &= ka == kb
        print(f"{z:4d} | {len(ka):8d} | {len(kb):6d} | {sum(1 for k in ka & kb if A[k] == B[k]):15d}")
    same = keys_ok and all(A[k] == B[k] for k in A)
    print(f"\npriority of qgis_process during the plugin run: {seen}")
    print(f"Timing: built-in {t_builtin:.1f}s, plugin {t_plugin:.1f}s (includes packaging and the project copy)")
    print("RESULT:", "identical tiles" if same else ("same tile keys, some pixels differ" if keys_ok else "TILE KEYS DIFFER"))
    project.clear()
    return 0 if keys_ok else 1


if __name__ == "__main__":
    sys.exit(main())
