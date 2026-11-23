"""Block alignment, occupancy with margin, workload splitting, scheduling (no QGIS)."""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.partition import (  # noqa: E402
    CostModel, Occupancy, ZoomOccupancy, build_plan, choose_split_zoom, partition_zoom, schedule_lpt)
from src.tile_grid import Rect, TileGrid  # noqa: E402

EXTENT = Rect(34.20, 31.10, 34.95, 31.90)  # ~70 x 90 km


def grid() -> TileGrid:
    return TileGrid.for_target("EPSG:4326", (-180, 180, -90, 90), 2, 1, 256, 256)


class OccupancyTest(unittest.TestCase):
    def test_point_marks_single_metatile_without_margin(self):
        g = grid()
        zo = ZoomOccupancy(g, EXTENT, 14, 8, 0)
        mg = zo.mgrid
        # centre of metatile (2, 3)
        r = zo.metatile_rect(2, 2, 3, 3)
        cx, cy = (r.xmin + r.xmax) / 2, (r.ymin + r.ymax) / 2
        zo.add_feature(Rect(cx, cy, cx, cy))
        self.assertEqual(zo.cells, {(2, 3): 1})
        self.assertGreater(mg.count, 1)

    def test_margin_reaches_neighbour(self):
        g = grid()
        zo0 = ZoomOccupancy(g, EXTENT, 14, 8, 0)
        zo = ZoomOccupancy(g, EXTENT, 14, 8, 100)
        r = zo.metatile_rect(2, 2, 3, 3)
        px = g.pixel_size(14)[0]
        x = r.xmax - 50 * px  # 50 px left of the east edge
        y = (r.ymin + r.ymax) / 2
        zo0.add_feature(Rect(x, y, x, y))
        zo.add_feature(Rect(x, y, x, y))
        self.assertEqual(set(zo0.cells), {(2, 3)})
        self.assertEqual(set(zo.cells), {(2, 3), (3, 3)})
        # margin is in pixels: 100 px at z14 == 100 * pixel size
        self.assertAlmostEqual(zo.margin, 100 * px)

    def test_large_feature_uses_geometry_test(self):
        g = grid()
        zo = ZoomOccupancy(g, EXTENT, 16, 8, 0)
        # a diagonal line across the whole extent: bbox covers everything,
        # but only cells near the diagonal must be marked
        x0, y0, x1, y1 = EXTENT.xmin, EXTENT.ymin, EXTENT.xmax, EXTENT.ymax

        def on_diagonal(rect: Rect) -> bool:
            # segment/rect intersection by sampling (good enough for a test)
            for k in range(2001):
                t = k / 2000
                x, y = x0 + t * (x1 - x0), y0 + t * (y1 - y0)
                if rect.xmin <= x <= rect.xmax and rect.ymin <= y <= rect.ymax:
                    return True
            return False

        zo.add_feature(EXTENT, intersects=on_diagonal)
        self.assertGreater(len(zo.cells), 0)
        self.assertLess(len(zo.cells), zo.mgrid.count / 4)

    def test_mark_all(self):
        zo = ZoomOccupancy(grid(), EXTENT, 12, 8, 0)
        zo.mark_all()
        self.assertTrue(zo.all_occupied)
        self.assertEqual(zo.occupied_count, zo.mgrid.count)


class PartitionTest(unittest.TestCase):
    def setUp(self):
        random.seed(7)
        self.g = grid()
        self.cm = CostModel(per_metatile=0.1, per_feature=0.001, startup=3.0)

    def _points(self, occ: Occupancy, n: int, clusters=((34.4, 31.3), (34.8, 31.8))):
        for i in range(n):
            cx, cy = clusters[i % len(clusters)]
            x, y = cx + random.uniform(-0.02, 0.02), cy + random.uniform(-0.02, 0.02)
            occ.add_feature(Rect(x, y, x, y))

    def test_blocks_aligned_and_cover_all_occupied_cells(self):
        occ = Occupancy(self.g, EXTENT, [17], 8, 100)
        self._points(occ, 3000)
        zo = occ.zooms[17]
        blocks = partition_zoom(zo, self.cm, target=20.0)
        covered = set()
        for b in blocks:
            for i in range(b.i0, b.i1 + 1):
                for j in range(b.j0, b.j1 + 1):
                    self.assertNotIn((i, j), covered, "blocks overlap")
                    covered.add((i, j))
            # alignment: first tile of the block is a full-run metatile origin
            span = zo.mgrid.tile_span(b.i0, b.i1, b.j0, b.j1)
            self.assertEqual((span.min_col - zo.mgrid.range.min_col) % 8, 0)
            self.assertEqual((span.min_row - zo.mgrid.range.min_row) % 8, 0)
        self.assertTrue(set(zo.cells) <= covered)
        # far fewer metatiles than the full zoom
        self.assertLess(len(covered), zo.mgrid.count / 10)

    def test_heavy_blocks_are_split_below_target(self):
        occ = Occupancy(self.g, EXTENT, [18], 8, 0)
        self._points(occ, 20000, clusters=((34.5, 31.5),))
        zo = occ.zooms[18]
        target = 15.0
        blocks = partition_zoom(zo, self.cm, target)
        self.assertGreater(len(blocks), 1)
        for b in blocks:
            if b.metatiles > 1:
                self.assertLessEqual(b.workload(self.cm), target + 1e-9)

    def test_tiny_neighbours_are_merged(self):
        zo = ZoomOccupancy(self.g, EXTENT, 15, 8, 0)
        # two adjacent occupied cells with an empty gap of one cell
        for i in (4, 6):
            r = zo.metatile_rect(i, i, 5, 5)
            c = ((r.xmin + r.xmax) / 2, (r.ymin + r.ymax) / 2)
            zo.add_feature(Rect(c[0], c[1], c[0], c[1]))
        cheap_startup = CostModel(0.1, 0.0, startup=0.01)
        dear_startup = CostModel(0.1, 0.0, startup=30.0)
        self.assertEqual(len(partition_zoom(zo, cheap_startup, target=100)), 2)
        merged = partition_zoom(zo, dear_startup, target=100)
        self.assertEqual(len(merged), 1)
        self.assertEqual((merged[0].i0, merged[0].i1), (4, 6))

    def test_choose_split_zoom(self):
        z = choose_split_zoom(self.g, EXTENT, 0, 19, 20000)
        self.assertGreater(self.g.tile_range(EXTENT, z).count, 20000)
        self.assertLessEqual(self.g.tile_range(EXTENT, z - 1).count, 20000)
        self.assertEqual(choose_split_zoom(self.g, EXTENT, 0, 5, 20000), 6)

    def test_build_plan_jobs(self):
        split = 14
        occ = Occupancy(self.g, EXTENT, range(split, 17), 8, 100)
        self._points(occ, 2000)
        plan = build_plan(self.g, EXTENT, 10, 16, 8, split, occ, self.cm, max_jobs=4, total_features=2000)
        low = [j for j in plan.jobs if j.tile_range is None]
        self.assertEqual(len(low), 1)
        self.assertEqual((low[0].zoom_min, low[0].zoom_max), (10, 13))
        self.assertEqual(low[0].extent, EXTENT)
        for j in plan.jobs:
            if j.tile_range is not None:
                self.assertEqual(j.zoom_min, j.zoom_max)
                self.assertEqual(self.g.tile_range(j.extent, j.zoom_min), j.tile_range)
        self.assertEqual(plan.jobs, sorted(plan.jobs, key=lambda j: j.workload, reverse=True))
        self.assertGreater(plan.stats["work_saved_share"], 0.5)

    def test_build_plan_without_skipping_covers_everything(self):
        plan = build_plan(self.g, EXTENT, 12, 14, 8, 13, None, self.cm, max_jobs=4, total_features=10)
        hi = [j for j in plan.jobs if j.tile_range is not None]
        for z in (13, 14):
            full = self.g.tile_range(EXTENT, z)
            n = sum(j.tile_range.count for j in hi if j.zoom_min == z)
            self.assertEqual(n, full.count)

    def test_lpt(self):
        makespan, assign = schedule_lpt([7, 5, 4, 3, 3, 2], 2)
        self.assertEqual(makespan, 12)
        self.assertEqual(sorted(i for a in assign for i in a), list(range(6)))


if __name__ == "__main__":
    unittest.main()
