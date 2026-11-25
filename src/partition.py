"""Work partitioning: occupancy, adaptive blocks, jobs and scheduling.

Design notes
------------
The built-in algorithm anchors metatiles at the first tile of the requested
range, separately for every zoom. A job whose extent started at an arbitrary
tile would therefore shift the metatile boundaries (and the labels placed on
them). To keep *identical* metatiles:

* zooms ``ZOOM_MIN .. split_zoom-1`` are rendered by one job over the full
  extent (exactly like a single run),
* every zoom ``>= split_zoom`` is partitioned on its own, in units of that
  zoom's full-run metatiles. A block is a rectangle of whole metatiles, so the
  job's first tile is always a full-run metatile origin and every metatile of
  the job equals the metatile of the single run.

Blocks are only produced where features exist (occupancy with a pixel margin)
when ``skip_empty_tiles`` is on; empty metatiles would produce no tiles anyway.

No QGIS dependency: geometry tests are injected as callables.
"""

from __future__ import annotations

import heapq
import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .tile_grid import MetatileGrid, Rect, TileGrid, TileRange

Cell = Tuple[int, int]
RectPredicate = Callable[[Rect], bool]

#: Features touching more metatiles than this use the exact geometry test.
LARGE_FEATURE_CELLS = 16
#: Safety valve: above this many occupied cells at one zoom treat the zoom as fully occupied.
MAX_CELLS_PER_ZOOM = 6_000_000


# ---------------------------------------------------------------------------
# Occupancy
# ---------------------------------------------------------------------------
class ZoomOccupancy:
    """Occupied metatiles (with feature counts) of the full run at one zoom."""

    def __init__(self, grid: TileGrid, extent: Rect, zoom: int, metatile_size: int, margin_px: float) -> None:
        self.grid = grid
        self.zoom = zoom
        self.mgrid: MetatileGrid = grid.metatile_grid(extent, zoom, metatile_size)
        px, py = grid.pixel_size(zoom)
        self.margin = margin_px * max(px, py)
        self.cells: Dict[Cell, int] = {}
        self.all_occupied = False
        self.total_features = 0

    # -- helpers -------------------------------------------------------------
    def _index_range(self, rect: Rect) -> Optional[Tuple[int, int, int, int]]:
        cols, rows = self.grid.tile_cols_rows_for_rect(rect, self.zoom)
        return self.mgrid.meta_index_range(cols, rows)

    def metatile_rect(self, i0: int, i1: int, j0: int, j1: int) -> Rect:
        return self.grid.range_bounds(self.mgrid.tile_span(i0, i1, j0, j1))

    def _mark(self, i0: int, i1: int, j0: int, j1: int, weight: int) -> None:
        cells = self.cells
        if (i1 - i0 + 1) * (j1 - j0 + 1) + len(cells) > MAX_CELLS_PER_ZOOM:
            self.mark_all()
            return
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                cells[(i, j)] = cells.get((i, j), 0) + weight
        if len(cells) > MAX_CELLS_PER_ZOOM:
            self.mark_all()

    # -- public API ----------------------------------------------------------
    def mark_all(self) -> None:
        """Treat every metatile of this zoom as occupied (e.g. inverted polygons)."""
        self.all_occupied = True
        self.cells = {}

    def add_feature(self, bbox: Rect, intersects: Optional[RectPredicate] = None,
                    contains: Optional[RectPredicate] = None) -> None:
        """Mark metatiles touched by a feature expanded by the pixel margin.

        Small features use their envelope. Features spanning more than
        :data:`LARGE_FEATURE_CELLS` metatiles use ``intersects`` (exact geometry
        test against a margin-expanded metatile rectangle) with a quadtree
        descent; ``contains`` lets fully covered quadrants be marked at once.
        """
        self.total_features += 1
        if self.all_occupied:
            return
        idx = self._index_range(bbox.buffered(self.margin))
        if idx is None:
            return
        i0, i1, j0, j1 = idx
        if intersects is None or (i1 - i0 + 1) * (j1 - j0 + 1) <= LARGE_FEATURE_CELLS:
            self._mark(i0, i1, j0, j1, 1)
            return
        self._descend(i0, i1, j0, j1, intersects, contains)

    def _descend(self, i0: int, i1: int, j0: int, j1: int,
                 intersects: RectPredicate, contains: Optional[RectPredicate]) -> None:
        stack = [(i0, i1, j0, j1)]
        while stack and not self.all_occupied:
            a0, a1, b0, b1 = stack.pop()
            r = self.metatile_rect(a0, a1, b0, b1)
            if not intersects(r.buffered(self.margin)):
                continue
            n = (a1 - a0 + 1) * (b1 - b0 + 1)
            if n == 1 or (contains is not None and contains(r)):
                self._mark(a0, a1, b0, b1, 1)
                continue
            for c in _quarters(a0, a1, b0, b1):
                stack.append(c)

    def add_rect(self, rect: Rect) -> None:
        """Mark everything touching ``rect`` (used for raster layer extents)."""
        if self.all_occupied:
            return
        idx = self._index_range(rect.buffered(self.margin))
        if idx is not None:
            self._mark(*idx, 0)

    @property
    def occupied_count(self) -> int:
        return self.mgrid.count if self.all_occupied else len(self.cells)


class Occupancy:
    """Occupancy of all partitioned zooms (``split_zoom .. zoom_max``)."""

    def __init__(self, grid: TileGrid, extent: Rect, zooms: Iterable[int], metatile_size: int, margin_px: float) -> None:
        self.zooms: Dict[int, ZoomOccupancy] = {
            z: ZoomOccupancy(grid, extent, z, metatile_size, margin_px) for z in zooms
        }
        self.feature_count = 0

    def add_feature(self, bbox: Rect, intersects: Optional[RectPredicate] = None,
                    contains: Optional[RectPredicate] = None) -> None:
        self.feature_count += 1
        for zo in self.zooms.values():
            zo.add_feature(bbox, intersects, contains)

    def add_rect(self, rect: Rect) -> None:
        for zo in self.zooms.values():
            zo.add_rect(rect)

    def mark_all(self) -> None:
        for zo in self.zooms.values():
            zo.mark_all()


def _quarters(i0: int, i1: int, j0: int, j1: int) -> List[Tuple[int, int, int, int]]:
    """Split an index rectangle in (up to) four, aligned to whole metatiles."""
    ispl = [(i0, i1)] if i0 == i1 else [(i0, (i0 + i1) // 2), ((i0 + i1) // 2 + 1, i1)]
    jspl = [(j0, j1)] if j0 == j1 else [(j0, (j0 + j1) // 2), ((j0 + j1) // 2 + 1, j1)]
    return [(a, b, c, d) for a, b in ispl for c, d in jspl]


# ---------------------------------------------------------------------------
# Blocks, jobs and cost model
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CostModel:
    """Workload = per-metatile fixed cost + per-feature cost (seconds)."""

    per_metatile: float
    per_feature: float
    startup: float

    def cost(self, metatiles: int, features: int) -> float:
        return metatiles * self.per_metatile + features * self.per_feature


@dataclass(frozen=True)
class Block:
    """Rectangle of whole full-run metatiles at one zoom (inclusive indices)."""

    zoom: int
    i0: int
    i1: int
    j0: int
    j1: int
    occupied: int
    features: int

    @property
    def metatiles(self) -> int:
        return (self.i1 - self.i0 + 1) * (self.j1 - self.j0 + 1)

    def workload(self, cm: CostModel) -> float:
        return cm.cost(self.metatiles, self.features)

    def overlaps(self, o: "Block") -> bool:
        return not (o.i0 > self.i1 or o.i1 < self.i0 or o.j0 > self.j1 or o.j1 < self.j0)


@dataclass
class Job:
    """One qgis_process run of the built-in algorithm."""

    job_id: str
    zoom_min: int
    zoom_max: int
    extent: Rect
    metatiles: int
    features: int
    workload: float
    tile_range: Optional[TileRange] = None  # None for the full-extent low-zoom job

    def to_dict(self) -> dict:
        d = {
            "job_id": self.job_id, "zoom_min": self.zoom_min, "zoom_max": self.zoom_max,
            "extent": [self.extent.xmin, self.extent.ymin, self.extent.xmax, self.extent.ymax],
            "metatiles": self.metatiles, "features": self.features, "workload": self.workload,
        }
        if self.tile_range:
            r = self.tile_range
            d["tile_range"] = [r.zoom, r.min_col, r.max_col, r.min_row, r.max_row]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        tr = TileRange(*d["tile_range"]) if d.get("tile_range") else None
        return cls(d["job_id"], d["zoom_min"], d["zoom_max"], Rect(*d["extent"]), d["metatiles"],
                   d["features"], d["workload"], tr)


@dataclass
class PartitionPlan:
    jobs: List[Job]
    split_zoom: int
    stats: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"split_zoom": self.split_zoom, "stats": self.stats, "jobs": [j.to_dict() for j in self.jobs]}

    @classmethod
    def from_dict(cls, d: dict) -> "PartitionPlan":
        return cls([Job.from_dict(j) for j in d["jobs"]], d["split_zoom"], d.get("stats", {}))


def choose_split_zoom(grid: TileGrid, extent: Rect, zoom_min: int, zoom_max: int, threshold: int) -> int:
    """Lowest zoom whose full-extent tile count exceeds ``threshold``.

    Returns ``zoom_max + 1`` when no zoom does (single job).
    """
    for z in range(zoom_min, zoom_max + 1):
        if grid.tile_range(extent, z).count > threshold:
            return z
    return zoom_max + 1


def partition_zoom(occ: ZoomOccupancy, cm: CostModel, target: float) -> List[Block]:
    """Adaptive quadtree partition of one zoom's occupied metatiles.

    A block is split while its workload exceeds ``target`` or while the cost of
    the empty metatiles it would render exceeds one job start-up. Blocks are
    then tightened to their occupied cells and small neighbours are merged when
    that saves start-up overhead.
    """
    g = occ.mgrid
    if g.count == 0:
        return []
    out: List[Block] = []

    if occ.all_occupied:
        per_cell_features = occ.total_features / max(1, g.count)
        stack = [(0, g.meta_cols - 1, 0, g.meta_rows - 1)]
        while stack:
            i0, i1, j0, j1 = stack.pop()
            n = (i1 - i0 + 1) * (j1 - j0 + 1)
            feats = int(round(per_cell_features * n))
            if n > 1 and cm.cost(n, feats) > target:
                stack.extend(_quarters(i0, i1, j0, j1))
            else:
                out.append(Block(occ.zoom, i0, i1, j0, j1, n, feats))
        return _merge_blocks(out, cm, target)

    cells = [(i, j, c) for (i, j), c in occ.cells.items()]
    if not cells:
        return []

    def solve(cs: list) -> Tuple[float, List[Block]]:
        """Cheapest way (render cost + one start-up per job) to cover the cells ``cs``.

        A block tightened to its cells is either one job, or split in quarters,
        whichever costs less: splitting removes empty metatiles but every extra
        job pays a start-up. Blocks heavier than ``target`` are always split
        (load balancing). No split is tried when the empty metatiles cost less
        than one start-up, as no split could pay off.
        """
        i0 = min(c[0] for c in cs); i1 = max(c[0] for c in cs)  # noqa: E702
        j0 = min(c[1] for c in cs); j1 = max(c[1] for c in cs)  # noqa: E702
        n = (i1 - i0 + 1) * (j1 - j0 + 1)
        feats = sum(c[2] for c in cs)
        base = cm.cost(n, feats)
        leaf = (base + cm.startup, [Block(occ.zoom, i0, i1, j0, j1, len(cs), feats)])
        if n == 1:
            return leaf
        must_split = base > target
        if not must_split and (n - len(cs)) * cm.per_metatile <= cm.startup:
            return leaf
        total, blocks = 0.0, []
        for a0, a1, b0, b1 in _quarters(i0, i1, j0, j1):
            sub = [c for c in cs if a0 <= c[0] <= a1 and b0 <= c[1] <= b1]
            if sub:
                cost, bl = solve(sub)
                total += cost
                blocks.extend(bl)
        return (total, blocks) if must_split or total < leaf[0] else leaf

    out = solve(cells)[1]
    return _merge_blocks(out, cm, target)


def _merge_blocks(blocks: List[Block], cm: CostModel, target: float, max_n: int = 4000) -> List[Block]:
    """Greedily merge neighbouring blocks when start-up overhead dominates.

    Two blocks merge into their bounding rectangle when the rectangle does not
    overlap any other block, its cost is at most both costs plus one saved
    start-up, and it stays below ``target``.
    """
    if len(blocks) < 2 or len(blocks) > max_n:
        return blocks
    blocks = sorted(blocks, key=lambda b: (b.i0, b.j0))
    changed = True
    while changed:
        changed = False
        for a_idx in range(len(blocks)):
            a = blocks[a_idx]
            best = None
            for b_idx in range(a_idx + 1, len(blocks)):
                b = blocks[b_idx]
                if b.i0 > a.i1 + 2:  # sorted by i0: further blocks are not neighbours
                    break
                if b.j0 > a.j1 + 2 or b.j1 < a.j0 - 2:
                    continue
                u = Block(a.zoom, min(a.i0, b.i0), max(a.i1, b.i1), min(a.j0, b.j0), max(a.j1, b.j1),
                          a.occupied + b.occupied, a.features + b.features)
                cu = u.workload(cm)
                if cu > target or cu > a.workload(cm) + b.workload(cm) + cm.startup:
                    continue
                if any(u.overlaps(o) for k, o in enumerate(blocks) if k not in (a_idx, b_idx)):
                    continue
                best = (b_idx, u)
                break
            if best is not None:
                b_idx, u = best
                blocks = [blk for k, blk in enumerate(blocks) if k not in (a_idx, b_idx)] + [u]
                blocks.sort(key=lambda b: (b.i0, b.j0))
                changed = True
                break
    return blocks


def block_to_job(grid: TileGrid, occ: ZoomOccupancy, block: Block, cm: CostModel) -> Job:
    tr = occ.mgrid.tile_span(block.i0, block.i1, block.j0, block.j1)
    job_id = f"z{tr.zoom}_c{tr.min_col}-{tr.max_col}_r{tr.min_row}-{tr.max_row}"
    return Job(job_id, block.zoom, block.zoom, grid.job_extent(tr), block.metatiles, block.features,
               block.workload(cm), tr)


def build_plan(grid: TileGrid, extent: Rect, zoom_min: int, zoom_max: int, metatile_size: int,
               split_zoom: int, occupancy: Optional[Occupancy], cm: CostModel, max_jobs: int,
               total_features: int) -> PartitionPlan:
    """Create the job list.

    ``occupancy`` covers zooms ``split_zoom..zoom_max``; ``None`` means
    "render everything" (skip_empty_tiles off, or no partitioned zooms).
    """
    split_zoom = max(split_zoom, zoom_min)
    jobs: List[Job] = []
    full_metatiles = {z: grid.metatile_grid(extent, z, metatile_size).count for z in range(zoom_min, zoom_max + 1)}
    zmax_low = min(split_zoom - 1, zoom_max)
    low_zooms = list(range(zoom_min, zmax_low + 1))
    # every low zoom renders all features once over the full extent
    low_cost = {z: cm.cost(full_metatiles[z], total_features) for z in low_zooms}

    occupied_total = 0
    hi_load = 0.0
    if split_zoom <= zoom_max:
        if occupancy is None:
            occupancy = Occupancy(grid, extent, range(split_zoom, zoom_max + 1), metatile_size, 0)
            occupancy.mark_all()
            for zo in occupancy.zooms.values():
                zo.total_features = total_features
        # total workload of the occupied area (before splitting) to size the blocks
        for z in range(split_zoom, zoom_max + 1):
            zo = occupancy.zooms[z]
            feats = zo.total_features if zo.all_occupied else sum(zo.cells.values())
            hi_load += cm.cost(zo.occupied_count, feats)
            occupied_total += zo.occupied_count
    total = hi_load + sum(low_cost.values())
    target = max(total / (max(1, max_jobs) * 4), 4 * cm.startup, 1e-9)

    # Low zooms: a job over the FULL extent has exactly the single run's metatiles, for
    # any set of zooms. Cheap consecutive zooms share one job; a zoom heavier than the
    # target is cut into blocks of its own metatiles (all occupied), like the high zooms.
    group: List[int] = []

    def flush() -> None:
        if group:
            mt = sum(full_metatiles[z] for z in group)
            feats = total_features * len(group)
            jobs.append(Job(f"full_z{group[0]}-{group[-1]}", group[0], group[-1], extent, mt, feats,
                            cm.cost(mt, feats)))
            group.clear()

    for z in low_zooms:
        if low_cost[z] > target and full_metatiles[z] > 1:
            flush()
            zo = ZoomOccupancy(grid, extent, z, metatile_size, 0)
            zo.mark_all()
            zo.total_features = total_features
            for b in partition_zoom(zo, cm, target):
                jobs.append(block_to_job(grid, zo, b, cm))
            continue
        if group and sum(low_cost[g] for g in group) + low_cost[z] > target:
            flush()
        group.append(z)
    flush()

    if split_zoom <= zoom_max:
        for z in range(split_zoom, zoom_max + 1):
            zo = occupancy.zooms[z]
            for b in partition_zoom(zo, cm, target):
                jobs.append(block_to_job(grid, zo, b, cm))

    jobs.sort(key=lambda j: j.workload, reverse=True)
    total_mt = sum(full_metatiles.values())
    planned_mt = sum(j.metatiles for j in jobs)
    hi_total = sum(full_metatiles[z] for z in range(split_zoom, zoom_max + 1)) if split_zoom <= zoom_max else 0
    makespan = schedule_lpt([j.workload + cm.startup for j in jobs], max_jobs)[0]
    stats = {
        "jobs": len(jobs),
        "total_metatiles": total_mt,
        "planned_metatiles": planned_mt,
        "partitioned_metatiles_total": hi_total,
        "partitioned_metatiles_occupied": occupied_total,
        "work_saved_share": (1.0 - planned_mt / total_mt) if total_mt else 0.0,
        "estimated_serial_s": sum(j.workload for j in jobs),
        "estimated_makespan_s": makespan,
    }
    return PartitionPlan(jobs, split_zoom, stats)


def schedule_lpt(workloads: Sequence[float], workers: int) -> Tuple[float, List[List[int]]]:
    """Largest-processing-time-first schedule; returns (makespan, job indices per worker)."""
    workers = max(1, workers)
    order = sorted(range(len(workloads)), key=lambda i: workloads[i], reverse=True)
    heap = [(0.0, w) for w in range(workers)]
    assign: List[List[int]] = [[] for _ in range(workers)]
    for i in order:
        load, w = heapq.heappop(heap)
        assign[w].append(i)
        heapq.heappush(heap, (load + workloads[i], w))
    return max(load for load, _ in heap), assign


# ---------------------------------------------------------------------------
# Exactness checks (used by the parallel workers method)
# ---------------------------------------------------------------------------
class PlanError(RuntimeError):
    pass


def parse_extent(text: str) -> tuple:
    """'xmin,xmax,ymin,ymax [CRS]' -> (xmin, xmax, ymin, ymax)."""
    nums = [float(v) for v in text.split("[")[0].split(",")]
    if len(nums) != 4:
        raise ValueError(f"not an extent: {text}")
    return tuple(nums)


def verify_plan(grid: TileGrid, extent: Rect, zmin: int, zmax: int, metatile: int, plan: PartitionPlan,
                occ: Optional[Occupancy]) -> None:
    """Raise PlanError unless every metatile of every zoom is rendered by exactly one job."""
    for z in range(zmin, zmax + 1):
        mg = grid.metatile_grid(extent, z, metatile)
        groups = [j for j in plan.jobs if j.tile_range is None and j.zoom_min <= z <= j.zoom_max]
        blocks = [j for j in plan.jobs if j.tile_range is not None and j.zoom_min == z]
        if len(groups) > 1 or (groups and blocks):
            raise PlanError(f"zoom {z}: rendered by more than one job")
        if groups:
            if groups[0].extent != extent:
                raise PlanError(f"zoom {z}: full-extent job has another extent")
            continue
        seen = set()
        for j in blocks:
            if grid.tile_range(j.extent, z) != j.tile_range:
                raise PlanError(f"job {j.job_id}: its extent would select other tiles")
            r = j.tile_range
            box = mg.meta_index_range((r.min_col, r.max_col), (r.min_row, r.max_row))
            if box is None or mg.tile_span(*box) != r:
                raise PlanError(f"job {j.job_id}: not aligned to the single run's metatiles")
            for i in range(box[0], box[1] + 1):
                for k in range(box[2], box[3] + 1):
                    if (i, k) in seen:
                        raise PlanError(f"zoom {z}: metatile {(i, k)} is in two jobs")
                    seen.add((i, k))
        zo = occ.zooms.get(z) if occ is not None else None
        if zo is None or zo.all_occupied:
            if len(seen) != mg.count:
                raise PlanError(f"zoom {z}: {mg.count - len(seen)} metatile(s) not rendered")
        else:
            missing = [c for c in zo.cells if c not in seen]
            if missing:
                raise PlanError(f"zoom {z}: {len(missing)} metatile(s) with features not rendered, e.g. {missing[:3]}")


def verify_parts(jobs: List[Job], outputs: Dict[str, str]) -> None:
    """Every part must contain only tiles of its own zooms and tile range."""
    for j in jobs:
        con = sqlite3.connect(f"file:{outputs[j.job_id]}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT zoom_level, MIN(tile_column), MAX(tile_column), MIN(tile_row), MAX(tile_row) "
                               "FROM tiles GROUP BY zoom_level").fetchall()
        finally:
            con.close()
        for z, c0, c1, r0, r1 in rows:
            if not j.zoom_min <= z <= j.zoom_max:
                raise PlanError(f"part {j.job_id} contains zoom {z}")
            r = j.tile_range
            if r is not None and not (r.min_col <= c0 and c1 <= r.max_col and r.min_row <= r0 and r1 <= r.max_row):
                raise PlanError(f"part {j.job_id} contains tiles outside its range")
