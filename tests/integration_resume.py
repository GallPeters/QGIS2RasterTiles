"""Integration test: resume an interrupted run whose plan came from an older planner.

    python-qgis-dev.bat tests/integration_resume.py [--out DIR]

1. full plugin run (reference output),
2. simulate an interrupted run with an old-version plan: drop the plan version,
   delete half of the finished parts and the final GeoPackage,
3. run again with the same inputs: finished parts must be kept, the rest
   re-planned, and the output must be identical to the reference.
"""

from __future__ import annotations

import argparse
import dataclasses
import glob
import json
import os
import shutil
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qgis_fixtures import ROOT, build_test_project, start_qgis  # noqa: E402

start_qgis()

from qgis.core import QgsProcessingContext, QgsProcessingFeedback, QgsProject  # noqa: E402

import src.algorithm as algmod  # noqa: E402
from src.algorithm import ParallelXyzTilesAlgorithm  # noqa: E402
from src.config import load_config  # noqa: E402


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


def run(out_dir):
    alg = ParallelXyzTilesAlgorithm().create()
    ctx = QgsProcessingContext()
    ctx.setProject(QgsProject.instance())
    res, ok = alg.run({"EXTENT": "34.70,34.90,31.98,32.18 [EPSG:4326]", "ZOOM_MIN": 12, "ZOOM_MAX": 17,
                       "CPU_PERCENT": 100, "OUTPUT_DIR": out_dir}, ctx, Fb())
    assert ok, res
    return res["OUTPUT_FILE"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "tests", "_integration_resume"))
    a = ap.parse_args()
    shutil.rmtree(a.out, ignore_errors=True)
    project = build_test_project(os.path.join(a.out, "src"))
    cfg = dataclasses.replace(load_config(), metatile_size=4, split_zoom=13, job_startup_s=0.2,
                              keep_temp_files=True, max_parallel_jobs=4)
    algmod.load_config = lambda path=None: cfg
    QgsProject.instance().read(project)

    print("[1] reference run")
    ref = tiles(run(os.path.join(a.out, "plugin")))

    work = glob.glob(os.path.join(a.out, "plugin", ".pxyz_work", "*"))[0]
    plan_path = os.path.join(work, "plan.json")
    plan = json.load(open(plan_path))
    plan.pop("version", None)  # as written by an older planner
    json.dump(plan, open(plan_path, "w"))
    man_path = os.path.join(work, "manifest.json")
    man = json.load(open(man_path))
    ids = sorted(man["completed"])
    removed = ids[::2]
    for jid in removed:
        os.remove(man["completed"][jid]["output"])
    os.remove(os.path.join(a.out, "plugin", "tiles.gpkg"))
    print(f"[2] simulated interruption: {len(ids) - len(removed)} of {len(ids)} parts kept, plan marked old")

    print("[3] resumed run")
    again = tiles(run(os.path.join(a.out, "plugin")))
    same = ref.keys() == again.keys() and all(ref[k] == again[k] for k in ref)
    print(f"tiles: reference {len(ref)}, resumed {len(again)}; identical: {same}")
    new_plan = json.load(open(plan_path))
    print(f"new plan version {new_plan.get('version')}, replanned flag {new_plan['stats'].get('replanned')}")
    QgsProject.instance().clear()
    print("RESULT:", "OK" if same else "DIFFERENT")
    return 0 if same else 1


if __name__ == "__main__":
    sys.exit(main())
