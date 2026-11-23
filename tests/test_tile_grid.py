"""Tile-grid math in EPSG:4326 with a 2x1 zoom-0 matrix (no QGIS needed)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.tile_grid import MERC_MAX, Rect, TileGrid  # noqa: E402


def grid4326() -> TileGrid:
    return TileGrid.for_target("EPSG:4326", (-180, 180, -90, 90), 2, 1, 256, 256)


class TileGridTest(unittest.TestCase):
    def test_zoom0_layout(self):
        g = grid4326()
        self.assertEqual(g.matrix_size(0), (2, 1))
        self.assertEqual(g.tile_size(0), 180.0)
        self.assertEqual(g.matrix_extent(), Rect(-180, -90, 180, 90))
        self.assertEqual(g.tile_bounds(0, 0, 0), Rect(-180, -90, 0, 90))
        self.assertEqual(g.tile_bounds(1, 0, 0), Rect(0, -90, 180, 90))

    def test_zoom_halving_and_pixel_size(self):
        g = grid4326()
        for z in range(0, 22):
            self.assertEqual(g.matrix_size(z), (2 << z, 1 << z))
            self.assertAlmostEqual(g.tile_size(z), 180.0 / 2 ** z)
            px, py = g.pixel_size(z)
            self.assertAlmostEqual(px, 180.0 / 2 ** z / 256)
            self.assertAlmostEqual(px, py)

    def test_tile_range_rules(self):
        g = grid4326()
        # zoom 3: tiles are 22.5 degrees, origin top-left (-180, 90)
        r = g.tile_range(Rect(0, 0, 22.5, 22.5), 3)
        # x: floor(180/22.5)=8 .. ceil(202.5/22.5)-1=8 ; y: floor(67.5/22.5)=3 .. ceil(90/22.5)-1=3
        self.assertEqual((r.min_col, r.max_col, r.min_row, r.max_row), (8, 8, 3, 3))
        r = g.tile_range(Rect(0.1, 0.1, 22.6, 22.4), 3)
        self.assertEqual((r.min_col, r.max_col, r.min_row, r.max_row), (8, 9, 3, 3))
        # clamping to the matrix
        r = g.tile_range(Rect(-500, -500, 500, 500), 1)
        self.assertEqual((r.min_col, r.max_col, r.min_row, r.max_row), (0, 3, 0, 1))

    def test_rows_grow_downwards(self):
        g = grid4326()
        north = g.tile_range(Rect(10, 80, 11, 81), 5)
        south = g.tile_range(Rect(10, -81, 11, -80), 5)
        self.assertLess(north.min_row, south.min_row)

    def test_job_extent_selects_exact_range(self):
        g = grid4326()
        for z in (10, 16, 19):
            r = g.tile_range(Rect(34.2, 31.1, 34.9, 31.9), z)
            again = g.tile_range(g.job_extent(r), z)
            self.assertEqual(again, r)
            # tile-aligned bounds without epsilon would grab the neighbours
            loose = g.tile_range(g.range_bounds(r), z)
            self.assertGreaterEqual(loose.count, r.count)

    def test_metatile_grid_anchored_at_range_start(self):
        g = grid4326()
        mg = g.metatile_grid(Rect(34.2, 31.1, 34.9, 31.9), 14, 8)
        span = mg.tile_span(0, 0, 0, 0)
        self.assertEqual(span.min_col, mg.range.min_col)
        self.assertEqual(span.max_col, mg.range.min_col + 7)
        last = mg.tile_span(mg.meta_cols - 1, mg.meta_cols - 1, mg.meta_rows - 1, mg.meta_rows - 1)
        self.assertEqual(last.max_col, mg.range.max_col)
        self.assertEqual(last.max_row, mg.range.max_row)
        self.assertEqual(mg.meta_cols, -(-mg.range.cols // 8))

    def test_non_square_zoom0_extent_is_expanded(self):
        g = TileGrid.from_zoom0_extent(0, 100, 0, 40, 1, 1, 256, 256)
        e = g.matrix_extent()
        self.assertAlmostEqual(e.width, 100)
        self.assertAlmostEqual(e.height, 100)
        self.assertAlmostEqual((e.ymin + e.ymax) / 2, 20)

    def test_web_mercator_ignores_zoom0_extent(self):
        g = TileGrid.for_target("EPSG:3857", (0, 1, 0, 1), 1, 1, 256, 256)
        self.assertAlmostEqual(g.matrix_extent().xmax, MERC_MAX)
        self.assertAlmostEqual(g.tile_size(0), 2 * MERC_MAX)

    def test_processing_string_full_precision(self):
        s = Rect(0.1, 0.2, 0.30000000000000004, 0.4).to_processing_string("EPSG:4326")
        self.assertEqual(s, "0.1,0.30000000000000004,0.2,0.4 [EPSG:4326]")


if __name__ == "__main__":
    unittest.main()
