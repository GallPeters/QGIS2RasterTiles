"""The end-to-end pipeline (runs in the Processing worker thread).

Steps
-----
1. adopt the layer clones captured on the main thread
2. analyse required fields per vector layer
3. export lean FlatGeobuf files (filtered, reprojected, only required fields)
4. build and save the render project (.qgz)
5. occupancy scan + partition into jobs
6. run the built-in algorithm in parallel ``qgis_process`` jobs
7. merge the part GeoPackages into the final file

All intermediate state lives in ``OUTPUT_DIR/.pxyz_work/<key>`` where ``key``
hashes every input that affects the result, so re-running the algorithm with
the same inputs resumes: finished exports, the project, the plan and every
finished job are reused.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsField,
    QgsGeometryGeneratorSymbolLayer,
    QgsProcessingFeedback,
    QgsRectangle,
    QgsRenderContext,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QMetaType, Qt

from .builtin_alg import ParameterMapping
from .config import TileConfig, resolve_max_parallel_jobs
from .exporter import ExportError, export_layer
from .field_analysis import analyze_layer_fields
from .job_runner import (
    JobFailed, JobSpec, JobsCanceled, Manifest, QgisProcessRunner, find_qgis_process, profile_arguments, run_jobs)
from .merger import merge_parts, validate_part
from .occupancy_scan import UNBOUNDED_RENDERERS, scan_other_layer, scan_vector_file
from .partition import CostModel, Job, Occupancy, PartitionPlan, build_plan, choose_split_zoom
from .project_builder import ProjectSnapshot, build_render_project
from .render_check import check_saved_project, missing_label_fonts, python_functions_used
from .tile_grid import Rect, TileGrid, TileRange

log = logging.getLogger("qgis2rastertiles.pipeline")

PIPELINE_VERSION = "2"
#: exported source feature id ($id): feature accounting and, optionally, drawing order
ID_FIELD = "_pxyz_fid"


class PipelineError(RuntimeError):
    pass


@dataclass
class PipelineInputs:
    cfg: TileConfig
    mapping: ParameterMapping
    snapshot: ProjectSnapshot
    target_crs: str
    extent: Rect           # in the target CRS
    zoom_min: int
    zoom_max: int
    output_dir: str
    cpu_percent: int
    qgis_prefix: str
    qgis_version: str
    settings_dir: str = ""  # QgsApplication.qgisSettingsDirPath() of the running QGIS (its user profile)


def _duration(seconds: float) -> str:
    """Human-readable duration (e.g. '45s', '12m 05s', '3h 07m')."""
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {(s % 3600) // 60:02d}m"


def _sha(obj) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class Pipeline:
    """Runs all steps; progress weights: export 15, project 2, plan 3, jobs 75, merge 5."""

    def __init__(self, inp: PipelineInputs, feedback: QgsProcessingFeedback, drain=None) -> None:
        self.inp = inp
        self.cfg = inp.cfg
        self.feedback = feedback
        self.drain = drain or (lambda: None)
        self.crs = QgsCoordinateReferenceSystem(inp.target_crs)
        c = self.cfg
        self.grid = TileGrid.for_target(inp.target_crs, c.zoom0_extent, c.zoom0_matrix_width,
                                        c.zoom0_matrix_height, c.tile_width, c.tile_height)
        self.timings: Dict[str, float] = {}

    # ------------------------------------------------------------------
    def progress(self, base: float, span: float, frac: float) -> None:
        self.feedback.setProgress(min(100.0, base + span * max(0.0, min(1.0, frac))))
        self.drain()

    def canceled(self) -> bool:
        return self.feedback.isCanceled()

    def check_cancel(self) -> None:
        if self.canceled():
            raise JobsCanceled("canceled by user")

    # ------------------------------------------------------------------
    def work_key(self) -> str:
        cfg = self.cfg.to_dict()
        for k in ("max_parallel_jobs", "keep_temp_files", "vacuum", "qgis_process_skip_plugins",
                  "qgis_process_path", "output_filename", "ram_per_job_gb", "max_job_retries"):
            cfg.pop(k, None)
        e = self.inp.extent
        return _sha({
            "v": PIPELINE_VERSION, "qgis": self.inp.qgis_version, "crs": self.inp.target_crs,
            "extent": [e.xmin, e.ymin, e.xmax, e.ymax], "zooms": [self.inp.zoom_min, self.inp.zoom_max],
            "cfg": cfg, "project": self.inp.snapshot.fingerprint(),
        })[:16]

    def export_filter_rect(self) -> Optional[QgsRectangle]:
        """Area rendered at ZOOM_MIN (the largest) + margin, in the target CRS."""
        z = self.inp.zoom_min
        covered = self.grid.range_bounds(self.grid.tile_range(self.inp.extent, z))
        margin = self.cfg.edge_margin_px * max(self.grid.pixel_size(z))
        r = covered.buffered(margin)
        m = self.grid.matrix_extent()
        if r.xmin <= m.xmin and r.ymin <= m.ymin and r.xmax >= m.xmax and r.ymax >= m.ymax:
            return None
        return QgsRectangle(r.xmin, r.ymin, r.xmax, r.ymax)

    # ------------------------------------------------------------------
    def run(self) -> Dict[str, object]:
        t_all = time.monotonic()
        inp, cfg = self.inp, self.cfg
        snap = inp.snapshot
        out_dir = Path(inp.output_dir)
        key = self.work_key()
        work = out_dir / ".pxyz_work" / key
        (work / "export").mkdir(parents=True, exist_ok=True)
        (work / "parts").mkdir(parents=True, exist_ok=True)
        (work / "logs").mkdir(parents=True, exist_ok=True)
        log.debug("Work directory: %s", work)
        log.debug(inp.mapping.describe())
        jobs_n, why = resolve_max_parallel_jobs(cfg, cpu_percent=inp.cpu_percent)
        e = inp.extent
        log.debug("Output extent (%s): %r, %r, %r, %r; parallel processes: %s", inp.target_crs, e.xmin, e.ymin,
                  e.xmax, e.ymax, why)
        log.info("Zooms %d-%d, %d layer(s), up to %d parallel processes", inp.zoom_min, inp.zoom_max,
                 len(snap.render_order), jobs_n)

        # Step 1 -------------------------------------------------------------
        snap.adopt_layers()
        try:
            for w in snap.warnings:
                log.warning(w)
            log.debug("Visible layers in drawing order: %s",
                      ", ".join(snap.layers[i].name() for i in snap.render_order))

            # Steps 2+3 ------------------------------------------------------
            t0 = time.monotonic()
            exports, order_fields = self.step_export(work)
            self.timings["export"] = time.monotonic() - t0
            log.info("Exported %d features from %d layer(s) in %.0fs (only used fields, filters applied)",
                     sum(v["count"] for v in exports.values()), len(exports), self.timings["export"])

            # Step 4 ---------------------------------------------------------
            t0 = time.monotonic()
            project_path = str(work / "render.qgz")
            marker = work / "project.json"
            if _read_json(marker) == {"ok": True} and os.path.isfile(project_path):
                log.debug("Reusing render project %s", project_path)
            else:
                rep = build_render_project(snap, {k: (v["path"], v["count"]) for k, v in exports.items()},
                                           self.crs, project_path, order_fields)
                for c in rep.checks:
                    log.debug("  verify: %s", c)
                for w in rep.warnings:
                    log.warning(w)
                _write_json(marker, {"ok": True})
                log.debug("Render project saved: %s", project_path)
            self.timings["project"] = time.monotonic() - t0
            self.progress(15, 2, 1)
            self.check_cancel()

            # Step 5 ---------------------------------------------------------
            t0 = time.monotonic()
            qp = find_qgis_process(inp.qgis_prefix, inp.qgis_version, cfg.qgis_process_path)
            log.debug("qgis_process: %s", qp)
            profile = profile_arguments(inp.settings_dir) if inp.settings_dir else []
            log.debug("qgis_process user profile: %s", " ".join(profile[1:2]) or "(qgis_process default)")
            runner = QgisProcessRunner(qp, inp.mapping.algorithm_id, project_path, work / "logs",
                                       ellipsoid=snap.ellipsoid or None, skip_plugins=cfg.qgis_process_skip_plugins,
                                       profile_args=profile)
            self.step_render_check(project_path, runner, exports)
            self.check_cancel()
            plan = self.step_plan(work, exports, runner, jobs_n)
            self.timings["plan"] = time.monotonic() - t0
            self.progress(17, 3, 1)
        finally:
            snap.dispose()

        # Step 6 -------------------------------------------------------------
        t0 = time.monotonic()
        specs = []
        for j in plan.jobs:
            out = str(work / "parts" / f"{j.job_id}.gpkg")
            specs.append(JobSpec(j.job_id, inp.mapping.job_inputs(cfg, j.extent, inp.target_crs, j.zoom_min,
                                                                  j.zoom_max, out), out, j.workload + 1e-6))
        manifest = Manifest(work / "manifest.json", _sha(plan.to_dict()))
        log.info("Rendering %d job(s) on %d parallel processes ...", len(specs), jobs_n)
        results = run_jobs(specs, runner, jobs_n, manifest, self.canceled,
                           lambda f: self.progress(20, 75, f), lambda m: log.info("%s", m),
                           retries=cfg.max_job_retries, validate_output=validate_part)
        self.timings["render"] = time.monotonic() - t0
        ran = [r for r in results if not r.skipped]
        log.info("Rendering finished in %s", _duration(self.timings["render"]))
        if ran:
            log.debug("Sum of job times %.1fs", sum(r.seconds for r in ran))

        # Step 7 -------------------------------------------------------------
        t0 = time.monotonic()
        by_id = {s.job_id: s.output for s in specs}
        ordered = [j.job_id for j in plan.jobs if j.tile_range is None] + \
                  [j.job_id for j in plan.jobs if j.tile_range is not None]
        parts = [by_id[i] for i in ordered]
        final = str(out_dir / cfg.output_filename)
        log.info("Merging %d part(s) ...", len(parts))
        report = merge_parts(parts, final, (e.xmin, e.ymin, e.xmax, e.ymax), inp.zoom_min, inp.zoom_max,
                             self.matrix_row, vacuum=cfg.vacuum,
                             progress=lambda f: self.progress(95, 5, f), is_canceled=self.canceled)
        self.timings["merge"] = time.monotonic() - t0
        for z in range(inp.zoom_min, inp.zoom_max + 1):
            log.debug("  zoom %2d: %d tiles", z, report.tiles_per_zoom.get(z, 0))
        log.debug("integrity_check=%s, table '%s'", report.integrity, report.tile_table)
        log.debug("Feature accounting (all checks passed, no feature lost):")
        for v in exports.values():
            log.debug("  %s: %d after layer filter = %d in render area + %d entirely outside it; %d exported "
                      "(ids verified) = %d in the saved render project; %d without geometry",
                      v.get("name", "?"), v.get("source_count", -1), v["count"], v.get("outside_area", 0),
                      v["count"], v["count"], v.get("null_geometry", 0))

        if not cfg.keep_temp_files:
            shutil.rmtree(work, ignore_errors=True)
            try:
                (out_dir / ".pxyz_work").rmdir()
            except OSError:
                pass
        else:
            log.info("Temporary files kept in %s", work)
        self.timings["total"] = time.monotonic() - t_all
        log.debug("Timings (s): " + ", ".join(f"{k}={v:.1f}" for k, v in self.timings.items()))
        outside = sum(v.get("outside_area", 0) for v in exports.values())
        log.info("Feature check passed: all %d features in the render area were exported and rendered%s",
                 sum(v["count"] for v in exports.values()),
                 f" ({outside} lie entirely outside it)" if outside else "")
        log.info("Done in %s: %d tiles -> %s", _duration(self.timings["total"]), report.total_tiles, final)
        self.progress(100, 0, 1)
        return {"output": final, "tiles": report.total_tiles, "per_zoom": report.tiles_per_zoom,
                "jobs": len(plan.jobs), "timings": dict(self.timings)}

    # ------------------------------------------------------------------
    def matrix_row(self, z: int):
        cols, rows = self.grid.matrix_size(z)
        px, py = self.grid.pixel_size(z)
        return cols, rows, self.cfg.tile_width, self.cfg.tile_height, px, py

    def step_export(self, work: Path):
        snap = self.inp.snapshot
        marker = work / "export.json"
        prev = _read_json(marker)
        if prev and all(os.path.isfile(v["path"]) for v in prev["exports"].values()) and \
                set(prev["exports"]) == set(snap.vector_ids()):
            log.info("Resuming: reusing %d exported layer(s)", len(prev["exports"]))
            return prev["exports"], prev["order_fields"]

        filter_rect = self.export_filter_rect()
        log.debug("Steps 2-3: export filter (target CRS): %s",
                 filter_rect.toString(6) if filter_rect is not None else "none (whole matrix is rendered)")
        exports: Dict[str, dict] = {}
        order_fields: Dict[str, str] = {}
        ids = snap.vector_ids()
        for n, lid in enumerate(ids):
            self.check_cancel()
            layer: QgsVectorLayer = snap.layers[lid]
            analysis = analyze_layer_fields(layer)
            for w in analysis.warnings:
                log.warning("%s: %s", layer.name(), w)
            r = layer.renderer()
            # source feature id, exported for the feature accounting (and drawing order)
            id_name = ID_FIELD
            while layer.fields().lookupField(id_name) >= 0:
                id_name += "_"
            layer.addExpressionField("$id", QgsField(id_name, QMetaType.Type.LongLong))
            analysis.required.append(id_name)
            if self.cfg.preserve_feature_order and r is not None and not r.orderByEnabled():
                order_fields[lid] = id_name
            log.debug("  %s", analysis.summary(layer.fields().count()))
            if layer.subsetString():
                log.debug("  %s: layer filter: %s", layer.name(), layer.subsetString())
            out = str(work / "export" / f"layer{n:03d}.fgb")
            base, span = 15 * n / max(1, len(ids)), 15 / max(1, len(ids))
            fb = _SubFeedback(self.feedback, base, span)
            t0 = time.monotonic()
            res = export_layer(layer, analysis, out, self.crs, filter_rect, snap.transform_context, fb, id_name)
            self.check_cancel()
            for note in res.notes:
                log.warning("  %s: %s", layer.name(), note)
            log.debug("  %s: features after layer filter %d -> inside render area %d (%d entirely outside it, "
                     "not drawn by any tile) -> exported %d (%d without geometry) in %.1fs",
                     layer.name(), res.source_count, res.expected_count, res.outside_area, res.exported_count,
                     res.null_geometry, time.monotonic() - t0)
            if res.missing_ids or res.extra_ids or res.exported_count != res.expected_count:
                raise ExportError(
                    f"{layer.name()}: feature accounting failed: {res.expected_count} source features in the "
                    f"render area, {res.exported_count} exported; missing ids {res.missing_ids[:20]}"
                    f"{'...' if len(res.missing_ids) > 20 else ''}, unexpected ids {res.extra_ids[:20]}")
            exports[lid] = {"path": res.path, "count": res.exported_count, "fields": res.fields,
                            "renderer": r.type() if r else None,
                            "geometry_generators": _has_geometry_generator(layer),
                            "source_count": res.source_count, "null_geometry": res.null_geometry,
                            "outside_area": res.outside_area, "name": layer.name(),
                            "expressions": analysis.expressions}
        _write_json(marker, {"exports": exports, "order_fields": order_fields})
        return exports, order_fields

    def step_render_check(self, project_path: str, runner: QgisProcessRunner, exports: Dict[str, dict]) -> None:
        """Verify the saved render project before rendering.

        * re-read from disk (the file the jobs load): every layer valid, exactly
          the exported number of features, renderer type unchanged,
        * Python-implemented expression functions in the styles -> jobs load plugins,
        * label fonts not installed -> warning (the built-in tool falls back the same way).
        """
        exprs = sorted({e for v in exports.values() for e in v.get("expressions", [])})
        py_funcs = python_functions_used(exprs)
        if py_funcs and runner.skip_plugins:
            log.warning("Styles use Python expression functions (%s): rendering jobs will load plugins",
                        ", ".join(sorted(py_funcs)))
            runner.skip_plugins = False
        snap = self.inp.snapshot
        for f in missing_label_fonts(snap.layers[i] for i in snap.render_order):
            log.warning("Label font '%s' is not installed: labels fall back to another font "
                        "(the built-in tool behaves the same)", f)
        seen = {x["id"]: x for x in check_saved_project(project_path)}
        for lid in snap.render_order:
            x = seen.get(lid)
            if x is None:
                raise PipelineError(f"layer '{snap.layers[lid].name()}' is missing from the render project "
                                    f"as loaded by qgis_process")
            if not x.get("valid"):
                raise PipelineError(f"layer '{x['name']}' is not valid when qgis_process loads the render project")
            if lid in exports:
                if x.get("feature_count") != exports[lid]["count"]:
                    raise PipelineError(f"{x['name']}: the saved render project has {x.get('feature_count')} "
                                        f"features, {exports[lid]['count']} were exported")
                if x.get("renderer") != exports[lid]["renderer"]:
                    raise PipelineError(f"{x['name']}: renderer '{x.get('renderer')}' in the saved render project, "
                                        f"'{exports[lid]['renderer']}' in the original")
        log.debug("Render check: saved project reloaded, %d layer(s) valid, feature counts match the export",
                 len(seen))

    def step_plan(self, work: Path, exports: Dict[str, dict], runner: QgisProcessRunner, jobs_n: int) -> PartitionPlan:
        inp, cfg = self.inp, self.cfg
        plan_path = work / "plan.json"
        prev = _read_json(plan_path)
        if prev:
            plan = PartitionPlan.from_dict(prev)
            log.info("Resuming: reusing the plan with %d job(s)", len(plan.jobs))
            return plan

        total_features = sum(v["count"] for v in exports.values())
        split = cfg.split_zoom if cfg.split_zoom != "auto" else choose_split_zoom(
            self.grid, inp.extent, inp.zoom_min, inp.zoom_max, cfg.split_tile_threshold)
        split = max(split, inp.zoom_min)
        log.debug("Step 5: split zoom = %d (%s)", split, "auto" if cfg.split_zoom == "auto" else "defaults.json")

        startup = 0.0
        if split <= inp.zoom_max:
            startup = self.job_startup(work, runner)
        cm = CostModel(cfg.cost_per_metatile_s, cfg.cost_per_feature_s, startup)

        occ = None
        if split <= inp.zoom_max and cfg.skip_empty_tiles:
            occ = Occupancy(self.grid, inp.extent, range(split, inp.zoom_max + 1), cfg.metatile_size,
                            cfg.edge_margin_px)
            snap = inp.snapshot
            for lid in snap.render_order:
                self.check_cancel()
                lyr = snap.layers[lid]
                if lid in exports:
                    info = exports[lid]
                    if info["renderer"] in UNBOUNDED_RENDERERS:
                        log.warning("%s uses renderer '%s' which draws outside its features: "
                                    "every block is treated as occupied", lyr.name(), info["renderer"])
                        occ.mark_all()
                        continue
                    if info.get("geometry_generators"):
                        log.warning("%s uses geometry generators: occupancy assumes they draw within "
                                    "edge_margin_px (%g px) of the features", lyr.name(), cfg.edge_margin_px)
                    t0 = time.monotonic()
                    n = scan_vector_file(info["path"], occ, self.canceled)
                    self.check_cancel()
                    with_geom = info["count"] - info.get("null_geometry", 0)
                    log.debug("  occupancy: %s, %d of %d features with geometry scanned in %.1fs", lyr.name(), n,
                             with_geom, time.monotonic() - t0)
                    if n != with_geom:
                        raise PipelineError(f"{lyr.name()}: occupancy scanned {n} features but {with_geom} exported "
                                            f"features have a geometry")
                else:
                    scan_other_layer(lyr, occ, self.crs, snap.transform_context)
                    log.debug("  occupancy: %s (not vector) marks its whole extent", lyr.name())
        elif split <= inp.zoom_max:
            log.debug("skip_empty_tiles is false: every block is rendered (no occupancy skipping)")

        plan = build_plan(self.grid, inp.extent, inp.zoom_min, inp.zoom_max, cfg.metatile_size, split, occ, cm,
                          jobs_n, total_features)
        if not plan.jobs:
            # nothing to render at all: one single-tile job still creates a valid, empty GeoPackage
            tr = self.grid.tile_range(inp.extent, inp.zoom_min)
            one = TileRange(tr.zoom, tr.min_col, tr.min_col, tr.min_row, tr.min_row)
            plan.jobs.append(Job(f"z{one.zoom}_c{one.min_col}-{one.min_col}_r{one.min_row}-{one.min_row}",
                                 one.zoom, one.zoom, self.grid.job_extent(one), 1, 0, cm.per_metatile, one))
        if occ is not None:
            verify_coverage(occ, plan)
        s = plan.stats
        if plan.split_zoom > inp.zoom_max:
            log.info("Plan: 1 job over the full extent (small area, nothing to split)")
            log.debug("%d metatiles, estimated %.0fs", s["planned_metatiles"], s["estimated_serial_s"])
            _write_json(plan_path, plan.to_dict())
            return plan
        log.info("Plan: %d job(s); %.0f%% of the map is empty and skipped", s["jobs"], 100 * s["work_saved_share"])
        log.debug("Partitioned zooms %d-%d: %d of %d metatiles occupied; metatiles to render %d of %d (single run); "
                  "estimated %.0fs serial, %.0fs with %d worker(s) (job start-up %.1fs)",
                  plan.split_zoom, inp.zoom_max, s["partitioned_metatiles_occupied"],
                  s["partitioned_metatiles_total"], s["planned_metatiles"], s["total_metatiles"],
                  s["estimated_serial_s"], s["estimated_makespan_s"], jobs_n, startup)
        for z in range(plan.split_zoom, inp.zoom_max + 1):
            zj = [j for j in plan.jobs if j.tile_range is not None and j.zoom_min == z]
            if zj:
                log.debug("  zoom %2d: %d job(s), %d metatiles", z, len(zj), sum(j.metatiles for j in zj))
        _write_json(plan_path, plan.to_dict())
        return plan

    def job_startup(self, work: Path, runner: QgisProcessRunner) -> float:
        """qgis_process start-up + project load, measured once with a one-tile job."""
        if self.cfg.job_startup_s != "auto":
            return float(self.cfg.job_startup_s)
        cache = work / "startup.json"
        prev = _read_json(cache)
        if prev and "seconds" in prev:
            return float(prev["seconds"])
        inp = self.inp
        tr = self.grid.tile_range(inp.extent, inp.zoom_min)
        one = TileRange(tr.zoom, tr.min_col, tr.min_col, tr.min_row, tr.min_row)
        out = str(work / "startup_probe.gpkg")
        spec = JobSpec("startup_probe", inp.mapping.job_inputs(self.cfg, self.grid.job_extent(one), inp.target_crs,
                                                                one.zoom, one.zoom, out), out)
        import threading
        t0 = time.monotonic()
        try:
            runner.run_once(spec, 1, threading.Event())
        except JobFailed as e:
            raise PipelineError(f"the built-in algorithm failed on a one-tile test job: {e}") from e
        secs = time.monotonic() - t0
        log.debug("Measured qgis_process start-up + project load: %.1fs", secs)
        _write_json(cache, {"seconds": secs})
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(out + suffix):
                os.remove(out + suffix)
        return secs


def verify_coverage(occ: Occupancy, plan: PartitionPlan) -> None:
    """Every occupied metatile (i.e. every feature's footprint + margin) must be rendered by a job.

    Raises PipelineError otherwise, so no feature can silently miss its tiles.
    """
    for z, zo in occ.zooms.items():
        jobs = [j for j in plan.jobs if j.tile_range is not None and j.zoom_min == z]
        g = zo.mgrid
        boxes = []
        for j in jobs:
            r = j.tile_range
            idx = g.meta_index_range((r.min_col, r.max_col), (r.min_row, r.max_row))
            if idx is not None:
                boxes.append(idx)
        if zo.all_occupied:
            covered = sum((b[1] - b[0] + 1) * (b[3] - b[2] + 1) for b in boxes)
            if covered < g.count:
                raise PipelineError(f"zoom {z}: jobs cover {covered} of {g.count} metatiles")
            log.debug("  coverage zoom %2d: all %d metatiles rendered by %d job(s)", z, g.count, len(jobs))
            continue
        by_col: Dict[int, List] = {}  # metatile column -> row intervals of the jobs covering it
        for i0, i1, j0, j1 in boxes:
            for i in range(i0, i1 + 1):
                by_col.setdefault(i, []).append((j0, j1))
        lost = [c for c in zo.cells if not any(j0 <= c[1] <= j1 for j0, j1 in by_col.get(c[0], ()))]
        if lost:
            raise PipelineError(f"zoom {z}: {len(lost)} occupied metatile(s) are not covered by any job, "
                                f"e.g. {lost[:5]}")
        log.debug("  coverage zoom %2d: all %d occupied metatiles rendered by %d job(s)", z, len(zo.cells), len(jobs))


def _has_geometry_generator(layer: QgsVectorLayer) -> bool:
    r = layer.renderer()
    if r is None:
        return False

    def walk(sym) -> bool:
        if sym is None:
            return False
        for sl in sym.symbolLayers():
            if isinstance(sl, QgsGeometryGeneratorSymbolLayer) or walk(sl.subSymbol()):
                return True
        return False

    return any(walk(s) for s in r.symbols(QgsRenderContext()))


class _SubFeedback(QgsProcessingFeedback):
    """Maps a child operation's 0-100 progress into a slice of the parent's."""

    def __init__(self, parent: QgsProcessingFeedback, base: float, span: float) -> None:
        super().__init__()
        self.progressChanged.connect(lambda p: parent.setProgress(base + span * p / 100.0),
                                     Qt.ConnectionType.DirectConnection)
        # Direct: the Processing worker thread has no event loop to deliver queued signals,
        # and the C++ writer polls isCanceled() (not virtual) on this object.
        parent.canceled.connect(self.cancel, Qt.ConnectionType.DirectConnection)
        if parent.isCanceled():
            self.cancel()
