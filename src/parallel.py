"""Step 6 with the "Parallel workers" method.

The built-in tool keeps only a few cores busy per run, so this method runs many
copies of it at once, each on its own part of the map, and merges the parts.

Exactness (no overlap, no gap):
* The built-in tool anchors metatiles at the first tile of the requested range,
  per zoom. Every job is either a FULL-extent job (some zooms, exactly the single
  run's metatiles) or a rectangle of the single run's own metatiles at ONE zoom.
* :func:`verify_plan` proves before rendering that, at every zoom, each metatile
  belongs to exactly one job (only metatiles without features may be left out,
  and only when empty tiles are skipped) and that each job's extent selects
  exactly its tiles.
* :func:`verify_parts` checks after rendering that every part holds only its own
  tiles; the merge must then find zero duplicates, and the result must open in
  GDAL with every zoom level.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from .job_runner import Concurrency, JobSpec, run_jobs, run_one
from .merger import merge_parts
from .occupancy_scan import scan_layer
from .partition import (CostModel, Job, Occupancy, PartitionPlan, PlanError, build_plan, choose_split_zoom,
                        parse_extent, verify_parts, verify_plan)
from .sysload import available_memory_gb
from .tile_grid import Rect, TileGrid, TileRange

EDGE_MARGIN_PX = 100          # symbols/labels reach at most this far beyond their feature
SPLIT_TILE_THRESHOLD = 20000  # zooms with fewer tiles than this run as full-extent jobs
RAM_PER_JOB_GB = 2.0
COST_PER_FEATURE_S = 0.0005
UNBOUNDED_RENDERERS = {"invertedPolygonRenderer"}  # draw outside their features


def run_parallel(*, xyz, defaults: Dict[str, Any], crs_authid: str, extent: Rect, zmin: int, zmax: int,
                 make_args: Callable[[Rect, int, int, str], List[str]], sources: Dict[str, str],
                 counts: Dict[str, int], renderers: Dict[str, str], has_non_vector: bool, output: str,
                 work: str, cpu_percent: int, feedback, info: Callable[[str], None]) -> Dict[str, Any]:
    """Plan, verify, render in parallel, verify, merge. Returns summary values."""
    def value(key):
        return defaults.get(key, xyz.parameterDefinition(key).defaultValue())

    z0 = value("Z0_EXTENT")
    grid = TileGrid.for_target(crs_authid, parse_extent(str(z0)) if z0 else (0, 1, 0, 1),
                               int(value("Z0_MATRIX_WIDTH")), int(value("Z0_MATRIX_HEIGHT")),
                               int(value("TILE_WIDTH")), int(value("TILE_HEIGHT")))
    metatile = int(value("METATILESIZE"))
    skip_empty = bool(value("SKIP_EMPTY_TILES"))
    split = choose_split_zoom(grid, extent, zmin, zmax, SPLIT_TILE_THRESHOLD)
    total_features = sum(counts.values())
    logs = os.path.join(work, "logs")
    os.makedirs(logs, exist_ok=True)

    # where features are (zooms from split up)
    occ = None
    if skip_empty and split <= zmax:
        occ = Occupancy(grid, extent, range(split, zmax + 1), metatile, EDGE_MARGIN_PX)
        if has_non_vector or any(renderers.get(lid) in UNBOUNDED_RENDERERS for lid in sources):
            info("   a visible layer draws everywhere (raster, inverted polygons): no empty areas skipped")
            occ.mark_all()
        else:
            for lid, uri in sources.items():
                read, _ = scan_layer(uri, occ, feedback.isCanceled)
                if feedback.isCanceled():
                    return {}
                if read != counts[lid]:
                    raise PlanError(f"{uri}: read {read} features, {counts[lid]} packaged")

    # measure: job start-up and the cost of an empty metatile (two probe jobs)
    cancel = threading.Event()
    one = grid.tile_range(extent, zmin)
    one = TileRange(one.zoom, one.min_col, one.min_col, one.min_row, one.min_row)

    def probe(name: str, r: TileRange) -> float:
        out = os.path.join(work, f"{name}.gpkg")
        t0 = time.monotonic()
        run_one(JobSpec(name, make_args(grid.job_extent(r), r.zoom, r.zoom, out), out), cancel, logs, 0)
        os.remove(out)
        return time.monotonic() - t0

    startup = min(probe("probe_startup", one) for _ in range(2))
    per_metatile = 0.03
    block = _empty_block(occ)
    if block is not None:
        per_metatile = max(0.002, (probe("probe_metatiles", block[0]) - startup) / (block[1] - 1))
    info(f"   measured: job start-up {startup:.1f}s, empty metatile {per_metatile * 1000:.0f} ms")
    cm = CostModel(per_metatile, COST_PER_FEATURE_S, startup)

    # how many processes
    cpus = os.cpu_count() or 2
    usable = max(1, int(cpus * cpu_percent / 100))
    maximum = usable
    mem = available_memory_gb()
    if mem is not None:
        maximum = max(1, min(maximum, int(mem // RAM_PER_JOB_GB)))
    conc = Concurrency(max(1, min(usable // 2, maximum)), maximum, adaptive=True, cpu_target=float(min(95, cpu_percent)),
                       ram_per_job_gb=RAM_PER_JOB_GB)

    plan = build_plan(grid, extent, zmin, zmax, metatile, split, occ, cm, maximum, total_features)
    if not plan.jobs:  # nothing to draw: one tile still makes a valid, empty GeoPackage
        plan.jobs.append(Job(f"z{one.zoom}_c{one.min_col}_r{one.min_row}", one.zoom, one.zoom,
                             grid.job_extent(one), 1, 0, cm.per_metatile, one))
    else:
        verify_plan(grid, extent, zmin, zmax, metatile, plan, occ)
    info(f"   plan: {len(plan.jobs)} jobs, {100 * plan.stats.get('work_saved_share', 0):.0f}% of the map empty "
         f"and skipped; verified: every metatile in exactly one job")

    outputs = {j.job_id: os.path.join(work, "parts", f"{j.job_id}.gpkg") for j in plan.jobs}
    os.makedirs(os.path.join(work, "parts"), exist_ok=True)
    specs = [JobSpec(j.job_id, make_args(j.extent, j.zoom_min, j.zoom_max, outputs[j.job_id]),
                     outputs[j.job_id], j.workload + 1e-6) for j in plan.jobs]
    stats: Dict[str, Any] = {}
    t0 = time.monotonic()
    run_jobs(specs, conc, logs, feedback.isCanceled, lambda f: feedback.setProgress(95 * f), stats=stats)
    cpu = stats.get("avg_cpu")
    info(f"   rendered in {time.monotonic() - t0:.0f}s with up to {stats.get('peak_processes')} processes"
         + (f", average CPU {cpu:.0f}%" if cpu is not None else ""))

    verify_parts(plan.jobs, outputs)
    order = [j.job_id for j in plan.jobs if j.tile_range is None] + \
            [j.job_id for j in plan.jobs if j.tile_range is not None]

    def matrix_row(z: int):
        cols, rows = grid.matrix_size(z)
        px, py = grid.pixel_size(z)
        return cols, rows, grid.tile_width_px, grid.tile_height_px, px, py

    rep = merge_parts([outputs[i] for i in order], output, (extent.xmin, extent.ymin, extent.xmax, extent.ymax),
                      zmin, zmax, matrix_row, progress=lambda f: feedback.setProgress(95 + 5 * f),
                      is_canceled=feedback.isCanceled)
    if rep.ignored_duplicates:
        raise PlanError(f"{rep.ignored_duplicates} tiles were rendered by two jobs")
    check_output(output, zmin, zmax)
    return {"jobs": len(plan.jobs), "tiles": rep.total_tiles}


def _empty_block(occ: Optional[Occupancy], size: int = 16):
    """(tile range, metatile count) of a square block without features at the highest zoom, or None."""
    if occ is None:
        return None
    zo = occ.zooms[max(occ.zooms)]
    if zo.all_occupied:
        return None
    g = zo.mgrid
    for k in (size, 8, 4):
        if g.meta_cols < k or g.meta_rows < k:
            continue
        for i0 in range(0, g.meta_cols - k + 1, k):
            for j0 in range(0, g.meta_rows - k + 1, k):
                if not any((i, j) in zo.cells for i in range(i0, i0 + k) for j in range(j0, j0 + k)):
                    return g.tile_span(i0, i0 + k - 1, j0, j0 + k - 1), k * k
    return None


def check_output(path: str, zmin: int, zmax: int) -> None:
    """The merged GeoPackage must open in GDAL and be readable over its whole extent.

    (GDAL leaves out zoom levels where the whole area is smaller than a tile, so the
    number of overviews is not a fixed function of the zoom range.)
    """
    from osgeo import gdal
    gdal.UseExceptions()
    try:
        ds = gdal.Open(path)
        ds.ReadRaster(0, 0, ds.RasterXSize, ds.RasterYSize, 256, 256)  # served from the overviews
        ds = None
    except RuntimeError as e:
        raise PlanError(f"the output GeoPackage cannot be read by GDAL: {e}") from e
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        zooms = [r[0] for r in con.execute("SELECT zoom_level FROM gpkg_tile_matrix ORDER BY 1")]
    finally:
        con.close()
    if zooms != list(range(zmin, zmax + 1)):
        raise PlanError(f"the output's tile matrix has zooms {zooms}, expected {zmin}-{zmax}")
